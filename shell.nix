{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = [
    pkgs.curl
    pkgs.uv
    pkgs.zlib
    pkgs.gcc.cc.lib
  ];

  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
    pkgs.zlib
    pkgs.gcc.cc.lib
  ];

  shellHook = "";
}
