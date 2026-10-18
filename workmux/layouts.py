"""Pure layout geometry, serialized in tmux's portable legacy layout format."""

import json
import re
from dataclasses import dataclass


LAYOUTS = (
    "even-horizontal", "even-vertical", "main-horizontal", "main-horizontal-mirrored",
    "main-vertical", "main-vertical-mirrored", "tiled",
)


@dataclass(frozen=True)
class Cell:
    width: int
    height: int
    x: int
    y: int
    pane: str | None = None
    axis: str = ""
    children: tuple["Cell", ...] = ()

    def pane_ids(self) -> list[str]:
        if self.pane is not None:
            return [self.pane]
        return [pane for child in self.children for pane in child.pane_ids()]

    def serialize(self) -> str:
        header = f"{self.width}x{self.height},{self.x},{self.y}"
        if self.pane is not None:
            return header + "," + self.pane.removeprefix("%")
        opening, closing = ("{", "}") if self.axis == "h" else ("[", "]")
        return header + opening + ",".join(child.serialize() for child in self.children) + closing

    def dump(self) -> str:
        body = self.serialize()
        return f"{layout_checksum(body):04x},{body}"


def layout_checksum(body: str) -> int:
    checksum = 0
    for byte in body.encode("ascii"):
        checksum = ((checksum >> 1) + ((checksum & 1) << 15) + byte) & 0xFFFF
    return checksum


def parse_layout(text: str) -> Cell:
    """Read current tmux geometry without guessing pane order from coordinates."""
    def from_json(data):
        dimensions = [data[key] for key in ("w", "h", "x", "y")]
        if data["t"] == "p":
            return Cell(*dimensions, pane=data["I"])
        if data["t"] not in ("h", "v"):
            raise ValueError("Unknown layout cell type")
        return Cell(*dimensions, axis=data["t"], children=tuple(from_json(child) for child in data["c"]))

    def legacy(body: str, position: int) -> tuple[Cell, int]:
        match = re.match(r"(\d+)x(\d+),(\d+),(\d+)", body[position:])
        if match is None:
            raise ValueError("Invalid layout geometry")
        dimensions = tuple(map(int, match.groups()))
        position += match.end()
        if position < len(body) and body[position] in "{[":
            opening = body[position]
            closing = "}" if opening == "{" else "]"
            children = []
            position += 1
            while True:
                child, position = legacy(body, position)
                children.append(child)
                if position >= len(body):
                    raise ValueError("Unterminated layout cell")
                if body[position] == closing:
                    return Cell(*dimensions, axis="h" if opening == "{" else "v",
                                children=tuple(children)), position + 1
                if body[position] != ",":
                    raise ValueError("Invalid layout separator")
                position += 1
        match = re.match(r",(\d+)", body[position:])
        if match is None:
            raise ValueError("Missing layout pane ID")
        return Cell(*dimensions, pane="%" + match.group(1)), position + match.end()

    def validate(cell: Cell):
        if any(type(value) is not int for value in (cell.width, cell.height, cell.x, cell.y)):
            raise ValueError("Invalid layout dimensions")
        if cell.width < 1 or cell.height < 1 or cell.x < 0 or cell.y < 0:
            raise ValueError("Invalid layout bounds")
        if cell.pane is not None:
            if not isinstance(cell.pane, str) or not re.fullmatch(r"%\d+", cell.pane):
                raise ValueError("Invalid layout pane ID")
        elif not cell.children:
            raise ValueError("Empty layout container")
        for child in cell.children:
            if (child.x < cell.x or child.y < cell.y
                    or child.x + child.width > cell.x + cell.width
                    or child.y + child.height > cell.y + cell.height):
                raise ValueError("Layout child is outside its parent")
            validate(child)

    try:
        if text.lstrip().startswith("{"):
            data = json.loads(text)
            if data["V"] != 2:
                raise ValueError("Unsupported JSON layout version")
            result = from_json(data["L"])
        else:
            checksum, separator, body = text.partition(",")
            if not separator or not re.fullmatch(r"[0-9a-fA-F]{4}", checksum):
                raise ValueError("Missing layout checksum")
            if int(checksum, 16) != layout_checksum(body):
                raise ValueError("Incorrect layout checksum")
            result, position = legacy(body, 0)
            if position != len(body):
                raise ValueError("Trailing layout data")
        validate(result)
        if len(set(result.pane_ids())) != len(result.pane_ids()):
            raise ValueError("Duplicate layout pane IDs")
        return result
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError("Cannot read the current tmux layout") from error


def protect_sidebar(root: Cell, sidebar: str) -> Cell:
    """Group horizontal siblings under the content area, without moving any pane."""
    if root.axis != "h" or len(root.children) < 2 or root.children[0].pane != sidebar:
        raise ValueError("Sidebar is misplaced. Apply a workmux layout preset first.")
    left = root.children[0]
    if (left.x, left.y, left.height) != (0, 0, root.height) or left.width + 1 >= root.width:
        raise ValueError("Sidebar is misplaced. Apply a workmux layout preset first.")
    content = node(root.width - left.width - 1, root.height, left.width + 1, 0, "h", list(root.children[1:]))
    return Cell(root.width, root.height, root.x, root.y, axis="h", children=(left, content))


