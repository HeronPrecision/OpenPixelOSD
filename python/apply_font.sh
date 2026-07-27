#!/bin/sh
# Re-merge a face (applying any edits in python/overrides/<face>_*.txt) and re-render
# the font sheets. Usage:
#   sh python/apply_font.sh terminus
# Edit cycle: edit python/overrides/terminus_0x47.txt, run this, view the sheet.
set -e
FACE="${1:?usage: apply_font.sh <face>}"
PY="${PY:-.venv/bin/python}"
$PY python/merge_font.py \
    --base src/fonts/font_bf_default.h \
    --candidate "python/candidates/${FACE}.bin" \
    --face "$FACE" \
    --out "src/fonts/font_data_${FACE}.c"
$PY python/render_font_sheets.py
echo "applied -> python/candidates/sheet_${FACE}.png"
