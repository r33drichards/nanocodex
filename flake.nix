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

          # ── /opt/languages: the WASM language engines + bootstrap.js ────
          # Pure-nix replication of the former Dockerfile.languages gen
          # stage (fetch-vendor.sh + build-bootstrap.mjs): vendor the pinned
          # third-party engines, generate bootstrap.js, and lay out
          # /opt/languages exactly as the sandbox presets expect.
          languagesVendor = rec {
            babel = pkgs.fetchurl {
              url = "https://registry.npmjs.org/@babel/standalone/-/standalone-7.26.4.tgz";
              hash = "sha256-Fgtct5rVYR9ko0lhONQ6iHx56uvnIiU3jBrJS9DTrDY=";
            };
            react = pkgs.fetchurl {
              url = "https://registry.npmjs.org/react/-/react-18.3.1.tgz";
              hash = "sha256-jZvtAaZy5+rzh5QteBrUfGpDCJowoDBgYPn9WseHA0c=";
            };
            react-dom = pkgs.fetchurl {
              url = "https://registry.npmjs.org/react-dom/-/react-dom-18.3.1.tgz";
              hash = "sha256-Ax1EJ6mfLz9srBi7vzCFlKZ+T0fRD0iW/exEZQWwQOA=";
            };
            marked = pkgs.fetchurl {
              url = "https://registry.npmjs.org/marked/-/marked-11.1.1.tgz";
              hash = "sha256-wp3d737tQNI3vcbniGmFyxlrI09sk2wur11s0rwVObU=";
            };
            mermaid = pkgs.fetchurl {
              url = "https://registry.npmjs.org/mermaid/-/mermaid-9.4.3.tgz";
              hash = "sha256-S2DQ3/g8zvtK5IKwDzDWQTibhhpnzfUcy4t+7RnPiw4=";
            };
            minizinc = pkgs.fetchurl {
              url = "https://registry.npmjs.org/minizinc/-/minizinc-4.4.6.tgz";
              hash = "sha256-B699O16733wS0Qlec7DTkemytVf5cKouUtlW1flnSUc=";
            };
            wasmoon = pkgs.fetchurl {
              url = "https://registry.npmjs.org/wasmoon/-/wasmoon-1.16.0.tgz";
              hash = "sha256-egfLbTmvEJEfq9UHQIM0d7GLR7bwMUjxUhjt9HTIr1Y=";
            };
            acadlisp-js = pkgs.fetchurl {
              url = "https://raw.githubusercontent.com/holg/acadlisp/aa555bbe87f950ceceb8cb587c0735bc69aa2f23/dist/acadlisp-86aa022a7657981b.js";
              hash = "sha256-b9mf9w9hd/+oMipRaXABTCVA7Dg9R+pJEy50SohiOok=";
            };
            acadlisp-wasm = pkgs.fetchurl {
              url = "https://raw.githubusercontent.com/holg/acadlisp/aa555bbe87f950ceceb8cb587c0735bc69aa2f23/dist/acadlisp-86aa022a7657981b_bg.wasm";
              hash = "sha256-MiAhWV3GJtDhyCtOJRE7+hfLgMWQfJXZAN8gxUKmM/g=";
            };
          };

          # ── /opt/languages/codebases: read-only reference source trees ──
          # Full upstream source of four ComputerCraft / CraftOS projects,
          # baked read-only into the languages image so the agent can grep and
          # read the real implementations behind the CC:Tweaked API (the mount
          # is already readable via filesystem.rego — no policy change needed).
          #
          # Pinned by commit; submodules are NOT fetched (their gitlink paths
          # remain empty dirs — the four listed repos are the reference, not
          # their sub-projects). `desc` also feeds codebases/README.md, so the
          # metadata lives here once. To bump a rev: change `rev`, set
          # `hash = lib.fakeHash`, rebuild, and paste the reported hash back.
          languagesCodebasesMeta = [
            {
              dir = "reconnected-docs";
              url = "https://github.com/ReconnectedCC/docs.git";
              rev = "95e195376448f043cc2a970eed42515cf038d845";
              hash = "sha256-VA5+NXZw6z4G7Segwti2gHNbDU6FuEbaxBa2+7ouhLs=";
              desc = "ReconnectedCC/docs — docs for the ReconnectedCC server (CC:Tweaked APIs, guides, ReconnectedChat, custom peripherals).";
            }
            {
              dir = "re-plethora";
              url = "https://github.com/ReconnectedCC/Re-Plethora.git";
              rev = "88e02f1dafdcc379e3f46768ea1ea89bf647510c";
              hash = "sha256-D+64pU0S9PL7EaD06rwk/G0PSIn+6wn8jPNvmlfu4v4=";
              desc = "ReconnectedCC/Re-Plethora — the Plethora peripherals/neural-interface mod (Java); source for the modules & methods it exposes to CC computers.";
            }
            {
              dir = "craftos2";
              url = "https://github.com/MCJack123/craftos2.git";
              rev = "2844cba6184e7e2590910d6c2c33697b9b5ff9fd";
              hash = "sha256-bV/KfVAXthHQnRs+MP5odIX6hYJlMf76XazhtDmmw2Q=";
              desc = "MCJack123/craftos2 — CraftOS-PC, the CC:Tweaked emulator (C++); reference for API/peripheral behaviour (this is what the `craftos` engine is built from).";
            }
            {
              dir = "cobalt";
              url = "https://github.com/cc-tweaked/Cobalt.git";
              rev = "5df90f08eefb0faf10ad4cadb47b9e59d661bf88";
              hash = "sha256-sjxISeWDGO2c+58GqfjNgb703rNl2Zo2GmWDh04huHc=";
              desc = "cc-tweaked/Cobalt — the Lua VM (Java) CC:Tweaked runs on; ground truth for Lua-compat quirks (5.1 + selected 5.2/5.3).";
            }
          ];
          languagesCodebases = map (c: c // {
            src = pkgs.fetchgit {
              inherit (c) url rev hash;
              fetchSubmodules = false;
            };
          }) languagesCodebasesMeta;
          languagesCodebasesCopy = lib.concatMapStrings (c: ''
            cp -r ${c.src} $out/opt/languages/codebases/${c.dir}
          '') languagesCodebases;
          languagesCodebasesReadme = pkgs.writeText "codebases-README.md" (''
            # Reference codebases (read-only)

            Full upstream source of the ComputerCraft / CraftOS projects below,
            baked into the languages image so the agent can read and grep the
            real implementations behind the CC:Tweaked API. This is a read-only
            mount — do not try to edit it (write scratch work under /work).
            Submodules are not included (their paths are empty dirs).

          '' + lib.concatMapStrings (c: ''
            - `${c.dir}/` — ${c.desc}
              (${c.url} @ ${c.rev})
          '') languagesCodebasesMeta);

          languagesOpt = pkgs.runCommand "nanocodex-languages-opt"
            { nativeBuildInputs = [ pkgsTools.nodejs_22 ]; } ''
            mkdir -p build/vendor build/package
            cp -r ${./languages/src} build/src
            cp ${./languages/build-bootstrap.mjs} build/build-bootstrap.mjs
            # In-repo engines land in vendor/ (fetch-vendor.sh's final copy).
            cp ${./languages/engines}/* build/vendor/

            cd build
            tar -xzf ${languagesVendor.babel} package/babel.min.js
            mv package/babel.min.js vendor/babel.min.js
            tar -xzf ${languagesVendor.react} package/umd/react.production.min.js
            mv package/umd/react.production.min.js vendor/react.min.js
            tar -xzf ${languagesVendor.react-dom} package/umd/react-dom-server-legacy.browser.production.min.js
            mv package/umd/react-dom-server-legacy.browser.production.min.js vendor/react-dom-server.min.js
            tar -xzf ${languagesVendor.marked} package/marked.min.js
            mv package/marked.min.js vendor/marked.min.js
            tar -xzf ${languagesVendor.mermaid} package/dist/mermaid.min.js
            mv package/dist/mermaid.min.js vendor/mermaid.min.js
            tar -xzf ${languagesVendor.minizinc} package/dist/minizinc-worker.js package/dist/minizinc.data package/dist/minizinc.wasm
            mv package/dist/minizinc-worker.js vendor/minizinc-worker.js
            mv package/dist/minizinc.data vendor/minizinc.data
            mv package/dist/minizinc.wasm vendor/minizinc.wasm
            tar -xzf ${languagesVendor.wasmoon} package/dist/index.js package/dist/glue.wasm
            mv package/dist/index.js vendor/wasmoon.js
            mv package/dist/glue.wasm vendor/lua.wasm
            cp ${languagesVendor.acadlisp-js} vendor/acadlisp.js
            cp ${languagesVendor.acadlisp-wasm} vendor/acadlisp.wasm

            mkdir -p $out/opt/languages
            node build-bootstrap.mjs $out/opt/languages/bootstrap.js
            for f in picat.wasm tla_checker.wasm minizinc.wasm acadlisp.wasm lua.wasm craftos.wasm; do
              cp vendor/$f $out/opt/languages/$f
            done
            cp ${./languages/filesystem.rego} $out/opt/languages/filesystem.rego
            cp ${./languages/policies.json} $out/opt/languages/policies.json
            cp ${./languages/filesystem-skills.rego} $out/opt/languages/filesystem-skills.rego
            cp ${./languages/policies-skills.json} $out/opt/languages/policies-skills.json

            # Read-only reference source trees under /opt/languages/codebases
            # (see languagesCodebases). chmod so the copied store paths (0444)
            # are writable during layering; the runtime mount stays read-only.
            mkdir -p $out/opt/languages/codebases
            ${languagesCodebasesCopy}
            cp ${languagesCodebasesReadme} $out/opt/languages/codebases/README.md
            chmod -R u+w $out/opt/languages/codebases

            # Pre-packaged codex skills — languages images only (merged into
            # /codex-home next to the rootfs-provided config.toml).
            mkdir -p $out/codex-home
            cp -r ${./languages/skills} $out/codex-home/skills
          '';

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
          # BRIDGE_PROXY_TARGET is read per request by the app's /agui route
          # handler (NOT a rewrite — rewrites buffer SSE and are build-frozen).
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

          mkStandaloneImage = { name, programs, extraContents ? [ ], basePorts ? [ 4500 8080 ], extraPorts ? [ ], extraFakeRoot ? "", extraEnv ? [ ], modelProvider ? "azure", model ? "gpt-5.4" }:
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
                  "NANOCODEX_MODEL_PROVIDER=${modelProvider}"
                  "NANOCODEX_MODEL=${model}"
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
          # standalone-frontend + the language engines, with the `skills`
          # sandbox preset (real-fs /work + self-editable /codex-home/skills)
          # and Ollama Cloud glm-5.2 as the default model.
          standaloneLanguagesImage = mkStandaloneImage {
            name = "ghcr.io/r33drichards/nanocodex-standalone-languages";
            programs = [ mcpV8Program codexProgram bridgeProgram frontendProgram ];
            extraContents = [ pkgsTools.nodejs_22 languagesOpt ];
            extraPorts = [ 8130 3000 ];
            extraFakeRoot = bridgeFakeRoot + frontendFakeRoot;
            extraEnv = [ "NANOCODEX_SANDBOX=skills" ];
            modelProvider = "ollama-cloud";
            model = "glm-5.2";
          };

          baseImageConfig = {
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
            config = baseImageConfig;
          };

          # The base runtime + the WASM language engines at /opt/languages
          # (formerly Dockerfile.languages; now pure nix via languagesOpt).
          languagesImage = pkgs.dockerTools.streamLayeredImage {
            name = "ghcr.io/r33drichards/nanocodex-languages";
            tag = "latest";
            contents = [
              rootfs
              languagesOpt
              pkgs.cacert
              pkgs.bashInteractive
              pkgs.coreutils
              pkgs.curl
            ];
            fakeRootCommands = ''
              chmod 1777 tmp
            '';
            config = baseImageConfig;
          };
        in
        {
          inherit codex-app-server image;
          mcp-v8 = mcpV8;
          bridge = bridgeEnv;
          slackbot = slackbotApp;
          frontend = frontendApp;
          languages-opt = languagesOpt;
          languages = languagesImage;
          standalone = standaloneImage;
          standalone-frontend = standaloneFrontendImage;
          standalone-slack = standaloneSlackImage;
          standalone-full = standaloneFullImage;
          slack-remote = slackRemoteImage;
          standalone-languages = standaloneLanguagesImage;
          default = image;
        };
    in
    {
      packages = forSystems linuxSystems packagesFor;
    };
}
