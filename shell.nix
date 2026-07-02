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

    ollama-up() {
      sudo systemctl start ollama
    }

    ollama-down() {
      sudo systemctl stop ollama
    }

    ollama-status() {
      if systemctl is-active --quiet ollama; then
        echo "ollama.service is running"
      else
        echo "ollama.service is not running"
      fi
      echo
      systemctl status ollama --no-pager --lines=12 || true
      echo
      echo "Recent logs:"
      if journalctl -u ollama -n 40 --no-pager >/dev/null 2>&1; then
        journalctl -u ollama -n 40 --no-pager
      else
        sudo journalctl -u ollama -n 40 --no-pager
      fi
    }

    echo "Ollama helpers: ollama-up, ollama-status, ollama-down"
  '';
}
