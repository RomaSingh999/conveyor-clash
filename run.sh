#!/usr/bin/env bash
# Mac / Linux: run with  bash run.sh
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
echo "Conveyor Clash is starting at http://localhost:8000 (Ctrl+C to stop)"
(sleep 3; (command -v open >/dev/null && open http://localhost:8000) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8000)) >/dev/null 2>&1 &
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
