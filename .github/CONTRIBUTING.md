# Contributing

CI (`.github/workflows/ci.yml`) lints, formats, and tests every language
ecosystem in the repo — each in its own parallel job. All of them roll up into
a single **`all-green`** status check. Run the equivalents locally before you
push:

## Python — `client/`, `integration/`, `frontend/e2e/`, skill scripts

```bash
pipx run ruff==0.15.8 check .            # lint (auto-fix with --fix)
pipx run ruff==0.15.8 format .           # format (drop --check to write)
pip install -e "client/[test]"
python -m pytest client/tests --ignore=client/tests/e2e
```

Config: `ruff.toml` (line length 100; rules E/F/W/I). Generated protobuf output
under `client/nanocodex_client/proto/` is excluded.

## Node / TypeScript — `frontend/`, `slackbot/`

```bash
cd frontend  && npm ci && npx tsc --noEmit                 # typecheck
cd slackbot  && npm ci && npx tsc --noEmit && npm test      # typecheck + tests
npx prettier@3.8.1 --check "frontend/**/*.{ts,tsx,js,jsx,mjs,css}" \
                           "slackbot/**/*.{ts,js,mjs}" "languages/*.mjs"
```

Config: `.prettierrc.json` (print width 100), `.prettierignore`.

## Protobuf — `proto/`

```bash
cd proto && buf lint          # buf breaking runs against main in CI on PRs
```

Formatting is left as-is: the `.proto` files use intentional column-aligned
comments that `buf format` would collapse.

## Rego (OPA) — `policies/`, `languages/`

```bash
for f in $(git ls-files '*.rego'); do opa check --strict "$f"; done  # OPA >= 1.0
```

## Shell / GitHub Actions / Nix

```bash
shellcheck -e SC2015 $(git ls-files '*.sh')
actionlint                        # SHELLCHECK_OPTS=--exclude=SC2016
nix flake check --no-build
```

## Making CI block PRs

CI runs on every pull request automatically. To *require* it before merge, a
repo admin sets **`all-green`** as a required status check:
Settings → Branches (or Rules → Rulesets) → protect `main` → **Require status
checks to pass** → add **`all-green`**. That one check covers the whole matrix.
