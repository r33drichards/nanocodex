{
  description = "nanocodex — locked-down codex app-server with per-thread mcp-v8 sandboxes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # mcp-v8: v0.18.1 (--config, OAuth fetch headers) plus the stdio-cluster and
    # --session-id patches needed for the per-thread learner topology. Same
    # Cargo.lock as v0.18.1, so the flake's vendor hash still applies.
    mcp-js.url = "github:r33drichards/mcp-js/claude/stdio-cluster-learner";
    # codex fork: source only — its in-tree flake builds the whole workspace,
    # which can't vendor hermetically (libwebrtc git submodule, no v8
    # archive). We build just the codex-app-server binary ourselves; that
    # bin's dependency graph avoids libwebrtc entirely.
    codex-src = {
      url = "github:r33drichards/codex/claude/kodex-mcp-js-library-37218h";
      flake = false;
    };
    # Fresh nixpkgs snapshot for the standalone images' sidecar tooling
    # (AG-UI bridge python deps, node/next builds, supervisor): fastmcp and
    # betterproto 2.0.0b7 only exist in newer nixpkgs than the pin above,
    # and bumping `nixpkgs` itself would rebuild both Rust workspaces.
    nixpkgs-tools.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, rust-overlay, mcp-js, codex-src, nixpkgs-tools }:
    let
      linuxSystems = [ "x86_64-linux" "aarch64-linux" ];
      forSystems = systems: f: nixpkgs.lib.genAttrs systems f;

      # Prebuilt V8 static lib for the codex build (codex-code-mode -> v8).
      # The v8 crate's build.rs downloads this when RUSTY_V8_ARCHIVE is unset,
      # which the nix sandbox forbids. Version must match codex-rs/Cargo.lock.
      rustyV8 = {
        version = "149.2.0";
        hashes = {
          x86_64-linux = "sha256-iu2YY323533Iv7i7R1nsW95HLQv3lD9Y4OYqNQlFxVk=";
          aarch64-linux = "sha256-+XdRJ8pk3MSjZi0BpSGizvuluY+DOUOog9hHc7Kv88U=";
        };
        targets = {
          x86_64-linux = "x86_64-unknown-linux-gnu";
          aarch64-linux = "aarch64-unknown-linux-gnu";
        };
      };

      packagesFor = system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ rust-overlay.overlays.default ];
          };

          rustToolchain = pkgs.rust-bin.stable.latest.minimal;
          rustPlatform = pkgs.makeRustPlatform {
            cargo = rustToolchain;
            rustc = rustToolchain;
          };

          rustyV8Archive = pkgs.fetchurl {
            url = "https://github.com/denoland/rusty_v8/releases/download/v${rustyV8.version}/librusty_v8_release_${rustyV8.targets.${system}}.a.gz";
            hash = rustyV8.hashes.${system};
          };

          codex-app-server = rustPlatform.buildRustPackage {
            pname = "codex-app-server";
            version = "0.0.0-nanocodex";
            src = "${codex-src}/codex-rs";

            cargoLock = {
              lockFile = "${codex-src}/codex-rs/Cargo.lock";
              # Fetch git deps (ratatui/crossterm/tungstenite forks, ...) by
              # locked rev instead of enumerating outputHashes. Submodules are
              # not fetched — fine, since the only crate that needs one
              # (libwebrtc/yuv-sys) is outside codex-app-server's graph and is
              # never compiled.
              allowBuiltinFetchGit = true;
            };

            cargoBuildFlags = [ "-p" "codex-app-server" "--bin" "codex-app-server" ];
            doCheck = false;

            nativeBuildInputs = with pkgs; [
              cmake
              llvmPackages.clang
              pkg-config
              perl
              python3
            ];
            buildInputs = with pkgs; [ openssl ]
              ++ lib.optionals stdenv.isLinux [ libcap ];

            env = {
              LIBCLANG_PATH = "${pkgs.llvmPackages.libclang.lib}/lib";
              RUSTY_V8_ARCHIVE = rustyV8Archive;
              PKG_CONFIG_PATH = pkgs.lib.makeSearchPathOutput "dev" "lib/pkgconfig"
                ([ pkgs.openssl ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.libcap ]);
            };

            meta.mainProgram = "codex-app-server";
          };

          mcpV8 = mcp-js.packages.${system}.default;

          # Filesystem layout the runtime expects: the client package points
          # threads at /usr/local/bin/mcp-v8 and /app/policies/policies.json.
          rootfs = pkgs.runCommand "nanocodex-rootfs" { } ''
            mkdir -p $out/usr/local/bin $out/codex-home $out/app/policies $out/tmp $out/run
            ln -s ${codex-app-server}/bin/codex-app-server $out/usr/local/bin/codex-app-server
            ln -s ${mcpV8}/bin/server $out/usr/local/bin/mcp-v8
            cp ${./codex-home/config.toml} $out/codex-home/config.toml
            cp ${./policies/policies.json} $out/app/policies/policies.json
            cp ${./policies/fetch.rego} $out/app/policies/fetch.rego
          '';

          # ── Standalone deployment mode ──────────────────────────────────
          # One container = the compose topology collapsed: supervisord runs
          # the mcp-v8 server and codex-app-server (plus, per variant, the
          # AG-UI bridge, the Next.js frontend and the Slack bot) side by
          # side, and MinIO is replaced by directory-backed stores
          # (--heap-store dir / --fs-store dir) on /data.
          #
          # mcp-v8 refuses node-local dir stores in cluster mode (fs blobs
          # must be on shared storage), so the standalone mcp-v8 runs as a
          # plain single-node stateful server — no --cluster-port/--node-id,
          # and per-thread sandboxes should use dir-store args
          # (--heap-store dir --heap-dir ... --fs-store dir --fs-dir ...
          # --session-id ... --session-db-path <unique>) instead of
          # --join/--join-as-learner — which is exactly what the AG-UI
          # bridge's sandbox.py already emits. All processes share the
          # container filesystem, so the content-addressed heap/fs blob dirs
          # are safely shared; only the sled session DBs must stay per-process.
          #
          # Persist by mounting volumes at /data, /tmp (per-thread agui
          # sandbox state), /codex-home/sqlite and /codex-home/sessions;
          # provide the ws token at /run/secrets/ws_token.
          pkgsTools = import nixpkgs-tools { inherit system; };
          pyPkgs = pkgsTools.python3Packages;
          lib = pkgs.lib;

          # ag-ui-protocol is not in nixpkgs; pure-python wheel from PyPI.
          ag-ui-protocol = pyPkgs.buildPythonPackage rec {
            pname = "ag_ui_protocol";
            version = "0.1.19";
            format = "wheel";
            src = pyPkgs.fetchPypi {
              inherit pname version;
              format = "wheel";
              dist = "py3";
              python = "py3";
              abi = "none";
              platform = "any";
              hash = "sha256-iYhDsUENN4gk2gxqd2SGKIucWChonQv1YxGIaON/OQ8=";
            };
            dependencies = [ pyPkgs.pydantic ];
          };

          # The client package (core + CLI + FastAPI AG-UI bridge + FastMCP
          # server); the standalone images run its bridge via uvicorn.
          nanocodexClient = pyPkgs.buildPythonPackage {
            pname = "nanocodex-client";
            version = "0.1.0";
            pyproject = true;
            src = ./client;
            build-system = [ pyPkgs.hatchling ];
            dependencies = with pyPkgs; [
              websockets
              typer
              fastapi
              uvicorn
              fastmcp
              tomli-w
              betterproto
              ag-ui-protocol
            ];
            # pyproject pins betterproto==2.0.0b7 and fastmcp>=2.0; accept
            # what nixpkgs carries (b7 today, fastmcp 3.x — only the
            # `nanocodex mcp` frontend touches fastmcp, not the bridge).
            pythonRelaxDeps = [ "betterproto" "fastmcp" ];
            doCheck = false;
          };

          bridgeEnv = pkgsTools.python3.withPackages (ps: [ nanocodexClient ]);

          # Slack bot: node22 + tsx, no build step — ship source + node_modules.
          slackbotApp = pkgsTools.buildNpmPackage {
            pname = "nanocodex-slackbot";
            version = "0.1.0";
            src = ./slackbot;
            npmDepsHash = "sha256-4qqu93eX4C9VoHve9HiLY1kWUxxiYSBDAbS8lEaAIYE=";
            dontNpmBuild = true;
            installPhase = ''
              runHook preInstall
              mkdir -p $out/lib/slackbot
              cp -r node_modules index.ts threads.ts tsconfig.json package.json $out/lib/slackbot/
              runHook postInstall
            '';
          };

          # Next.js frontend, `next build` at nix build time. Built with
          # NEXT_PUBLIC_BRIDGE_URL="" (inlined by next build): the browser
          # makes same-origin /agui/... calls, which `next start` proxies to
          # the in-container bridge via the BRIDGE_PROXY_TARGET rewrite
          # (runtime config, set on frontendProgram below) — so one public
          # port (3000) serves UI + bridge on any host.
          frontendApp = pkgsTools.buildNpmPackage {
            pname = "nanocodex-frontend";
            version = "0.2.0";
            src = ./frontend;
            npmDepsHash = "sha256-4b3vngjnIOiCWZaTJFheCkYTbN3QqLDs8lFQjyNiqCg=";
            env = {
              NEXT_TELEMETRY_DISABLED = "1";
              NEXT_PUBLIC_BRIDGE_URL = "";
            };
            installPhase = ''
              runHook preInstall
              rm -rf .next/cache
              mkdir -p $out/lib/frontend
              cp -r .next node_modules package.json next.config.js $out/lib/frontend/
              runHook postInstall
            '';
          };

          mkSupervisordConf = programs: pkgs.writeTextDir "etc/supervisord.conf" (''
            [supervisord]
            nodaemon=true
            logfile=/dev/null
            logfile_maxbytes=0
          '' + lib.concatMapStrings (p: ''

            [program:${p.name}]
            priority=${toString p.priority}
            command=${p.command}
            ${lib.optionalString (p ? directory) "directory=${p.directory}"}
            ${lib.optionalString (p ? environment) "environment=${p.environment}"}
            autorestart=true
            stdout_logfile=/dev/stdout
            stdout_logfile_maxbytes=0
            stderr_logfile=/dev/stderr
            stderr_logfile_maxbytes=0
          '') programs);

          mcpV8Program = {
            name = "mcp-v8";
            priority = 10;
            command = "/usr/local/bin/mcp-v8 --http-port 8080 --bind-host 0.0.0.0 --heap-store dir --heap-dir /data/heaps --fs-store dir --fs-dir /data/fs --session-db-path /data/sessions --policies-json /app/policies/policies.json";
          };
          # Provider/model are runtime env config on the supervisord images:
          # python-supervisor expands %(ENV_*)s at startup, and codex parses
          # a non-TOML `-c` value as a literal string. Defaults (azure /
          # gpt-5.4, matching config.toml) are baked into the image Env below;
          # override with `docker run -e NANOCODEX_MODEL_PROVIDER=ollama-cloud
          # -e NANOCODEX_MODEL=glm-5.2` (provider must exist in config.toml).
          codexProgram = {
            name = "codex";
            priority = 20;
            command = "/usr/local/bin/codex-app-server --listen ws://0.0.0.0:4500 --ws-auth capability-token --ws-token-file /run/secrets/ws_token -c model_provider=%(ENV_NANOCODEX_MODEL_PROVIDER)s -c model=%(ENV_NANOCODEX_MODEL)s";
          };
          # directory=/app so the client's walk-up token search finds the
          # baked /app/secrets/ws-token -> /run/secrets/ws_token symlink.
          # NANOCODEX_SANDBOX is inherited from the container env, so the
          # languages overlay image just sets ENV NANOCODEX_SANDBOX=languages.
          bridgeProgram = {
            name = "bridge";
            priority = 30;
            command = "${bridgeEnv}/bin/uvicorn nanocodex_client.agui.app:app --host 0.0.0.0 --port 8130";
            directory = "/app";
            environment = ''NANOCODEX_URL="ws://127.0.0.1:4500",AGUI_BINDINGS_PATH="/data/agui/bindings.json"'';
          };
          frontendProgram = {
            name = "frontend";
            priority = 40;
            command = "/bin/node /opt/frontend/node_modules/next/dist/bin/next start --hostname 0.0.0.0 --port 3000";
            directory = "/opt/frontend";
            environment = ''BRIDGE_PROXY_TARGET="http://127.0.0.1:8130"'';
          };
          slackbotProgram = {
            name = "slackbot";
            priority = 40;
            command = "/bin/node /opt/slackbot/node_modules/tsx/dist/cli.mjs index.ts";
            directory = "/opt/slackbot";
            environment = ''AGENT_URL="http://127.0.0.1:8130/agui"'';
          };

          # /opt copies are real files (not store symlinks) so next can write
          # .next/cache and paths stay stable for the supervisord commands.
          frontendFakeRoot = ''
            mkdir -p opt
            cp -r ${frontendApp}/lib/frontend opt/frontend
            chmod -R u+w opt/frontend
          '';
          slackbotFakeRoot = ''
            mkdir -p opt
            cp -r ${slackbotApp}/lib/slackbot opt/slackbot
            chmod -R u+w opt/slackbot
          '';
          bridgeFakeRoot = ''
            mkdir -p app/secrets data/agui
            ln -sf /run/secrets/ws_token app/secrets/ws-token
          '';

          mkStandaloneImage = { name, programs, extraContents ? [ ], basePorts ? [ 4500 8080 ], extraPorts ? [ ], extraFakeRoot ? "", extraEnv ? [ ] }:
            pkgs.dockerTools.streamLayeredImage {
              inherit name;
              tag = "latest";
              contents = [
                rootfs
                (mkSupervisordConf programs)
                pyPkgs.supervisor
                pkgs.cacert
                pkgs.bashInteractive
                pkgs.coreutils
                pkgs.curl
              ] ++ extraContents;
              fakeRootCommands = ''
                chmod 1777 tmp
                mkdir -p data/heaps data/fs data/sessions run/secrets
              '' + extraFakeRoot;
              config = {
                Entrypoint = [ "/bin/supervisord" "-c" "/etc/supervisord.conf" ];
                Env = [
                  "CODEX_HOME=/codex-home"
                  "PATH=/usr/local/bin:/bin"
                  "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
                  "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
                  # Consumed by codexProgram's %(ENV_*)s -c overrides; must
                  # exist or supervisord fails conf expansion at startup.
                  "NANOCODEX_MODEL_PROVIDER=azure"
                  "NANOCODEX_MODEL=gpt-5.4"
                ] ++ extraEnv;
                ExposedPorts = builtins.listToAttrs
                  (map (p: { name = "${toString p}/tcp"; value = { }; }) (basePorts ++ extraPorts));
                Volumes."/data" = { };
              };
            };

          standaloneImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-standalone";
            programs = [ mcpV8Program codexProgram bridgeProgram ];
            extraPorts = [ 8130 ];
            extraFakeRoot = bridgeFakeRoot;
          };
          standaloneFrontendImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-standalone-frontend";
            programs = [ mcpV8Program codexProgram bridgeProgram frontendProgram ];
            extraContents = [ pkgsTools.nodejs_22 ];
            extraPorts = [ 8130 3000 ];
            extraFakeRoot = bridgeFakeRoot + frontendFakeRoot;
          };
          standaloneSlackImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-standalone-slack";
            programs = [ mcpV8Program codexProgram bridgeProgram slackbotProgram ];
            extraContents = [ pkgsTools.nodejs_22 ];
            extraPorts = [ 8130 ];
            extraFakeRoot = bridgeFakeRoot + slackbotFakeRoot;
          };
          # No local mcp-v8: the bridge declares each thread's sandbox as a
          # streamable-HTTP mcp server on a REMOTE mcp-v8 instance
          # (NANOCODEX_SANDBOX=remote baked; set NANOCODEX_MCP_V8_URL at run,
          # e.g. http://mcp-v8-host:8080/mcp — nanocodex-standalone's :8080
          # works as that remote). Threads stay stateful+isolated via the
          # X-MCP-Session-Id header, see client agui/sandbox.py.
          slackRemoteImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-slack-remote";
            programs = [ codexProgram bridgeProgram slackbotProgram ];
            extraContents = [ pkgsTools.nodejs_22 ];
            basePorts = [ 4500 ];
            extraPorts = [ 8130 ];
            extraFakeRoot = bridgeFakeRoot + slackbotFakeRoot;
            extraEnv = [ "NANOCODEX_SANDBOX=remote" ];
          };
          standaloneFullImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-standalone-full";
            programs = [ mcpV8Program codexProgram bridgeProgram frontendProgram slackbotProgram ];
            extraContents = [ pkgsTools.nodejs_22 ];
            extraPorts = [ 8130 3000 ];
            extraFakeRoot = bridgeFakeRoot + frontendFakeRoot + slackbotFakeRoot;
          };

          image = pkgs.dockerTools.streamLayeredImage {
            name = "ghcr.io/r33drichards/nanocodex";
            tag = "latest";
            contents = [
              rootfs
              pkgs.cacert
              pkgs.bashInteractive
              pkgs.coreutils
              pkgs.curl
            ];
            fakeRootCommands = ''
              chmod 1777 tmp
            '';
            config = {
              Entrypoint = [ "/usr/local/bin/codex-app-server" ];
              Cmd = [
                "--listen" "ws://0.0.0.0:4500"
                "--ws-auth" "capability-token"
                "--ws-token-file" "/run/secrets/ws_token"
              ];
              Env = [
                "CODEX_HOME=/codex-home"
                "PATH=/usr/local/bin:/bin"
                "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
                "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
              ];
              ExposedPorts."4500/tcp" = { };
            };
          };
        in
        {
          inherit codex-app-server image;
          mcp-v8 = mcpV8;
          bridge = bridgeEnv;
          slackbot = slackbotApp;
          frontend = frontendApp;
          standalone = standaloneImage;
          standalone-frontend = standaloneFrontendImage;
          standalone-slack = standaloneSlackImage;
          standalone-full = standaloneFullImage;
          slack-remote = slackRemoteImage;
          default = image;
        };
    in
    {
      packages = forSystems linuxSystems packagesFor;
    };
}
