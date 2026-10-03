#!/bin/bash
# Run everything without Docker (Mac/Linux). Usage: ./run_local.sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "== Creating Python environment (first time only, ~2 min)"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
set -a; source .env; set +a

echo "== Running tests"
.venv/bin/python -W ignore -m unittest tests.test_all 2>&1 | tail -1

.venv/bin/python -m data.make_demo_data

echo "== Starting API on http://localhost:8000/docs"
.venv/bin/uvicorn api.main:app --port 8000 > api.log 2>&1 &
API_PID=$!
trap "kill $API_PID 2>/dev/null" EXIT
sleep 2

echo "== Starting dashboard on http://localhost:8501  (Ctrl+C stops both)"
API_URL=http://localhost:8000 .venv/bin/streamlit run dashboard/app.py --server.port 8501
