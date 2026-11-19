#!/usr/bin/python3
"""StratuxAudio status service.

Reads shairport-sync's metadata pipe (what's playing, who's playing it, volume,
cover art) and checks the audio side of the box (shairport running, USB dongle
present and in use, output level). Serves it read-only as JSON for the "Audio"
page in the Stratux web UI.

  GET /status.json   everything, as JSON
  GET /cover         current cover art (image), 404 if none
  POST /level        {"level_pct": 0-100}: set the dongle's output level
  POST /eq           {"bands_db": [7 x -12..12], "loudness": bool, "preset": str}
  POST /mono         {"mono": bool}: mix down to mono (restarts the AirPlay receiver)
  POST /output       {"card": id}: play through this sound card (restarts the receiver)

Settings are saved to /boot/firmware/stratuxaudio.json (the Stratux settings
partition, which stays writable with the overlay on) and applied again at start-up.

Standard library only. Port 8090.
"""

import base64
import cmath
import glob
import json
import math
import os
import re
import stat
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIPE = "/tmp/shairport-sync-metadata"
CONF = "/etc/shairport-sync.conf"
PORT = 8090
SAVED = "/boot/firmware/stratuxaudio.json"
RATE = 44100  # AirPlay 1 RTP frames per second

lock = threading.Lock()
now_playing = {
    "state": "idle",        # idle | playing | paused
    "title": "", "artist": "", "album": "", "genre": "",
    "client": "",           # the sending device's name
    "volume_pct": None,     # AirPlay volume slider, 0-100
    "duration_s": None, "position_s": None, "position_at": None,
    "cover_id": 0,
    "since": None,          # when the current state started
}
cover = {"data": None, "type": "image/jpeg"}


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def set_state(s):
    if now_playing["state"] != s:
        now_playing["state"] = s
        now_playing["since"] = ts()


def handle(typ, code, data):
    """One metadata item from shairport-sync."""
    text = lambda: data.decode("utf-8", "replace") if data else ""
    with lock:
        if typ == "core":
            key = {"minm": "title", "asar": "artist", "asal": "album", "asgn": "genre"}.get(code)
            if key:
                now_playing[key] = text()
        elif typ == "ssnc":
            if code in ("pbeg", "prsm"):
                set_state("playing")
                if code == "prsm" and now_playing["position_at"] is None and now_playing["position_s"] is not None:
                    now_playing["position_at"] = time.time()
            elif code == "pfls":
                # Pause or skip: freeze the progress clock.
                if now_playing["position_at"] is not None and now_playing["position_s"] is not None:
                    now_playing["position_s"] += time.time() - now_playing["position_at"]
                now_playing["position_at"] = None
                set_state("paused")
            elif code == "pend":
                set_state("idle")
                for k in ("title", "artist", "album", "genre", "client"):
                    now_playing[k] = ""
                now_playing.update(duration_s=None, position_s=None, position_at=None)
                cover["data"] = None
            elif code == "snam":
                now_playing["client"] = text()
            elif code == "pvol":
                try:
                    av = float(text().split(",")[0])   # -30..0, -144 = muted
                    now_playing["volume_pct"] = 0 if av <= -30 else round((av + 30) / 30 * 100)
                except ValueError:
                    pass
            elif code == "prgr":
                try:
                    start, cur, end = (int(x) for x in text().split("/"))
                    now_playing["duration_s"] = max(0, (end - start) / RATE)
                    now_playing["position_s"] = max(0, (cur - start) / RATE)
                    now_playing["position_at"] = time.time() if now_playing["state"] == "playing" else None
                except ValueError:
                    pass
            elif code == "PICT" and data:
                cover["data"] = data
                cover["type"] = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                now_playing["cover_id"] += 1


ITEM = re.compile(
    rb"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
    rb"(?:\s*<data encoding=\"base64\">\s*([A-Za-z0-9+/=\s]*?)</data>)?\s*</item>", re.S)


