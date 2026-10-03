#!/usr/bin/env bash
# Probe de vânătoare, în afara CI.
# CI rulează doar: cd packages/core && uv run pytest tests/unit/
# Suita de aici este așteptată să iasă cu cod diferit de zero cât timp
# bug-urile din docs/audit/BUG-HUNT-2026-10-03.md sunt deschise.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}/packages/core${PYTHONPATH:+:$PYTHONPATH}"
python3 -m pytest probes/bug-hunt-2026-10-03 -q --tb=line \
  --confcutdir=probes/bug-hunt-2026-10-03 "$@"
