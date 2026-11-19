#!/bin/bash
# Build the offline update file: update-stratuxaudio-v<version>.sh
#
# Upload that one file through the Stratux web page (Settings > Update) and the
# Stratux installs StratuxAudio at its next start, with no internet. Stratux runs
# any uploaded file named like update*stratux*v*.sh (debian/stratux-pre-start.sh).
#
# Run this ON A STRATUX that:
#   - is on the same Stratux version as the airplanes it's for;
#   - does NOT have StratuxAudio installed yet (only packages missing from the
#     image get bundled, so the bundle has to be made on a clean one);
#   - has internet (AP+Client mode). The overlay can stay on: everything goes to /tmp.
#
#   sudo ./tools/make-offline-update.sh
#
# The file lands in the current folder; copy it off with scp.

set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/install.sh")"
OUT="$PWD/update-stratuxaudio-v${VERSION}.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if dpkg-query -W -f='${Status}' shairport-sync 2>/dev/null | grep -q "ok installed"; then
  echo "shairport-sync is already installed here; build the bundle on a clean Stratux." >&2
  exit 1
fi
if [ "$(date +%Y)" -lt 2025 ]; then
  d="$(curl -sI https://deb.debian.org/ | tr -d '\r' | sed -n 's/^[Dd]ate: //p')"
  [ -n "$d" ] && date -s "$d" >/dev/null
fi

PKG="$WORK/stratuxaudio"
mkdir -p "$PKG/debs/partial" "$PKG/config"
apt-get update
# Downloads shairport-sync plus every dependency this image is missing.
apt-get install -y --download-only --no-install-recommends \
  -o Dir::Cache::archives="$PKG/debs" shairport-sync avahi-daemon alsa-utils avahi-utils
rm -rf "$PKG/debs/partial" "$PKG/debs/lock"

cp "$ROOT/install.sh" "$ROOT/check.sh" "$PKG/"
cp -r "$ROOT/status" "$ROOT/webui" "$PKG/"
cp "$ROOT/config/"* "$PKG/config/"
echo "Bundled packages:"; ls -1 "$PKG/debs"

cat > "$OUT" <<'HEADER'
#!/bin/bash
# StratuxAudio offline installer. Upload through the Stratux web page
# (Settings > Update); Stratux runs it at the next start and reboots.
T="$(mktemp -d)"
sed '1,/^__PAYLOAD_BELOW__$/d' "$0" | base64 -d | tar xz -C "$T"
bash "$T/stratuxaudio/install.sh" --debs "$T/stratuxaudio/debs" --boot
rc=$?
rm -rf "$T"
exit $rc
__PAYLOAD_BELOW__
HEADER
tar cz -C "$WORK" stratuxaudio | base64 >> "$OUT"
chmod +x "$OUT"
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
