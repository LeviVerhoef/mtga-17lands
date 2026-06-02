#!/usr/bin/env bash
# Run the MTGA Draft Overlay on macOS / Linux.
# Usage: ./run.sh [path/to/Player.log]
set -euo pipefail
python3 main.py ${1:+-f "$1"}
