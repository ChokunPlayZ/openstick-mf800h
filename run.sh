#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$HERE/.venv/bin/python" ]; then
    PYTHON="$HERE/.venv/bin/python"
else
    PYTHON=python3
fi
if [ "$#" -eq 0 ]; then
    set -- --help
fi
exec "$PYTHON" "$HERE/flash.py" "$@"
