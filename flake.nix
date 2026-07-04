{
  description = "Local multi-repo code retrieval with tree-sitter and SQLite";

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
            pname = "local-code-context";
            version = "0.1.0";
            pyproject = true;

            src = self;

            build-system = [
              py.setuptools
            ];

            dependencies = [
              py."tree-sitter"
              py."tree-sitter-python"
              py."tree-sitter-rust"
              py.watchfiles
            ];

            pythonImportsCheck = [
              "local_code_context.syntax.detection"
              "local_code_context.indexing.indexer"
              "local_code_context.indexing.watcher"
              "local_code_context.mcp.context"
              "local_code_context.mcp.server"
              "local_code_context.syntax.legacy_python"
              "local_code_context.syntax.models"
              "local_code_context.syntax.indexer"
              "local_code_context.syntax.parsers"
              "local_code_context.syntax.extraction"
              "local_code_context.syntax.queries"
              "local_code_context.storage.schema"
              "local_code_context.storage.writer"
              "local_code_context.storage.reader"
              "local_code_context.storage.resolver"
            ];

            meta = {
              description = "Local multi-repo code retrieval with tree-sitter and SQLite";
              license = pkgs.lib.licenses.mit;
              mainProgram = "code-context-mcp";
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
          default = app "code-context-mcp";
          index = app "code-context-index";
          mcp = app "code-context-mcp";
          watch = app "code-context-watch";
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
          };
          commonPackages = [
            pkgs.curl
            pkgs.uv
            pkgs.zlib
            pkgs.gcc.cc.lib
          ];
          commonLibraryPath = pkgs.lib.makeLibraryPath [
            pkgs.zlib
            pkgs.gcc.cc.lib
          ];
        in
        {
          default = pkgs.mkShell {
            packages = commonPackages;
            LD_LIBRARY_PATH = commonLibraryPath;
            shellHook = "";
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
          cfg = config.services.local-code-context;
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
              "--debounce-seconds ${toString cfg.debounceSeconds}"
            ]
            ++ lib.optional (!cfg.initialIndex) "--no-initial-index"
          );
        in
        {
          options.services.local-code-context = {
            enable = lib.mkEnableOption "local multi-repo code context watcher";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.system}.default;
              defaultText = lib.literalExpression "inputs.local-code-context.packages.\${pkgs.system}.default";
              description = "local-code-context package to install and run.";
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
              default = "${config.home.homeDirectory}/.local/share/local-code-context/codebase_index";
              defaultText = lib.literalExpression ''"''${config.home.homeDirectory}/.local/share/local-code-context/codebase_index"'';
              description = "SQLite database directory.";
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
              example = { };
              description = "Additional environment variables for the watcher service.";
            };

            shellAliases = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Add shell aliases for managing the watcher user service.";
            };
          };

          config = lib.mkIf cfg.enable {
            assertions = [
              {
                assertion = cfg.repos != [ ] || cfg.workspaces != [ ];
                message = "services.local-code-context.repos or services.local-code-context.workspaces must contain at least one path.";
              }
            ];

            home.packages = [
              package
            ];

            systemd.user.services.local-code-context-watch = {
              Unit = {
                Description = "Local code context SQLite index watcher";
                After = [ "network-online.target" ];
              };

              Service = {
                Type = "simple";
                WorkingDirectory = cfg.workingDirectory;
                ExecStart = "${package}/bin/code-context-watch ${watchArgs}";
                Environment = lib.mapAttrsToList (name: value: "${name}=${value}") cfg.environment;
                Restart = "on-failure";
                RestartSec = "10s";
              };

              Install = lib.mkIf cfg.autoStart {
                WantedBy = [ "default.target" ];
              };
            };

            home.shellAliases = lib.mkIf cfg.shellAliases {
              code-context-up = "systemctl --user start local-code-context-watch";
              code-context-status = "systemctl --user status local-code-context-watch";
              code-context-down = "systemctl --user stop local-code-context-watch";
              code-context-logs = "journalctl --user -u local-code-context-watch -f";
            };
          };
        };
    };
}
