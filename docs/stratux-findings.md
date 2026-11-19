# How Stratux is built: what matters for an AirPlay add-on

Read from `github.com/stratux/stratux`, master at commit `db61af4` (2026-10-29).
Latest release is v2.0; v2.0-pre6 is the newest pre-release (2026-10-21).
The Europe edition (`b3nn0/stratux`) now redirects to this repository: one main line.

## The system

- **OS:** Raspberry Pi OS (Debian Bookworm), 64-bit (`arm_64bit=1`), built with the
  official `pi-gen` image builder (`image_build/`). Stratux itself is one `.deb` package.
- **Many packages removed** to shrink the image (`image_build/stage2/10-stratux/03-run.sh`):
  no compiler, no git, no make. Anything we add must come as ready-built packages.
- **CPU slowed on purpose:** `config.txt` sets `arm_freq=900`, `core_freq=450`,
  `sdram_freq=450` to reduce radio interference with the ADS-B receivers (Stratux issue #573).
  The audio service has to fit in what's left after dump1090 (1090 MHz) and dump978 (978 MHz UAT).
- **Audio is on:** `dtparam=audio=on`, so the Pi 3/4 headphone jack is enabled (but it doesn't play on the bench unit; see below).
  The Pi 5 has no jack: it needs a USB sound dongle or an add-on DAC board.
- **No internet in flight;** system clock set from GPS (timesyncd disabled).

## Read-only root filesystem (overlay)

- Root runs under an overlay (`/sbin/init-overlay`, `/sbin/overlayctl`): changes go to RAM
  and are **lost at reboot** unless the overlay is off.
- `overlayctl disable` + reboot gives a writable system; the web setting
  "Persistent Logging" does the same. `overlayctl enable` turns protection back on.
- To install: turn the overlay off, reboot, install, turn it back on, reboot.

## How Stratux installs updates (`debian/stratux-pre-start.sh`)

- Files uploaded through the Stratux web page land in `/boot/firmware/StratuxUpdates/`.
- At the next start it accepts two kinds:
  - `update*stratux*v*.sh`: a shell script, run as root on the writable system;
  - `stratux*.deb`: installed with `dpkg -i --force-depends` (missing dependencies are
    **not** fetched).
- It turns the overlay off, reboots, installs, turns the overlay back on, and reboots.
- **This is our way to install from a phone in the hangar with no internet:** an
  `update-stratuxaudio-v1.sh` script that carries shairport-sync and its dependencies
  as built packages and installs them offline. (The name has to match the mask
  `update*stratux*v*.sh`; check the exact match on a real unit.)
- A normal Stratux update replaces only the Stratux package, so our add-on should survive it.
  Re-flashing the SD card wipes it: reinstall afterward.

## Wi-Fi network

- Stratux runs its access point on a virtual interface **`ap0`**, address **192.168.10.1/24**,
  using `wpa_supplicant` in AP mode (not hostapd) and `dnsmasq` for DHCP
  (`files/interfaces`, `debian/stratux-wifi.sh`).
- Modes: AP only, Wi-Fi Direct, or AP+Client (internet pass-through over `wlan0`;
  useful for installing over the network on the ground).
- No firewall on `ap0` in the image; the only iptables rules are for AP+Client NAT.
- **mDNS (how the iPhone finds AirPlay speakers):** Raspberry Pi OS normally ships
  `avahi-daemon`; Stratux doesn't remove it explicitly. **Check on a real unit** that it's
  installed, running, and answering on `ap0`.

## Effect on the design

1. **shairport-sync** as the AirPlay receiver, installed from Debian Bookworm packages
   (arm64). Start with **AirPlay 1** (classic): lighter on the CPU, no extra timing
   service. AirPlay 2 needs `nqptp` and a newer shairport-sync build than Bookworm
   ships; consider it later.
2. Run it at low priority (`Nice=`, `CPUWeight=`, `CPUQuota=` in the systemd unit) so
   ADS-B decoding always comes first. Verify that message rates on the Stratux status
   page don't fall while music plays.
3. Advertise as a fixed name, "Stratux Audio" (changeable with `--name`). It shows up on
   whatever network the Stratux is on: its own Wi-Fi, or the client network in AP+Client mode.
4. Installation: (a) by hand over SSH with AP+Client internet for development;
   (b) offline update script uploaded through the Stratux web page for users.
5. Don't write to the root filesystem at run time (the overlay discards it anyway). The one
   setting that has to survive a reboot, the output level from the Audio page, goes to
   `/boot/firmware/stratuxaudio.json`: the settings partition Stratux itself uses for
   `stratux.conf`, which stays writable with the overlay on.

## Found on the bench (2026-11-18, Pi 3 Model B, Stratux v2.0 line)

- **The Pi's own headphone jack doesn't play** under the Stratux image: `speaker-test` on
  `Headphones` fails with "Input/output error" and the kernel logs a VCHI error from
  `bcm2835_audio`. Not proven why (the Stratux clock settings in `config.txt` are a
  suspect). A USB sound dongle works fine; that's why the installer looks for one.
- **The clock can be days off** until the GPS gets a fix (Stratux turns NTP off), and apt
  then rejects the package lists. The installer checks the time against a web server over
  plain HTTP and sets it if it's more than an hour out.
- AirPlay 1 is enough for music from an iPhone; AirPlay 2 hasn't been needed.
