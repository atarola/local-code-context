{
  description = "Local multi-repo code retrieval with Chroma and Ollama";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          py = pkgs.python313Packages;
        in
        {
          default = py.buildPythonApplication {
            pname = "local-code-rag";
            version = "0.1.0";
            pyproject = true;

            src = self;

            build-system = [
              py.setuptools
            ];

            dependencies = [
              py.chromadb
              py.requests
              py.watchfiles
            ];

            pythonImportsCheck = [
              "local_code_rag.index_repos"
              "local_code_rag.query"
              "local_code_rag.watch_repos"
            ];

            meta = {
              description = "Local multi-repo code retrieval with Chroma and Ollama";
              license = pkgs.lib.licenses.mit;
              mainProgram = "code-rag-query";
            };
          };
        });

      apps = forAllSystems (system:
        let
          package = self.packages.${system}.default;
          app = program: {
            type = "app";
            program = "${package}/bin/${program}";
          };
        in
        {
          default = app "code-rag-query";
          index = app "code-rag-index";
          query = app "code-rag-query";
          watch = app "code-rag-watch";
        });

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
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
          };
        });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.local-code-rag;
          package = cfg.package;
          repoArgs = lib.concatMapStringsSep " " (repo: "--repo ${lib.escapeShellArg repo}") cfg.repos;
          watchArgs = lib.concatStringsSep " " ([
            repoArgs
            "--db ${lib.escapeShellArg cfg.db}"
            "--collection ${lib.escapeShellArg cfg.collection}"
            "--embed-model ${lib.escapeShellArg cfg.embedModel}"
            "--ollama-url ${lib.escapeShellArg cfg.ollamaUrl}"
            "--debounce-seconds ${toString cfg.debounceSeconds}"
          ] ++ lib.optional (!cfg.initialIndex) "--no-initial-index");
        in
        {
          options.services.local-code-rag = {
            enable = lib.mkEnableOption "local multi-repo code RAG watcher";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              defaultText = lib.literalExpression "inputs.local-code-rag.packages.\${pkgs.system}.default";
              description = "local-code-rag package to install and run.";
            };

            repos = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
              example = [
                "/home/your-user/code/service-a"
                "/home/your-user/code/service-b"
              ];
              description = "Repository directories to watch and index.";
            };

            db = lib.mkOption {
              type = lib.types.str;
              default = "/var/lib/local-code-rag/codebase_index";
              example = "/home/your-user/.local/share/local-code-rag/codebase_index";
              description = "Persistent Chroma database directory.";
            };

            collection = lib.mkOption {
              type = lib.types.str;
              default = "code_chunks";
              description = "Chroma collection name.";
            };

            embedModel = lib.mkOption {
              type = lib.types.str;
              default = "nomic-embed-text";
              description = "Ollama embedding model.";
            };

            ollamaUrl = lib.mkOption {
              type = lib.types.str;
              default = "http://127.0.0.1:11434";
              description = "Ollama HTTP API base URL.";
            };

            debounceSeconds = lib.mkOption {
              type = lib.types.number;
              default = 5;
              description = "Seconds to debounce file changes before refreshing the index.";
            };

            initialIndex = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Run an index refresh immediately when the watcher starts.";
            };

            autoStart = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Start the watcher at boot. Disabled by default so Ollama is only used when explicitly started.";
            };

            user = lib.mkOption {
              type = lib.types.str;
              default = "root";
              example = "your-user";
              description = "User that runs the watcher service.";
            };

            group = lib.mkOption {
              type = lib.types.str;
              default = "root";
              example = "users";
              description = "Group that runs the watcher service.";
            };

            workingDirectory = lib.mkOption {
              type = lib.types.str;
              default = "/";
              example = "/home/your-user/code";
              description = "Working directory for the watcher service.";
            };

            environment = lib.mkOption {
              type = lib.types.attrsOf lib.types.str;
              default = { };
              example = {
                OLLAMA_HOST = "127.0.0.1:11434";
              };
              description = "Additional environment variables for the watcher service.";
            };
          };

          config = lib.mkIf cfg.enable {
            assertions = [
              {
                assertion = cfg.repos != [ ];
                message = "services.local-code-rag.repos must contain at least one repo path.";
              }
            ];

            environment.systemPackages = [
              package
            ];

            systemd.services.local-code-rag-watch = {
              description = "Local code RAG Chroma index watcher";
              after = [
                "network-online.target"
                "ollama.service"
              ];
              wants = [
                "network-online.target"
              ];
              wantedBy = lib.optional cfg.autoStart "multi-user.target";
              environment = cfg.environment // {
                OLLAMA_HOST = lib.removePrefix "http://" (lib.removePrefix "https://" cfg.ollamaUrl);
              };

              serviceConfig = {
                Type = "simple";
                User = cfg.user;
                Group = cfg.group;
                WorkingDirectory = cfg.workingDirectory;
                ExecStart = "${package}/bin/code-rag-watch ${watchArgs}";
                Restart = "on-failure";
                RestartSec = "10s";
              };
            };
          };
        };
    };
}
