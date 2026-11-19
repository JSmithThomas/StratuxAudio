#!/bin/bash
# stratuxaudio-check: is the AirPlay receiver healthy, and is traffic still first?
# Installed as /usr/local/bin/stratuxaudio-check. Read-only; safe to run any time.

ok()   { printf '  [ ok ] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
info() { printf '  [info] %s\n' "$*"; }

echo "StratuxAudio $(cat /etc/stratuxaudio-version 2>/dev/null || echo '?')"

systemctl is-active --quiet avahi-daemon && ok "avahi-daemon running (iPhone discovery)" \
  || bad "avahi-daemon not running: the iPhone won't find the speaker"
systemctl is-active --quiet shairport-sync && ok "shairport-sync running" \
  || bad "shairport-sync not running: journalctl -u shairport-sync -n 30"

if systemctl is-active --quiet stratuxaudio-status; then
  curl -s -m 3 http://localhost:8090/status.json | grep -q now_playing && ok "Audio page service answering (port 8090)"     || bad "stratuxaudio-status running but not answering on port 8090"
else
  bad "stratuxaudio-status not running (the web UI's Audio page): journalctl -u stratuxaudio-status -n 30"
fi
grep -q 'href="#/audio"' /opt/stratux/www/index.html 2>/dev/null && ok "Audio page in the Stratux web UI menu"   || info "Audio page not in the web UI menu (a Stratux update replaces the web UI: run install.sh again)"

dev="$(sed -n 's/^ *output_device *= *"\(.*\)";.*/\1/p' /etc/shairport-sync.conf 2>/dev/null)"
card="$(echo "$dev" | sed -n 's/.*CARD=\([^,]*\).*/\1/p')"
if [ -n "$card" ] && grep -q "\b$card\b" /proc/asound/cards 2>/dev/null; then
  ok "sound device present: $dev"
else
  bad "sound device $dev not found: is the USB dongle plugged in?"
fi

ip -4 addr show ap0 2>/dev/null | grep -q inet && ok "Stratux Wi-Fi (ap0) up: $(ip -4 -o addr show ap0 | awk '{print $4}')" \
  || info "ap0 not up (Stratux Wi-Fi mode other than AP?)"

if command -v avahi-browse >/dev/null; then
  avahi-browse -tp _raop._tcp 2>/dev/null | grep -q . && ok "AirPlay service advertised on the network" \
    || bad "AirPlay service not seen on the network"
else
  info "avahi-browse not installed; skipping the advertisement check"
fi

[ "$(findmnt -n -o FSTYPE /)" = overlay ] && ok "overlay on (SD card protected)" \
  || info "overlay OFF: turn it back on with: sudo overlayctl enable && sudo reboot"

echo "  CPU (top 5):"
ps -eo pcpu,ni,comm --sort=-pcpu | head -6 | sed 's/^/    /'
echo "  Compare traffic message rates on the Stratux status page with and without music."
