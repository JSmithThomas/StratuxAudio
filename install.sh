#!/bin/bash
# StratuxAudio installer: adds an AirPlay receiver (shairport-sync) to a Stratux.
#
# Two ways it runs:
#   1. By hand over SSH, Stratux in AP+Client mode (internet), overlay OFF:
#        sudo ./install.sh
#      Packages come from apt.
#   2. Inside the offline update file (update-stratuxaudio-v*.sh) uploaded through
#      the Stratux web page. Stratux runs it at start-up on the writable system:
#        install.sh --debs <dir> --boot
#      Packages come from <dir>; no systemctl calls (they can interrupt Stratux's
#      start-up step); the Stratux reboots afterward.
#
# Options:
#   --debs DIR     install .deb files from DIR instead of apt
#   --boot         running inside Stratux start-up: don't touch running services
#   --device DEV   ALSA output device (default: the USB sound dongle, found automatically)
#   --name NAME    AirPlay speaker name (default: "Stratux Audio")
#   --level PCT    dongle hardware output level, 0-100 (default 80, or the level last
#                  set on the web page's Audio screen)

set -euo pipefail

VERSION="1.2.2"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG=/var/log/stratuxaudio-install.log

DEBS=""
BOOT=no
DEVICE=""
NAME="Stratux Audio"
LEVEL=80
LEVEL_GIVEN=no
SAVED=/boot/firmware/stratuxaudio.json   # level set from the web page; survives the overlay

while [ $# -gt 0 ]; do
  case "$1" in
    --debs)   DEBS="$2"; shift 2 ;;
    --boot)   BOOT=yes; shift ;;
    --device) DEVICE="$2"; shift 2 ;;
    --name)   NAME="$2"; shift 2 ;;
    --level)  LEVEL="$2"; LEVEL_GIVEN=yes; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { echo "$(date -Is) stratuxaudio-install: $*" | tee -a "$LOG"; }
die() { log "ERROR: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"
log "StratuxAudio $VERSION installing (boot=$BOOT, debs=${DEBS:-apt})"

# --- 1. The root filesystem must be writable for real -------------------------
# Stratux runs with an overlay: changes go to RAM and vanish at reboot.
if [ "$(findmnt -n -o FSTYPE /)" = "overlay" ]; then
  die "the Stratux overlay is on, so nothing installed would survive a reboot.
  Turn it off first:  sudo overlayctl disable && sudo reboot
  Then run this again, and afterward:  sudo overlayctl enable && sudo reboot"
fi

# --- 2. Packages ---------------------------------------------------------------
if [ -n "$DEBS" ]; then
  [ -d "$DEBS" ] || die "no such folder: $DEBS"
  # Stop package scripts from starting services in the middle of Stratux start-up.
  POLICY=/usr/sbin/policy-rc.d
  if [ "$BOOT" = yes ] && [ ! -e "$POLICY" ]; then
    printf '#!/bin/sh\nexit 101\n' > "$POLICY"; chmod +x "$POLICY"
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  fi
  # Install only what isn't already there, so we never downgrade a library
  # the Stratux image already has.
  todo=()
  for f in "$DEBS"/*.deb; do
    [ -e "$f" ] || continue
    pkg="$(dpkg-deb -f "$f" Package)"
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
      log "already installed, skipping: $pkg"
    else
      todo+=("$f")
    fi
  done
  if [ ${#todo[@]} -gt 0 ]; then
    log "installing ${#todo[@]} package(s): ${todo[*]##*/}"
    DEBIAN_FRONTEND=noninteractive dpkg -i --force-confold "${todo[@]}" >>"$LOG" 2>&1 \
      || die "dpkg failed; see $LOG"
  fi
