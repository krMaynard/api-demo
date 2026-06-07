#!/usr/bin/env bash
# Refresh the vendored dataset snapshot (data/vlop-dsa.json) from the canonical
# source in the sibling krMaynard.github.io repo. The snapshot is what the Docker
# image is seeded from at build time, so run this whenever the upstream dataset
# changes, then commit the result.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${1:-$here/../krMaynard.github.io/data/vlop-dsa.json}"
dst="$here/data/vlop-dsa.json"

if [[ ! -f "$src" ]]; then
  echo "Source dataset not found: $src" >&2
  echo "Pass the path explicitly: scripts/refresh-dataset.sh /path/to/vlop-dsa.json" >&2
  exit 1
fi

python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$src"  # validate JSON
cp "$src" "$dst"
echo "Updated $dst from $src ($(wc -c <"$dst") bytes)."
