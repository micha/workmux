{
  description = "Workspace sidebar and content-only tiling for tmux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      packages = forAllSystems (system:
        let
          workmux = import ./nix/workmux.nix { pkgs = pkgsFor system; };
        in
        { default = workmux; inherit workmux; });

      devShells = forAllSystems (system: {
        default = import ./shell.nix { pkgs = pkgsFor system; };
        demo = import ./nix/demo.nix { pkgs = pkgsFor system; };
      });

      checks = forAllSystems (system: {
        package = self.packages.${system}.default;
        tests = import ./nix/checks.nix {
          pkgs = pkgsFor system;
          package = self.packages.${system}.default;
          source = self;
        };
      });
    };
}
