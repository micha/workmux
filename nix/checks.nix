{ pkgs, package, source }:

let
  python = pkgs.python3.withPackages (_: [ package ]);
in
pkgs.runCommand "workmux-tests" {
  nativeBuildInputs = [ python package pkgs.tmux pkgs.git pkgs.nodejs_24 ];
} ''
  export HOME="$TMPDIR/home"
  export SHELL="${pkgs.runtimeShell}"
  export TERM=xterm-256color
  mkdir -p "$HOME"

  # Run outside the checkout against the installed package and console script.
  # The suite uses private tmux sockets and temporary agent directories only.
  cp -R ${source}/tests tests
  python3 -c 'import pathlib, workmux; assert pathlib.Path(workmux.__file__).is_relative_to("${package}"), workmux.__file__'
  python3 -m unittest discover -s tests -v
  touch "$out"
''