def reader():
    """Read the pipe forever; reopen when shairport closes it."""
    while True:
        try:
            if not os.path.exists(PIPE):
                os.mkfifo(PIPE, 0o644)
            elif not stat.S_ISFIFO(os.stat(PIPE).st_mode):
                time.sleep(10)
                continue
            buf = b""
            with open(PIPE, "rb", buffering=0) as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break   # writer closed; reopen
                    buf += chunk
                    while True:
                        m = ITEM.search(buf)
                        if not m:
                            break
                        typ = bytes.fromhex(m.group(1).decode()).decode("latin-1")
                        code = bytes.fromhex(m.group(2).decode()).decode("latin-1")
                        data = base64.b64decode(re.sub(rb"\s", b"", m.group(4))) if m.group(4) else b""
                        try:
                            handle(typ, code, data)
                        except Exception as e:  # never let one odd item stop the reader
                            print(f"{ts()} item {typ}/{code}: {e}", flush=True)
                        buf = buf[m.end():]
                    if len(buf) > 4_000_000:   # runaway: drop it
                        buf = b""
        except OSError as e:
            print(f"{ts()} pipe: {e}", flush=True)
            time.sleep(5)


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


_health = {"at": 0, "data": {}}


# The Pi's built-in audio (headphone jack, HDMI) isn't offered: the jack doesn't play
# under the Stratux image, and there's no HDMI in an airplane. USB adapters and sound
# HATs (I2S boards on the Pi's pins) are listed.
BUILTIN_DRIVERS = ("bcm2835", "vc4-hdmi", "vc4hdmi")


def list_cards():
    """Sound cards that can play: [{id, name, usbid, usb, playing}]."""
    names, drivers = {}, {}
    try:
        for line in open("/proc/asound/cards"):
            m = re.match(r"\s*(\d+) \[(\S+)\s*\]: (.*)", line)
            if m:
                names[m.group(1)] = m.group(3).split(" - ", 1)[-1].strip()
                drivers[m.group(1)] = m.group(3).split(" - ", 1)[0].strip().lower()
    except OSError:
        pass
    out = []
    for c in sorted(glob.glob("/proc/asound/card[0-9]*")):
        if not glob.glob(c + "/pcm*p"):
            continue
        n = c.rsplit("card", 1)[1]
        if drivers.get(n, "").startswith(BUILTIN_DRIVERS):
            continue
        try:
            cid = open(c + "/id").read().strip()
            usbid = open(c + "/usbid").read().strip() if os.path.exists(c + "/usbid") else ""
            playing = any("RUNNING" in open(f).read() for f in glob.glob(c + "/pcm*p/sub*/status"))
        except OSError:
            continue
        name = names.get(n, cid)
        out.append({"id": cid, "name": name, "usbid": usbid, "usb": bool(usbid), "playing": playing})
    return out


def conf_card():
    """Card id shairport-sync is set to play through (from output_device)."""
    try:
        m = re.search(r'^\s*output_device\s*=\s*"[^"]*CARD=([^,"]+)', open(CONF).read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def mixer_control(cid):
    ctl = run(["amixer", "-c", cid, "scontrols"]).splitlines()
    m = re.search(r"'(.*)'", ctl[0]) if ctl else None
    return m.group(1) if m else None


def health():
    """Audio-side checks, cached for 3 s."""
    if time.time() - _health["at"] < 3:
        return _health["data"]
    conf = open(CONF).read() if os.path.exists(CONF) else ""
    m = re.search(r'output_device\s*=\s*"([^"]*)"', conf)
    device = m.group(1) if m else ""
    m = re.search(r'^\s*name\s*=\s*"([^"]*)"', conf, re.M)
    name = m.group(1) if m else ""

    cards = list_cards()
    sel = conf_card()
    dongle = None
    for c in cards:
        if c["id"] == sel:
            level = None
            ctl = mixer_control(c["id"])
            if ctl:
                lm = re.search(r"\[(\d+)%\]", run(["amixer", "-c", c["id"], "sget", ctl]))
                level = int(lm.group(1)) if lm else None
            dongle = {"id": c["id"], "name": c["name"], "usbid": c["usbid"], "usb": c["usb"],
                      "in_use": c["playing"], "level_pct": level, "selected": True}

    ps = run(["ps", "-C", "shairport-sync", "-o", "%cpu=,rss="]).split()
    ver = open("/etc/stratuxaudio-version").read().strip() if os.path.exists("/etc/stratuxaudio-version") else ""
    data = {
        "version": ver,
        "speaker_name": name,
        "output_device": device,
        "output_card": sel,
        "cards": cards,
        "shairport": run(["systemctl", "is-active", "shairport-sync"]) or "unknown",
        "avahi": run(["systemctl", "is-active", "avahi-daemon"]) or "unknown",
        "shairport_cpu_pct": float(ps[0]) if len(ps) >= 2 else None,
        "shairport_mem_mb": round(int(ps[1]) / 1024, 1) if len(ps) >= 2 else None,
        "dongle": dongle,          # the selected output card, if it's plugged in
    }
    _health.update(at=time.time(), data=data)
    return data


def dongle_control():
    """(card id, mixer control) of the selected output card, or (None, None)."""
    cid = conf_card()
    if not cid or cid not in {c["id"] for c in list_cards()}:
        return None, None
    return cid, mixer_control(cid)


def set_level(pct):
    cid, ctl = dongle_control()
    if not ctl:
        return False
    out = subprocess.run(["amixer", "-c", cid, "sset", ctl, f"{pct}%", "unmute"],
                         capture_output=True, text=True, timeout=3)
    _health["at"] = 0   # show the new level right away
    return out.returncode == 0


def load_saved():
    try:
        with open(SAVED) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_level(pct):
    save_key(level_pct=pct, level_saved_at=ts())


def save_key(**kv):
    d = load_saved()
    d.update(kv)
    tmp = SAVED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SAVED)


