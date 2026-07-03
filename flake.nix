{
  description = "nanocodex — locked-down codex app-server with per-thread mcp-v8 sandboxes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # mcp-v8: build from the mcp-js v0.18.1 release tag (has --config and OAuth
    # fetch headers; PRs #184/#192). Its flake already handles the rusty_v8
    # prefetch and cargo vendor hash. Pinned to a release, not our fork branch.
    mcp-js.url = "github:r33drichards/mcp-js/v0.18.1";
    # codex fork: source only — its in-tree flake builds the whole workspace,
    # which can't vendor hermetically (libwebrtc git submodule, no v8
    # archive). We build just the codex-app-server binary ourselves; that
    # bin's dependency graph avoids libwebrtc entirely.
    codex-src = {
      url = "github:r33drichards/codex/claude/kodex-mcp-js-library-37218h";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, rust-overlay, mcp-js, codex-src }:
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
          default = image;
        };
    in
    {
      packages = forSystems linuxSystems packagesFor;
    };
}
