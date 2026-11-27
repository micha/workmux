{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python3.withPackages (ps:
    (import ./nix/python-deps.nix { inherit pkgs; }) ++ [ ps.argcomplete ]);
in
pkgs.mkShell {
  packages = [ python pkgs.tmux pkgs.pipx pkgs.git pkgs.nodejs_24 pkgs.bashInteractive ];
}
