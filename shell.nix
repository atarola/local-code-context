{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = [
    pkgs.curl
    pkgs.ollama
    pkgs.uv
    pkgs.zlib
    pkgs.gcc.cc.lib
  ];

  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
    pkgs.zlib
    pkgs.gcc.cc.lib
  ];

  shellHook = ''
    export OLLAMA_HOST=''${OLLAMA_HOST:-127.0.0.1:11434}
    export LOCAL_CODE_RAG_OLLAMA_URL="http://$OLLAMA_HOST"
  '';
}