# --- Equalizer ---------------------------------------------------------------------
# Seven bands: a low shelf, five bells, a high shelf. The sliders become standard
# "Audio EQ Cookbook" filters (R. Bristow-Johnson); their combined impulse response is
# written as a short WAV file and handed to shairport-sync's convolution stage over
# D-Bus, which switches it live, without a restart or a dropout.

EQ_BANDS = [(60, "lowshelf"), (150, "bell"), (400, "bell"), (1000, "bell"),
            (2500, "bell"), (6000, "bell"), (15000, "highshelf")]
EQ_MAX_DB = 12
EQ_PRESETS = {
    "Flat":       [0, 0, 0, 0, 0, 0, 0],
    "Rich":       [5, 3, 0, -1, 1, 2, 3],     # the "smile" most headphones apply over Bluetooth
    "Bass boost": [7, 4, 1, 0, 0, 0, 0],
    "Cockpit":    [-4, -1, 0, 2, 4, 3, 1],    # less engine rumble, clearer voices and detail
    "Vocal":      [-2, -1, 1, 3, 3, 1, 0],
}
IR_LEN = 4096          # 93 ms at 44.1 kHz: long enough for the 60 Hz shelf to settle
FS = 44100.0
DBUS = ["busctl", "--system"]
SP = ["org.gnome.ShairportSync", "/org/gnome/ShairportSync", "org.gnome.ShairportSync"]

eq_state = {"bands_db": [0] * len(EQ_BANDS), "loudness": False, "preset": "Flat",
            "gain_db": 0.0, "active": False, "error": "", "curve": []}


def biquad(kind, f0, gain_db, q=1.0):
    """Normalized (b0, b1, b2, a1, a2) for one band."""
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / FS
    cw, sw = math.cos(w0), math.sin(w0)
    if kind == "bell":
        alpha = sw / (2 * q)
        b = (1 + alpha * a, -2 * cw, 1 - alpha * a)
        aa = (1 + alpha / a, -2 * cw, 1 - alpha / a)
    else:
        alpha = sw / 2 * math.sqrt(2)          # shelf slope S = 1
        sa = 2 * math.sqrt(a) * alpha
        if kind == "lowshelf":
            b = (a * ((a + 1) - (a - 1) * cw + sa), 2 * a * ((a - 1) - (a + 1) * cw),
                 a * ((a + 1) - (a - 1) * cw - sa))
            aa = ((a + 1) + (a - 1) * cw + sa, -2 * ((a - 1) + (a + 1) * cw),
                  (a + 1) + (a - 1) * cw - sa)
        else:
            b = (a * ((a + 1) + (a - 1) * cw + sa), -2 * a * ((a - 1) + (a + 1) * cw),
                 a * ((a + 1) + (a - 1) * cw - sa))
            aa = ((a + 1) - (a - 1) * cw + sa, 2 * ((a - 1) - (a + 1) * cw),
                  (a + 1) - (a - 1) * cw - sa)
    return (b[0] / aa[0], b[1] / aa[0], b[2] / aa[0], aa[1] / aa[0], aa[2] / aa[0])


