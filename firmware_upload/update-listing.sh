#!/usr/bin/env bash
# Regenerate files.json describing the firmware directory's contents, so the
# static index.html can list them on any web server (no autoindex needed).
#
# Usage:  ./update-listing.sh [FIRMWARE_DIR]
# Cron:   */5 * * * * /var/www/firmware/update-listing.sh
set -euo pipefail

DIR="${1:-/var/www/firmware}"
cd "$DIR"

{
  printf '{\n  "files": [\n'
  first=1
  for f in *; do
    # Skip the page, the listing itself, and directories.
    [ -f "$f" ] || continue
    case "$f" in index.html|files.json|manifest.json|update-listing.sh|*.tmp) continue ;; esac

    size=$(stat -c %s "$f")
    modified=$(date -u -d "@$(stat -c %Y "$f")" '+%Y-%m-%d %H:%M UTC')
    md5=$(md5sum "$f" | cut -d' ' -f1)

    [ $first -eq 1 ] || printf ',\n'
    first=0
    printf '    {"name": "%s", "size_bytes": %s, "modified": "%s", "md5": "%s"}' \
      "$f" "$size" "$modified" "$md5"
  done
  printf '\n  ]\n}\n'
} > files.json.tmp

mv files.json.tmp files.json
echo "Wrote $DIR/files.json"