else
  # Stratux syncs its clock from GPS (timesyncd is off). On the ground without a
  # fix the clock can be years off, and apt then rejects the package lists.
  # Plain HTTP on purpose: with the clock wrong, HTTPS certificates look "not yet valid".
  d="$( (curl -sI -m 10 http://deb.debian.org/ || true) | tr -d '\r' | sed -n 's/^[Dd]ate: //p')"
  if [ -n "$d" ]; then
    skew=$(( $(date -d "$d" +%s) - $(date +%s) ))
    if [ "${skew#-}" -gt 3600 ]; then
      log "clock reads $(date) but the internet says $d; setting it so apt will work"
      date -s "$d" >/dev/null || die "couldn't set the clock"
    fi
  elif [ "$(date +%Y)" -lt 2025 ]; then
    die "clock is wrong and couldn't be checked; set it with: sudo date -s 'YYYY-MM-DD HH:MM'"
  fi
  log "installing from apt (needs internet: Stratux AP+Client mode)"
  apt-get update >>"$LOG" 2>&1 || die "apt-get update failed; see $LOG"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    -o Dpkg::Options::=--force-confold shairport-sync avahi-daemon alsa-utils avahi-utils >>"$LOG" 2>&1 \
    || die "apt-get install failed; see $LOG"
fi

command -v shairport-sync >/dev/null || die "shairport-sync did not install"
log "$(shairport-sync -V 2>&1 | head -1) (this string shows AirPlay2 only if built with it)"

# --- 3. Find the USB sound dongle ------------------------------------------------
if [ -z "$DEVICE" ]; then
  card=""
  for c in /proc/asound/card[0-9]*; do
    [ -e "$c/usbid" ] && { card="$(cat "$c/id")"; break; }
  done
  if [ -n "$card" ]; then
    DEVICE="plughw:CARD=${card},DEV=0"
    log "USB sound dongle found: $card"
    # Set its hardware output to a fixed level; the phone controls volume from there.
    # A level set earlier on the Audio page wins, unless --level was given.
    if [ "$LEVEL_GIVEN" = no ] && [ -f "$SAVED" ]; then
      saved="$(sed -n 's/.*"level_pct": *\([0-9]*\).*/\1/p' "$SAVED")"
      [ -n "$saved" ] && LEVEL="$saved"
    fi
    if [ -d /boot/firmware ]; then
      # Update only the level; keep the equalizer, mono and anything else saved there.
      python3 - "$SAVED" "$LEVEL" "$(date -Is)" <<'PY' >>"$LOG" 2>&1 || log "WARNING: couldn't save the level"
import json, os, sys
path, level, when = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    with open(path) as f:
        d = json.load(f)
except (OSError, ValueError):
    d = {}
d.update(level_pct=level, level_saved_at=when)
with open(path + ".tmp", "w") as f:
    json.dump(d, f)
os.replace(path + ".tmp", path)
PY
    fi
    if command -v amixer >/dev/null; then
      ctl="$(amixer -c "$card" scontrols 2>/dev/null | head -1 | sed -n "s/^Simple mixer control '\(.*\)',.*/\1/p")"
      if [ -n "$ctl" ]; then
        amixer -c "$card" sset "$ctl" "${LEVEL}%" unmute >>"$LOG" 2>&1 || true
        # C-Media dongles have an "Auto Gain Control" switch meant for the mic; keep it off.
        amixer -c "$card" sset "Auto Gain Control" off >>"$LOG" 2>&1 || true
        alsactl store >>"$LOG" 2>&1 || true
        log "dongle '$ctl' set to ${LEVEL}%"
      fi
    fi
  else
    die "no USB sound dongle found. Plug it in and run again, or pass --device (for example --device plughw:CARD=Headphones,DEV=0 for the Pi's own jack)"
  fi
fi

# --- 4. Configuration ------------------------------------------------------------
[ -f /etc/shairport-sync.conf ] && [ ! -f /etc/shairport-sync.conf.orig ] \
  && cp /etc/shairport-sync.conf /etc/shairport-sync.conf.orig
