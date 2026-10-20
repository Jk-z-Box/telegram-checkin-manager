#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest -q
python -m compileall -q app
bash -n install.sh scripts/tgcheckin
if command -v node >/dev/null; then node --check app/static/app.js; fi
