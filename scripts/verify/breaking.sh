#!/usr/bin/env bash
# Fail when the working tree's openapi/v1.yaml breaks clients of the spec at a
# base ref. Additive changes pass; removals and narrowings fail.
#   scripts/verify/breaking.sh               # against origin/main
#   scripts/verify/breaking.sh HEAD          # against the last commit
# oasdiff runs from a pinned image, so nothing is installed on the host.
set -uo pipefail
OASDIFF=tufin/oasdiff@sha256:4796ad5d47a6697b195a0e33cf10f63b2718c17a5fb4b54086beb72251966f7d
base_ref="${1:-origin/main}"
root=$(git rev-parse --show-toplevel)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if ! git -C "$root" show "$base_ref:openapi/v1.yaml" > "$tmp/base.yaml" 2>/dev/null; then
  echo "PASS breaking: $base_ref has no openapi/v1.yaml, nothing to break"
  exit 0
fi
cp "$root/openapi/v1.yaml" "$tmp/head.yaml"
docker run --rm -v "$tmp:/specs:ro" "$OASDIFF" breaking /specs/base.yaml /specs/head.yaml --fail-on ERR
code=$?
if [ $code -eq 0 ]; then echo "PASS breaking: no breaking change against $base_ref"
else echo "FAIL breaking: breaking change against $base_ref (oasdiff exit $code)"; fi
exit $code
