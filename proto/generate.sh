#!/usr/bin/env bash
# Regenerate the Python protobuf code from proto/nanocodex/v1/*.proto.
#
# Uses the client venv's betterproto compiler (protoc-gen-python_betterproto)
# driven by grpc_tools.protoc, so no system protoc/buf is required. Run buf
# lint/breaking separately via `nix run nixpkgs#buf -- lint` (see buf.yaml).
#
#   proto/generate.sh            # regenerate into client/nanocodex_client/proto
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
venv="$repo/client/.venv"

if [[ ! -x "$venv/bin/protoc-gen-python_betterproto" ]]; then
  echo "installing betterproto compiler into $venv ..." >&2
  "$venv/bin/pip" install -q "betterproto[compiler]==2.0.0b7" grpcio-tools
fi

out="$repo/client/nanocodex_client/proto"
mkdir -p "$out"
PATH="$venv/bin:$PATH" "$venv/bin/python" -m grpc_tools.protoc \
  -I "$here" \
  --python_betterproto_out="$out" \
  "$here"/nanocodex/v1/*.proto
echo "generated -> $out" >&2
