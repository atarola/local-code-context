{
  description = "Local multi-repo code retrieval with Chroma and Ollama";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
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
              "local_code_rag.mcp_server"
              "local_code_rag.query"
              "local_code_rag.watch_repos"
            ];

            meta = {
              description = "Local multi-repo code retrieval with Chroma and Ollama";
              license = pkgs.lib.licenses.mit;
              mainProgram = "code-rag-query";
            };
          };
        }
      );

      apps = forAllSystems (
        system:
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
          mcp = app "code-rag-mcp";
          query = app "code-rag-query";
          watch = app "code-rag-watch";
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.curl
              pkgs.ollama-cuda
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
        }
      );

      homeManagerModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.local-code-rag;
          package = cfg.package;
          repoArgs = lib.concatMapStringsSep " " (repo: "--repo ${lib.escapeShellArg repo}") cfg.repos;
          workspaceArgs = lib.concatMapStringsSep " " (
            workspace: "--workspace ${lib.escapeShellArg workspace}"
          ) cfg.workspaces;
          watchArgs = lib.concatStringsSep " " (
            [
              repoArgs
              workspaceArgs
              "--db ${lib.escapeShellArg cfg.db}"
              "--collection ${lib.escapeShellArg cfg.collection}"
              "--embed-model ${lib.escapeShellArg cfg.embedModel}"
              "--ollama-url ${lib.escapeShellArg cfg.ollamaUrl}"
              "--debounce-seconds ${toString cfg.debounceSeconds}"
            ]
            ++ lib.optional (!cfg.initialIndex) "--no-initial-index"
          );
          environmentList = lib.mapAttrsToList (name: value: "${name}=${value}") (
            cfg.environment
            // {
              OLLAMA_HOST = lib.removePrefix "http://" (lib.removePrefix "https://" cfg.ollamaUrl);
            }
          );
          systemctl = if cfg.ollamaServiceScope == "user" then "systemctl --user" else "sudo systemctl";
          systemctlStatus = if cfg.ollamaServiceScope == "user" then "systemctl --user" else "systemctl";
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

            workspaces = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
              example = [
                "/home/your-user/code"
              ];
              description = "Workspace directories. Each immediate child directory containing `.git` is watched and indexed as a separate repo.";
            };

            db = lib.mkOption {
              type = lib.types.str;
              default = "${config.home.homeDirectory}/.local/share/local-code-rag/codebase_index";
              defaultText = lib.literalExpression ''"''${config.home.homeDirectory}/.local/share/local-code-rag/codebase_index"'';
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

            ollamaPackage = lib.mkOption {
              type = lib.types.package;
              default = pkgs.ollama;
              defaultText = lib.literalExpression "pkgs.ollama";
              description = "Ollama package to install into the Home Manager profile.";
            };

            installOllama = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Install the Ollama CLI into the Home Manager profile.";
            };

            ollamaServiceName = lib.mkOption {
              type = lib.types.str;
              default = "ollama";
              description = "Systemd service name for the Ollama daemon.";
            };

            ollamaServiceScope = lib.mkOption {
              type = lib.types.enum [
                "system"
                "user"
              ];
              default = "system";
              description = "Whether aliases target a system or user Ollama service.";
            };

            manageOllama = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Create a Home Manager user service for `ollama serve`. Leave disabled if Ollama is configured elsewhere.";
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
              description = "Start the watcher when the user systemd manager starts.";
            };

            workingDirectory = lib.mkOption {
              type = lib.types.str;
              default = config.home.homeDirectory;
              defaultText = lib.literalExpression "config.home.homeDirectory";
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

            shellAliases = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Add shell aliases for managing Ollama and the Home Manager user service.";
            };
          };

          config = lib.mkIf cfg.enable {
            assertions = [
              {
                assertion = cfg.repos != [ ] || cfg.workspaces != [ ];
                message = "services.local-code-rag.repos or services.local-code-rag.workspaces must contain at least one path.";
              }
            ];

            home.packages = [
              package
            ]
            ++ lib.optional cfg.installOllama cfg.ollamaPackage;

            systemd.user.services.local-code-rag-watch = {
              Unit = {
                Description = "Local code RAG Chroma index watcher";
                After = [
                  "network-online.target"
                  "${cfg.ollamaServiceName}.service"
                ];
              };

              Service = {
                Type = "simple";
                WorkingDirectory = cfg.workingDirectory;
                ExecStart = "${package}/bin/code-rag-watch ${watchArgs}";
                Environment = environmentList;
                Restart = "on-failure";
                RestartSec = "10s";
              };

              Install = lib.mkIf cfg.autoStart {
                WantedBy = [ "default.target" ];
              };
            };

            systemd.user.services.${cfg.ollamaServiceName} = lib.mkIf cfg.manageOllama {
              Unit = {
                Description = "Ollama local model server";
                After = [ "network-online.target" ];
              };

              Service = {
                Type = "simple";
                ExecStart = "${cfg.ollamaPackage}/bin/ollama serve";
                Environment = environmentList;
                Restart = "on-failure";
                RestartSec = "10s";
              };

              Install = lib.mkIf cfg.autoStart {
                WantedBy = [ "default.target" ];
              };
            };

            home.shellAliases = lib.mkIf cfg.shellAliases {
              ollama-up = "${systemctl} start ${cfg.ollamaServiceName}";
              ollama-down = "${systemctl} stop ${cfg.ollamaServiceName}";
              ollama-status = "${systemctlStatus} status ${cfg.ollamaServiceName} --no-pager --lines=12";
              code-up = "${systemctl} start ${cfg.ollamaServiceName} && systemctl --user start local-code-rag-watch";
              code-down = "systemctl --user stop local-code-rag-watch; ${systemctl} stop ${cfg.ollamaServiceName}";
              code-status = "${systemctlStatus} status ${cfg.ollamaServiceName} --no-pager --lines=8; systemctl --user status local-code-rag-watch --no-pager --lines=8";
              code-rag-up = "systemctl --user start local-code-rag-watch";
              code-rag-status = "systemctl --user status local-code-rag-watch";
              code-rag-down = "systemctl --user stop local-code-rag-watch";
              code-rag-logs = "journalctl --user -u local-code-rag-watch -f";
            };
          };
        };
    };
}
