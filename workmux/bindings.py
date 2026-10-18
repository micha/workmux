"""Parse tmux's evaluated bind-key output, never the user's configuration files."""

from dataclasses import dataclass
import hashlib
import shlex

from tree_sitter import Language, Parser
import tree_sitter_tmux

from .layouts import LAYOUTS


ALIASES = {
    "new-window": "new-window", "neww": "new-window",
    "split-window": "split-window", "splitw": "split-window",
    "select-layout": "select-layout", "selectl": "select-layout",
    "next-layout": "next-layout", "nextl": "next-layout",
    "previous-layout": "previous-layout", "prevl": "previous-layout",
}


class UnsupportedBinding(ValueError):
    """A binding cannot safely be adapted without changing its semantics."""


def binding_token(table: str, key: str) -> str:
    return hashlib.sha256((table + "\0" + key).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Binding:
    table: str
    key: str
    header: str
    body: str
    command: str
    args: tuple[str, ...]

    @property
    def token(self) -> str:
        return binding_token(self.table, self.key)


class BindingParser:
    def __init__(self):
        self.parser = Parser(Language(tree_sitter_tmux.language()))

    def parts(self, line: str):
        source = line.encode()
        root = self.parser.parse(source).root_node
        if len(root.named_children) != 1 or root.named_children[0].type != "bind_key_directive":
            raise UnsupportedBinding("unrecognized binding syntax")
        directive = root.named_children[0]
        children = directive.named_children
        key = next((child for child in children if child.type == "key"), None)
        table = next((child for child in children if child.type == "key_table"), None)
        if key is None:
            raise UnsupportedBinding("missing binding key")
        table_name = shlex.split(table.text.decode())[0] if table else "prefix"
        key_name = shlex.split(key.text.decode())[0]
        bodies = [child for child in children if child.start_byte >= key.end_byte]
        if len(bodies) != 1:
            raise UnsupportedBinding("compound binding")
        body = bodies[0]
        header = source[:body.start_byte].decode()
        return root, table_name, key_name, header, body

    def opaque_outer_command(self, line: str) -> bool:
        """Recognize out-of-scope menus/templates even when their contents do not parse."""
        source = line.encode()
        root = self.parser.parse(source).root_node
        directive = next((node for node in root.named_children if node.type == "bind_key_directive"), None)
        if directive is None:
            return False
        key = next((node for node in directive.named_children if node.type == "key"), None)
        if key is None:
            return False
        words = source[key.end_byte:].lstrip().split(None, 1)
        if not words:
            return False
        return words[0] in {
            b"display-menu", b"menu", b"if-shell", b"if", b"run-shell", b"run",
            b"display-popup", b"popup", b"command-prompt", b"commandp", b"confirm-before", b"confirm",
        }

    def identity(self, line: str) -> tuple[str, str]:
        _, table, key, _, _ = self.parts(line)
        return table, key

    def equivalent(self, first: str, second: str) -> bool:
        try:
            _, _, _, first_header, first_body = self.parts(first)
            _, _, _, second_header, second_body = self.parts(second)
            return (shlex.split(first_header) == shlex.split(second_header)
                    and shlex.split(first_body.text.decode()) == shlex.split(second_body.text.decode()))
        except (UnsupportedBinding, ValueError):
            return first == second

    def parse(self, line: str) -> Binding:
        root, table, key, header, body_node = self.parts(line)
        if root.has_error:
            pending = [root]
            errors = []
            while pending:
                node = pending.pop()
                if node.is_error or node.is_missing:
                    errors.append(node)
                pending.extend(node.children)
            # This grammar incorrectly requires a layout name even for valid -E/-n/-p
            # and no-argument select-layout. Accept only that specific missing node.
            if body_node.type != "select_layout_directive" or not errors or any(
                not node.is_missing or node.type != "string" or node.parent.type != "layout_name"
                for node in errors
            ):
                raise UnsupportedBinding("syntax not supported by the tmux grammar")
        if not body_node.type.endswith("_directive"):
            raise UnsupportedBinding("quoted or compound command body")
        body = body_node.text.decode()
        words = shlex.split(body)
        if not words:
            raise UnsupportedBinding("empty command")
        command = ALIASES.get(words[0], words[0])
        return Binding(table, key, header, body, command, tuple(words[1:]))


def command_options(binding: Binding) -> tuple[set[str], dict[str, int], list[str]]:
    """Validate the supported subset and return value-option indexes in args."""
    supported = {
        "new-window": ("d", "cn"),
        "split-window": ("bdhv", "clp"),
        "select-layout": ("Enp", ""),
        "next-layout": ("", ""),
        "previous-layout": ("", ""),
    }
    if binding.command not in supported:
        raise UnsupportedBinding("not a workspace operation")
    flags_allowed, values_allowed = supported[binding.command]
    flags = set()
    values = {}
    index = 0
    args = list(binding.args)
    while index < len(args) and args[index].startswith("-"):
        option = args[index]
        if option == "--":
            index += 1
            break
        for position, flag in enumerate(option[1:]):
            if flag in flags_allowed:
                flags.add(flag)
            elif flag in values_allowed and position == len(option) - 2 and index + 1 < len(args):
                index += 1
                values[flag] = index
            else:
                raise UnsupportedBinding(f"unsupported option in {binding.command}")
        index += 1
    positional = args[index:]
    if binding.command == "select-layout":
        if flags and positional or len(positional) > 1 or len(flags) > 1:
            raise UnsupportedBinding("unsupported layout selection")
        if positional and positional[0] not in LAYOUTS:
            raise UnsupportedBinding("custom or abbreviated layout")
    elif binding.command in ("next-layout", "previous-layout") and positional:
        raise UnsupportedBinding("unsupported layout arguments")
    return flags, values, positional


def quote_tmux(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("\n", "\\n") + '"'


def wrap_binding(binding: Binding, command: str) -> str:
    return (
        binding.header + "if-shell -F '#{==:#{@workmux-enabled},1}' { run-shell -b "
        + quote_tmux(command + " >/dev/null") + " } { " + binding.body + " }"
    )
