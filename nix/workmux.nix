# Shared build definition for machine-local Nix installation helpers.
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
        resourcePath = relative == "workmux/resources/pi-notify.ts";
      in
      toString path == toString source ||
      relative == "pyproject.toml" || relative == "README.md" ||
      (packagePath && builtins.baseNameOf path != "__pycache__" &&
        (type == "directory" || pkgs.lib.hasSuffix ".py" relative || resourcePath));
  };
  build-system = [ pkgs.python3Packages.setuptools ];
  dependencies = import ./python-deps.nix { inherit pkgs; };
  makeWrapperArgs = [ "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.tmux ]) ];
  pythonImportsCheck = [
    "workmux" "workmux.cli" "workmux.sidebar" "workmux.bindings"
    "workmux.integration" "workmux.plugins"
  ];
  doInstallCheck = true;
  postInstallCheck = ''
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
    )
  '';
}
