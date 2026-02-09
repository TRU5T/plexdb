#!/bin/bash
# Run Plex DB Merge app with sqlite3 from ./bin (for .recover support).
# Usage: ./run.sh   or   bash run.sh

cd "$(dirname "$0")"
export SQLITE3="$(pwd)/bin/sqlite3"

if [[ ! -x "$SQLITE3" ]]; then
  echo "Error: $SQLITE3 not found or not executable."
  echo "Download sqlite-tools from https://www.sqlite.org/2026/sqlite-tools-linux-x64-3510200.zip"
  echo "and put the sqlite3 binary in $(pwd)/bin/"
  exit 1
fi

echo "Using SQLite: $SQLITE3 ($("$SQLITE3" --version 2>/dev/null || echo 'version unknown'))"
echo "Starting app at http://127.0.0.1:5000"
echo ""

.venv/bin/python app.py
