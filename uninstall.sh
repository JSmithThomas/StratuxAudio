#!/bin/bash
# Remove StratuxAudio. Overlay must be off (sudo overlayctl disable && sudo reboot).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ "$(findmnt -n -o FSTYPE /)" = overlay ] && { echo "Overlay is on; turn it off first: sudo overlayctl disable && sudo reboot"; exit 1; }

systemctl disable --now stratuxaudio-status 2>/dev/null || true
rm -f /etc/systemd/system/stratuxaudio-status.service
[ -f /usr/local/lib/stratuxaudio/webui/patch-webui.py ] && python3 /usr/local/lib/stratuxaudio/webui/patch-webui.py remove /opt/stratux/www || true
rm -rf /usr/local/lib/stratuxaudio
rm -f /boot/firmware/stratuxaudio.json
systemctl disable --now shairport-sync 2>/dev/null || true
rm -rf /etc/systemd/system/shairport-sync.service.d/stratuxaudio.conf
[ -f /etc/shairport-sync.conf.orig ] && mv /etc/shairport-sync.conf.orig /etc/shairport-sync.conf
rm -f /usr/local/bin/stratuxaudio-check /etc/stratuxaudio-version
apt-get purge -y shairport-sync 2>/dev/null || dpkg --purge shairport-sync || true
systemctl daemon-reload
echo "StratuxAudio removed (avahi-daemon left in place: Raspberry Pi OS ships it)."
echo "Turn the overlay back on: sudo overlayctl enable && sudo reboot"
