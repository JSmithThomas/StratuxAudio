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
# Or, to ship new StratuxAudio scripts with the packages from an earlier update file
# (no Stratux, no root, no internet needed; runs on any machine with bash):
#
#   ./tools/make-offline-update.sh --debs-from update-stratuxaudio-v1.1.0.sh
#
# The file lands in the current folder; copy it off with scp.

set -euo pipefail

DEBS_FROM=""
if [ "${1:-}" = "--debs-from" ]; then
  DEBS_FROM="$2"
  [ -f "$DEBS_FROM" ] || { echo "no such file: $DEBS_FROM"; exit 1; }
  DEBS_FROM="$(cd "$(dirname "$DEBS_FROM")" && pwd)/$(basename "$DEBS_FROM")"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^VERSION="\(.*\)"/\1/p' "$ROOT/install.sh")"
OUT="$PWD/update-stratuxaudio-v${VERSION}.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PKG="$WORK/stratuxaudio"
mkdir -p "$PKG/config"

if [ -n "$DEBS_FROM" ]; then
  [ "$DEBS_FROM" != "$OUT" ] || { echo "the old and new file have the same name; bump VERSION in install.sh"; exit 1; }
  OLD="$WORK/old"; mkdir -p "$OLD"
  sed '1,/^__PAYLOAD_BELOW__$/d' "$DEBS_FROM" | base64 -d | tar xz -C "$OLD"
  [ -d "$OLD/stratuxaudio/debs" ] || { echo "no packages inside $DEBS_FROM"; exit 1; }
  mv "$OLD/stratuxaudio/debs" "$PKG/debs"
  echo "Reusing $(ls -1 "$PKG/debs" | wc -l) packages from $(basename "$DEBS_FROM")"
else
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

if dpkg-query -W -f='${Status}' shairport-sync 2>/dev/null | grep -q "ok installed"; then
  echo "shairport-sync is already installed here; build the bundle on a clean Stratux." >&2
  exit 1
fi
# Stratux sets its clock from GPS; without a fix it can be days off and apt rejects
# the package lists. Check against a web server over plain HTTP (HTTPS fails while
# the clock is wrong) and set it if it's more than an hour out.
d="$( (curl -sI -m 10 http://deb.debian.org/ || true) | tr -d '\r' | sed -n 's/^[Dd]ate: //p')"
if [ -n "$d" ]; then
  skew=$(( $(date -d "$d" +%s) - $(date +%s) ))
  if [ "${skew#-}" -gt 3600 ]; then echo "Setting the clock to $d"; date -s "$d" >/dev/null; fi
else
  echo "Couldn't reach deb.debian.org: no internet? Set Stratux Wi-Fi to AP+Client." >&2
  exit 1
fi

mkdir -p "$PKG/debs/partial"
apt-get update
# Downloads shairport-sync plus every dependency this image is missing.
apt-get install -y --download-only --no-install-recommends \
  -o Dir::Cache::archives="$PKG/debs" shairport-sync avahi-daemon alsa-utils avahi-utils
rm -rf "$PKG/debs/partial" "$PKG/debs/lock"
fi

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
