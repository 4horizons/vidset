#!/bin/bash
# Open the saved judge presentation in Safari; no server or model calls.
set -euo pipefail
demo_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
open -a Safari "$demo_dir/START-HERE.html"
