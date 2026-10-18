{ pkgs }:

let
  pythonPackages = pkgs.python3Packages;
  tmuxGrammar = pythonPackages.buildPythonPackage {
    pname = "tree-sitter-tmux";
    version = "0.1.5";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "tree_sitter_tmux";
      version = "0.1.5";
      sha256 = "53b09c470486d1d7a4039a319a3dc5d8dc3a039c42b0191c68d18224d672e33e";
    };
    build-system = [ pythonPackages.setuptools pythonPackages.wheel ];
    pythonImportsCheck = [ "tree_sitter_tmux" ];
  };
in
[ pythonPackages.tree-sitter tmuxGrammar ]
