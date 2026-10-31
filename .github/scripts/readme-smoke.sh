#!/usr/bin/env bash
# Runs the README quickstart, command for command.
# Any drift between README.md and reality breaks this job.
#
# The README installs the released wheel from the GitHub release page; this
# job builds the very same wheel from the checkout and installs that one.
set -euo pipefail

python -m pip install --upgrade pip build
python -m build

WORK="$(mktemp -d)"
cd "$WORK"

python -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "$GITHUB_WORKSPACE"/dist/*.whl

icyloom init
icyloom doctor

icyloom serve &
SERVE_PID=$!
trap 'kill $SERVE_PID 2>/dev/null || true' EXIT

# Wait for the MCP HTTP and SSE transports to accept connections.
for port in 8151 8152; do
  for _ in $(seq 1 60); do
    if python -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1', $port))==0 else 1)"; then
      break
    fi
    sleep 1
  done
done

icyloom check --transport all
