#!/usr/bin/env bash
# Lint the committed OpenAPI spec with redocly.yaml. Pinned, so a new Redocly
# release cannot change the error count under a phase gate.
#   scripts/verify/lint.sh            # openapi/v1.yaml
#   scripts/verify/lint.sh other.yaml
set -uo pipefail
root=$(git rev-parse --show-toplevel)
spec="${1:-$root/openapi/v1.yaml}"
out=$(cd "$root" && npx -y @redocly/cli@2.59.0 lint "$spec" --config "$root/redocly.yaml" --format=summary 2>&1)
code=$?
echo "$out" | grep -E '^(error|warning) ' || true
summary=$(echo "$out" | grep -E 'Validation failed|Woohoo|valid' | tail -1)
if [ $code -eq 0 ]; then echo "PASS lint: $summary"; else echo "FAIL lint: $summary"; fi
exit $code
