#!/bin/bash
# macOS: double-click to run. (Linux: bash start.command)
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not installed. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close."
  exit 1
fi
python3 -m teukbo.local "$@"
echo
read -r -p "Press Enter to close."
