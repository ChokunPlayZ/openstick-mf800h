#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -eq 0 ]; then
    sh "$HERE/run.sh" --help
    printf '\nGuide: %s/docs/FLASHING.md\nPress Return to close.\n' "$HERE"
    read -r unused
else
    exec sh "$HERE/run.sh" "$@"
fi
