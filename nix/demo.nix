{ pkgs }:

pkgs.mkShell {
  inputsFrom = [ (import ../shell.nix { inherit pkgs; }) ];
  packages = [ pkgs.vhs pkgs.ttyd pkgs.ffmpeg pkgs.chromium pkgs.dejavu_fonts ];
  # Only VHS's local renderer uses this; never use it for untrusted web pages.
  VHS_NO_SANDBOX = "1";
  FONTCONFIG_FILE = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };
}
