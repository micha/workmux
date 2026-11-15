{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python3.withPackages (_: import ./nix/python-deps.nix { inherit pkgs; });
in
pkgs.mkShell {
  packages = [ python pkgs.tmux pkgs.pipx pkgs.git pkgs.nodejs_24 ];
}