sed -e "s|@OUTPUT_DEVICE@|${DEVICE}|" -e "s|@NAME@|${NAME}|" \
  "$HERE/config/shairport-sync.conf.template" > /etc/shairport-sync.conf
install -D -m 644 "$HERE/config/stratuxaudio-priority.conf" \
  /etc/systemd/system/shairport-sync.service.d/stratuxaudio.conf
install -D -m 755 "$HERE/check.sh" /usr/local/bin/stratuxaudio-check
echo "$VERSION" > /etc/stratuxaudio-version
log "config written: device=$DEVICE name=\"$NAME\""

# --- 4b. "Audio" page in the Stratux web UI ----------------------------------------
# A small status service (now playing + audio health, port 8090) and the page itself.
install -D -m 755 "$HERE/status/stratuxaudio-status.py" /usr/local/lib/stratuxaudio/stratuxaudio-status.py
install -D -m 644 "$HERE/config/stratuxaudio-status.service" /etc/systemd/system/stratuxaudio-status.service
mkdir -p /usr/local/lib/stratuxaudio/webui/js
cp "$HERE/webui/audio.html" "$HERE/webui/audio-help.html" "$HERE/webui/patch-webui.py" /usr/local/lib/stratuxaudio/webui/
cp "$HERE/webui/js/audio.js" /usr/local/lib/stratuxaudio/webui/js/
if [ -d /opt/stratux/www ]; then
  python3 /usr/local/lib/stratuxaudio/webui/patch-webui.py install /opt/stratux/www >>"$LOG" 2>&1 \
    && log "Audio page added to the Stratux web UI menu" \
    || log "WARNING: couldn't add the Audio page to this Stratux version's web UI (AirPlay still works); see $LOG"
else
  log "no Stratux web UI at /opt/stratux/www; skipping the Audio page"
fi

# --- 5. Turn the services on -----------------------------------------------------
if [ "$BOOT" = yes ]; then
  # Same result as 'systemctl enable', without asking systemd to reload in the
  # middle of Stratux start-up. Takes effect at the reboot that follows.
  wants=/etc/systemd/system/multi-user.target.wants
  mkdir -p "$wants"
  for u in shairport-sync avahi-daemon stratuxaudio-status; do
    unit=""
    for d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
      if [ -f "$d/$u.service" ]; then unit="$d/$u.service"; break; fi
    done
    if [ -n "$unit" ]; then
      ln -sf "$unit" "$wants/$u.service"
      log "enabled $u"
    else
      log "WARNING: no unit file for $u"
    fi
  done

  # Stratux turned its SD-card protection (the overlay) off to run this update. Newer
  # Stratux versions turn it back on afterward; v2.0 (pre4) leaves it off. Turn it back
  # on for the next boot, unless the owner keeps it off on purpose ("Persistent Logging").
  # Same method Stratux's own updater uses: remove the flag file, don't call overlayctl.
  if [ "$(findmnt -n -o FSTYPE /)" != overlay ] && [ -e /overlay/disable ]; then
    if grep -q '"PersistentLogging": *true' /boot/firmware/stratux.conf 2>/dev/null; then
      log "Persistent Logging is on, so the overlay stays off"
    else
      rm -f /overlay/disable && log "SD-card protection (overlay) back on from the next boot"
    fi
  fi
  log "done; services start after the reboot"
else
  systemctl daemon-reload
  systemctl enable --now avahi-daemon >>"$LOG" 2>&1 || true
  systemctl enable shairport-sync >>"$LOG" 2>&1
  systemctl restart shairport-sync
  systemctl enable stratuxaudio-status >>"$LOG" 2>&1
  systemctl restart stratuxaudio-status
  sleep 2
  systemctl is-active --quiet shairport-sync || die "shairport-sync didn't start; see: journalctl -u shairport-sync"
  log "done; \"$NAME\" should now show in the iPhone's AirPlay list"
  echo
  echo "Next: turn the overlay back on so the SD card is protected again:"
  echo "  sudo overlayctl enable && sudo reboot"
fi