def eq_filters(bands_db):
    return [biquad(k, f, g) for (f, k), g in zip(EQ_BANDS, bands_db) if g]


def eq_curve(bands_db, points=60):
    """[(Hz, dB)] of the combined curve, 20 Hz - 20 kHz, log spaced."""
    fl = eq_filters(bands_db)
    out = []
    for i in range(points):
        f = 20 * (1000 ** (i / (points - 1)))
        z = cmath.exp(-1j * 2 * math.pi * f / FS)
        h = 1
        for b0, b1, b2, a1, a2 in fl:
            h *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
        out.append((round(f), round(20 * math.log10(max(abs(h), 1e-9)), 2)))
    return out


def eq_impulse(bands_db):
    x = [1.0] + [0.0] * (IR_LEN - 1)
    for b0, b1, b2, a1, a2 in eq_filters(bands_db):
        y, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
        for s in x:
            o = b0 * s + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, s, y1, o
            y.append(o)
        x = y
    fade = IR_LEN // 4                        # smooth the truncated tail
    for i in range(fade):
        x[IR_LEN - fade + i] *= 0.5 * (1 + math.cos(math.pi * i / fade))
    return x


def write_wav_float(path, mono):
    """32-bit float stereo WAV (both channels the same filter)."""
    data = b"".join(struct.pack("<ff", s, s) for s in mono)
    fmt = struct.pack("<HHIIHH", 3, 2, int(FS), int(FS) * 8, 8, 32)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<II", 4, len(mono))
            + b"data" + struct.pack("<I", len(data)) + data)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def sp_set(prop, sig, value):
    r = subprocess.run(DBUS + ["set-property", "--"] + SP + [prop, sig, str(value)],
                       capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise RuntimeError(f"{prop}: {r.stderr.strip() or r.stdout.strip()}")


_ir_flip = [0]
eq_lock = threading.Lock()


def apply_eq(bands_db, loudness):
    """Build the filter and switch shairport-sync to it, live."""
    with eq_lock:
        flat = not any(bands_db)
        gain = 0.0
        if not flat:
            peak = max(db for _, db in eq_curve(bands_db, 240))
            gain = -max(0.0, round(peak + 0.5, 1))   # headroom: never clip
            _ir_flip[0] ^= 1                    # new file name, so shairport reloads it
            path = f"/tmp/stratuxaudio-eq-{_ir_flip[0]}.wav"
            write_wav_float(path, eq_impulse(bands_db))
            sp_set("ConvolutionImpulseResponseFile", "s", path)
            sp_set("ConvolutionGain", "d", gain)
        sp_set("Convolution", "b", "false" if flat else "true")
        sp_set("Loudness", "b", "true" if loudness else "false")
        eq_state.update(bands_db=list(bands_db), loudness=bool(loudness), gain_db=gain,
                        active=not flat, error="", curve=eq_curve(bands_db, 48))


def shairport_pid():
    return run(["systemctl", "show", "-p", "MainPID", "--value", "shairport-sync"])


def keep_settings_applied():
    """At start-up, when the dongle reappears and when shairport restarts:
    put the saved level and equalizer back."""
    last_level, last_pid = None, None
    while True:
        saved = load_saved()
        pct = saved.get("level_pct")
        cid, ctl = dongle_control()
        key = (cid, pct)
        if ctl and key != last_level:
            # The C-Media "Auto Gain Control" switch is meant for the mic; keep it off.
            run(["amixer", "-c", cid, "sset", "Auto Gain Control", "off"])
            if pct is None or set_level(int(pct)):
                if pct is not None:
                    print(f"{ts()} output level {pct}% applied to {cid}", flush=True)
                last_level = key
        elif not ctl:
            last_level = None   # dongle unplugged: apply again when it's back
        try:
            check_output()
        except Exception as e:
            print(f"{ts()} output check: {e}", flush=True)
        pid = shairport_pid()
        if pid and pid != "0" and pid != last_pid:
            eq = saved.get("eq") or {}
            bands = eq.get("bands_db") or [0] * len(EQ_BANDS)
            try:
                time.sleep(2)               # let shairport finish starting
                apply_eq(bands, eq.get("loudness", False))
                eq_state["preset"] = eq.get("preset", "")
                print(f"{ts()} equalizer applied: {bands} loudness={eq.get('loudness', False)}", flush=True)
                last_pid = pid
            except Exception as e:
                eq_state["error"] = str(e)
                print(f"{ts()} equalizer not applied yet: {e}", flush=True)
        time.sleep(10)


# --- Mono ----------------------------------------------------------------------------
# shairport-sync's playback_mode can't change live, so a switch rewrites that one line
# of its config and restarts it (about a 2 s gap). With the overlay on, /etc goes back
# to the installed file at each boot; the saved choice is put back then.

def conf_mono():
    try:
        m = re.search(r'^\s*playback_mode\s*=\s*"([^"]*)"', open(CONF).read(), re.M)
    except OSError:
        return None
    return (m.group(1) == "mono") if m else False


def set_mono(mono):
    text = open(CONF).read()
    line = f'  playback_mode = "{"mono" if mono else "stereo"}";   // set from the Audio page'
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if re.match(r"\s*playback_mode\s*=", l):
            lines[i] = line
            break
    else:                            # older config without the line: add it to general
        i = next(i for i, l in enumerate(lines) if l.strip().startswith("general"))
        j = next(j for j in range(i, len(lines)) if "{" in lines[j])
        lines.insert(j + 1, line)
    text = "\n".join(lines)
    with open(CONF, "r+") as f:      # in place: the service may write the file, not /etc
        f.seek(0)
        f.write(text)
        f.truncate()
    restart_shairport()


def restart_shairport():
    r = subprocess.run(["systemctl", "restart", "shairport-sync"], capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "restart failed")
    _health["at"] = 0


def set_output(cid):
    """Point shairport-sync at sound card cid (one line of its config) and restart it."""
    if cid not in {c["id"] for c in list_cards()}:
        raise ValueError(f"no sound card {cid!r}")
    line = f'  output_device = "plughw:CARD={cid},DEV=0";   // set from the Audio page'
    lines = open(CONF).read().split("\n")
    for i, l in enumerate(lines):
        if re.match(r"\s*output_device\s*=", l):
            lines[i] = line
            break
    else:
        raise RuntimeError(f"no output_device line in {CONF}")
    with open(CONF, "r+") as f:      # in place: the service may write the file, not /etc
        f.seek(0)
        f.write("\n".join(lines))
        f.truncate()
    restart_shairport()


def check_output():
    """Keep shairport pointed at a card that's actually there.
    1. the card chosen on the Audio page, if plugged in (matched by USB id, then name);
    2. otherwise leave a working setting alone;
    3. otherwise (the set card is gone, or it's the Pi's built-in audio) fall back to
       any USB sound card."""
    cards = list_cards()
    by_id = {c["id"]: c for c in cards}
    cur = conf_card()
    want = load_saved().get("output") or {}
    target = None
    if want:
        for c in cards:
            if (want.get("usbid") and c["usbid"] == want["usbid"]) or \
               (not want.get("usbid") and c["id"] == want.get("id")):
                target = c["id"]
                break
    if target is None:
        if cur in by_id:
            return
        usb = [c for c in cards if c["usb"]]
        if not usb:
            return
        target = usb[0]["id"]
        why = "set card missing; falling back to"
    else:
        why = "restoring saved choice"
    if target == cur:
        return
    if cur in by_id and by_id[cur]["playing"]:
        return                       # don't cut off music; try again on the next pass
    set_output(target)
    print(f"{ts()} output: {why} {target} (was {cur})", flush=True)


def keep_mono_applied():
    want = load_saved().get("mono")
    if want is not None and conf_mono() is not None and conf_mono() != bool(want):
        try:
            set_mono(bool(want))
            print(f"{ts()} mono={bool(want)} applied", flush=True)
        except Exception as e:
            print(f"{ts()} mono not applied: {e}", flush=True)

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")   # page is served from port 80
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):   # CORS preflight for the POSTs
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > 4000:
            raise ValueError("too long")
        return json.loads(self.rfile.read(n) or b"{}")

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_POST(self):
        path = self.path.split("?")[0]
        who = self.client_address[0]
        try:
            body = self._body()
        except (ValueError, TypeError):
            return self._json(400, {"error": "send JSON"})

        if path == "/level":
            try:
                pct = int(body["level_pct"])
                if not 0 <= pct <= 100:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": "send {\"level_pct\": 0-100}"})
            if not set_level(pct):
                return self._json(503, {"error": "no USB sound dongle"})
            saved = True
            try:
                save_level(pct)
            except OSError as e:
                saved = False
                print(f"{ts()} couldn't save level: {e}", flush=True)
            print(f"{ts()} output level set to {pct}% from {who} (saved={saved})", flush=True)
            return self._json(200, {"level_pct": pct, "saved": saved})

        if path == "/eq":
            try:
                bands = [max(-EQ_MAX_DB, min(EQ_MAX_DB, round(float(x) * 2) / 2)) for x in body["bands_db"]]
                if len(bands) != len(EQ_BANDS):
                    raise ValueError
                loud = bool(body.get("loudness", False))
                preset = str(body.get("preset", ""))[:40]
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"error": f"send {{\"bands_db\": [{len(EQ_BANDS)} numbers]}}"})
            try:
                apply_eq(bands, loud)
            except Exception as e:
                eq_state["error"] = str(e)
                print(f"{ts()} equalizer failed: {e}", flush=True)
                return self._json(503, {"error": f"couldn't apply: {e}"})
            eq_state["preset"] = preset
            saved = True
            try:
                save_key(eq={"bands_db": bands, "loudness": loud, "preset": preset}, eq_saved_at=ts())
            except OSError:
                saved = False
            print(f"{ts()} equalizer set to {bands} loudness={loud} preset={preset!r} from {who}", flush=True)
            return self._json(200, {"eq": eq_state, "saved": saved})

        if path == "/mono":
            mono = bool(body.get("mono"))
            try:
                set_mono(mono)
                save_key(mono=mono, mono_saved_at=ts())
            except Exception as e:
                print(f"{ts()} mono failed: {e}", flush=True)
                return self._json(503, {"error": f"couldn't switch: {e}"})
            print(f"{ts()} mono={mono} from {who}", flush=True)
            return self._json(200, {"mono": mono})

        if path == "/output":
            cid = str(body.get("card", ""))[:40]
            card = next((c for c in list_cards() if c["id"] == cid), None)
            if not card:
                return self._json(400, {"error": f"no sound card {cid!r}"})
            try:
                set_output(cid)
                save_key(output={"id": cid, "usbid": card["usbid"], "name": card["name"]},
                         output_saved_at=ts())
            except Exception as e:
                print(f"{ts()} output switch failed: {e}", flush=True)
                return self._json(503, {"error": f"couldn't switch: {e}"})
            print(f"{ts()} output set to {cid} ({card['name']}) from {who}", flush=True)
            return self._json(200, {"card": cid})

        return self._send(404, b"not found", "text/plain")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status.json":
            with lock:
                np = dict(now_playing)
            if np["position_at"] is not None and np["position_s"] is not None:
                np["position_s"] += time.time() - np["position_at"]
                if np["duration_s"]:
                    np["position_s"] = min(np["position_s"], np["duration_s"])
            np.pop("position_at")
            np["has_cover"] = cover["data"] is not None
            eq = dict(eq_state)
            eq["bands"] = [{"hz": f, "db": g} for (f, _), g in zip(EQ_BANDS, eq["bands_db"])]
            eq["presets"] = EQ_PRESETS
            eq["max_db"] = EQ_MAX_DB
            audio = {"eq": eq, "mono": conf_mono()}
            body = json.dumps({"time": ts(), "now_playing": np, "health": health(),
                               "audio": audio}).encode()
            self._send(200, body, "application/json")
        elif path == "/cover" and cover["data"]:
            self._send(200, cover["data"], cover["type"])
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass   # quiet: the page polls every couple of seconds


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    try:
        check_output()
    except Exception as e:
        print(f"{ts()} output check: {e}", flush=True)
    keep_mono_applied()
    threading.Thread(target=keep_settings_applied, daemon=True).start()
    print(f"{ts()} stratuxaudio-status listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
