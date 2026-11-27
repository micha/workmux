# Shared application build for the flake and optional local Nix helpers.
{ pkgs, source ? ../. }:

let
  metadata = (builtins.fromTOML (builtins.readFile (source + "/pyproject.toml"))).project;
in
pkgs.python3Packages.buildPythonApplication {
  pname = metadata.name;
  version = metadata.version;
  pyproject = true;
  src = builtins.path {
    path = source;
    name = "workmux";
    # Copy only packaging inputs, including bundled agent extensions.
    filter = path: type:
      let
        relative = pkgs.lib.removePrefix ((toString source) + "/") (toString path);
        packagePath = relative == "workmux" || pkgs.lib.hasPrefix "workmux/" relative;
        resourcePath = pkgs.lib.hasPrefix "workmux/resources/" relative &&
          builtins.any (suffix: pkgs.lib.hasSuffix suffix relative) [ ".ts" ".mjs" ".json" ];
      in
      toString path == toString source ||
      relative == "pyproject.toml" || relative == "README.md" ||
      (packagePath && builtins.baseNameOf path != "__pycache__" &&
        (type == "directory" || pkgs.lib.hasSuffix ".py" relative || resourcePath));
  };
  build-system = [ pkgs.python3Packages.setuptools ];
  dependencies = import ./python-deps.nix { inherit pkgs; };
  # Pin private dependencies without leaking immutable bin directories into
  # tmux's session environment and the interactive shells it creates.
  postPatch = ''
    substituteInPlace workmux/core.py \
      --replace-fail 'TMUX_EXECUTABLE = "tmux"' 'TMUX_EXECUTABLE = "${pkgs.tmux}/bin/tmux"'
  '';
  dontWrapPythonPrograms = true;
  postFixup = ''
    # Reuse Nixpkgs' Python module-path injection, but not its PATH wrapper.
    buildPythonPath "$out ''${pythonPath[*]}"
    patchPythonScript "$out/bin/workmux"
    wrapProgram "$out/bin/workmux" --set PYTHONNOUSERSITE true
  '';
  pythonImportsCheck = [
    "workmux" "workmux.cli" "workmux.sidebar" "workmux.bindings"
    "workmux.integration" "workmux.plugins" "workmux.checkouts"
    "workmux.workspace_prompt"
  ];
  doInstallCheck = true;
  postInstallCheck = ''
    ${pkgs.python3}/bin/python3 ${./check-environment.py} \
      "$out/bin/workmux" "${pkgs.tmux}/bin/tmux" "${pkgs.runtimeShell}" \
      "${pkgs.coreutils}/bin/sleep" "${pkgs.coreutils}/bin/env"
    # Test actual packaged resources from outside the source tree, without user config.
    (
      agent_dir="$(mktemp -d)"
      trap 'rm -rf -- "$agent_dir"' EXIT
      cd "$agent_dir"
      "$out/bin/workmux" install --agent-dir "$agent_dir" | grep -F 'not installed'
      "$out/bin/workmux" install pi --agent-dir "$agent_dir"
      test -f "$agent_dir/extensions/workmux-notify.ts"
      "$out/bin/workmux" install --agent-dir "$agent_dir" | grep -F 'installed'
      "$out/bin/workmux" uninstall pi --agent-dir "$agent_dir"
      test ! -e "$agent_dir/extensions/workmux-notify.ts"
      for harness in opencode codex claude; do
        "$out/bin/workmux" install "$harness" --agent-dir "$agent_dir/$harness"
        "$out/bin/workmux" install --agent-dir "$agent_dir/$harness" | grep -F 'installed'
        case "$harness" in
          opencode) test -f "$agent_dir/$harness/plugins/workmux-notify.js" ;;
          codex) test -f "$agent_dir/$harness/hooks.json" ;;
          claude)
            test -f "$agent_dir/$harness/skills/workmux/.claude-plugin/plugin.json"
            test -f "$agent_dir/$harness/skills/workmux/hooks/hooks.json" ;;
        esac
        "$out/bin/workmux" uninstall "$harness" --agent-dir "$agent_dir/$harness"
      done
    )
  '';
}