def content_needs_balance(root: Cell, pane: str, border_status: str = "off") -> bool:
    """Mirror tmux's ancestor search, stopping before the sidebar/content root."""
    def path(cell: Cell) -> list[Cell]:
        if cell.pane == pane:
            return [cell]
        for child in cell.children:
            found = path(child)
            if found:
                return [cell, *found]
        return []

    def border(cell: Cell) -> int:
        return int((border_status == "top" and cell.y == 0)
                   or (border_status == "bottom" and cell.y + cell.height == root.height))

    ancestors = path(root.children[1])
    if not ancestors:
        raise ValueError("Content pane is not in the current layout")
    for parent in reversed(ancestors[:-1]):
        count = len(parent.children)
        if count <= 1:
            continue
        size = parent.width if parent.axis == "h" else parent.height - border(parent)
        each, remainder = divmod(size - count + 1, count)
        if each < 1:
            continue
        for index, child in enumerate(parent.children):
            expected = each + (index < remainder) + (border(child) if parent.axis == "v" else 0)
            actual = child.width if parent.axis == "h" else child.height
            if actual != expected:
                return True
    return False


def split_sizes(length: int, count: int) -> list[int]:
    available = length - count + 1  # One cell for each internal border.
    if count < 1 or available < 2 * count:
        raise ValueError("Too little space for this layout. Enlarge the window or choose another layout.")
    base, remainder = divmod(available, count)
    return [base + (index >= count - remainder) for index in range(count)]


def node(width: int, height: int, x: int, y: int, axis: str, children: list[Cell]) -> Cell:
    if len(children) == 1:
        return children[0]
    return Cell(width, height, x, y, axis=axis, children=tuple(children))


def line(panes: list[str], width: int, height: int, x: int, y: int, axis: str) -> Cell:
    lengths = split_sizes(width if axis == "h" else height, len(panes))
    children = []
    offset = 0
    for pane, length in zip(panes, lengths):
        children.append(Cell(length if axis == "h" else width, height if axis == "h" else length,
                             x + offset if axis == "h" else x, y if axis == "h" else y + offset, pane))
        offset += length + 1
    return node(width, height, x, y, axis, children)


def main_size(option: str, length: int) -> int:
    available = length - 1
    try:
        requested = int(option[:-1]) * available // 100 if option.endswith("%") else int(option)
    except ValueError as error:
        raise ValueError(f"Invalid main pane size: {option}") from error
    if available < 4:
        raise ValueError("Too little space for the main pane layout. Enlarge the window.")
    return max(2, min(requested, available - 2))


def content_layout(preset: str, panes: list[str], width: int, height: int, x: int, y: int,
                   main_width: str, main_height: str) -> Cell:
    if width < 2 or height < 2 or not panes:
        raise ValueError("Too little space for content panes. Enlarge the window.")
    if len(panes) == 1:
        return Cell(width, height, x, y, panes[0])
    if preset in ("even-horizontal", "even-vertical"):
        return line(panes, width, height, x, y, "h" if preset == "even-horizontal" else "v")
    if preset.startswith("main-"):
        horizontal = preset.startswith("main-horizontal")
        mirrored = preset.endswith("-mirrored")
        length = height if horizontal else width
        primary = main_size(main_height if horizontal else main_width, length)
        secondary = length - primary - 1
        main_offset = secondary + 1 if mirrored else 0
        other_offset = 0 if mirrored else primary + 1
        main = Cell(width if horizontal else primary, primary if horizontal else height,
                    x if horizontal else x + main_offset, y + main_offset if horizontal else y, panes[0])
        others = line(panes[1:], width if horizontal else secondary, secondary if horizontal else height,
                      x if horizontal else x + other_offset, y + other_offset if horizontal else y,
                      "h" if horizontal else "v")
        children = [others, main] if mirrored else [main, others]
        return node(width, height, x, y, "v" if horizontal else "h", children)

    rows = columns = 1
    while rows * columns < len(panes):
        rows += 1
        if rows * columns < len(panes):
            columns += 1
    heights = split_sizes(height, rows)
    children = []
    offset = 0
    for index, row_height in enumerate(heights):
        row = panes[index * columns:(index + 1) * columns]
        children.append(line(row, width, row_height, x, y + offset, "h"))
        offset += row_height + 1
    return node(width, height, x, y, "v", children)


def workspace_layout(preset: str, sidebar: str, panes: list[str], width: int, height: int,
                     sidebar_width: int, main_width: str = "80", main_height: str = "24") -> Cell:
    if sidebar_width < 2:
        raise ValueError("The sidebar needs at least two columns.")
    if preset not in LAYOUTS:
        raise ValueError(f"Unknown layout: {preset}")
    if not panes or sidebar in panes or len(set(panes)) != len(panes):
        raise ValueError("A workspace layout needs one sidebar and distinct content panes.")
    content = content_layout(preset, panes, width - sidebar_width - 1, height, sidebar_width + 1, 0,
                             main_width, main_height)
    return Cell(width, height, 0, 0, axis="h", children=(Cell(sidebar_width, height, 0, 0, sidebar), content))
