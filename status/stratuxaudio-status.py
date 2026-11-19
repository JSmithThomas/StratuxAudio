#!/usr/bin/python3
"""StratuxAudio status service.

Reads shairport-sync's metadata pipe (what's playing, who's playing it, volume,
cover art) and checks the audio side of the box (shairport running, USB dongle
present and in use, output level). Serves it read-only as JSON for the "Audio"
page in the Stratux web UI.

  GET /status.json   everything, as JSON
  GET /cover         current cover art (image), 404 if none
  POST /level        {"level_pct": 0-100}: set the dongle's output level

The level is saved to /boot/firmware/stratuxaudio.json (the Stratux settings
partition, which stays writable with the overlay on) and applied again at start-up.

Standard library only. Port 8090.
"""

import base64
import glob
import json
import os
import re
import stat
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


def health():
    """Audio-side checks, cached for 3 s."""
    if time.time() - _health["at"] < 3:
        return _health["data"]
    conf = open(CONF).read() if os.path.exists(CONF) else ""
    m = re.search(r'output_device\s*=\s*"([^"]*)"', conf)
    device = m.group(1) if m else ""
    m = re.search(r'^\s*name\s*=\s*"([^"]*)"', conf, re.M)
    name = m.group(1) if m else ""

    dongle = None
    for c in sorted(glob.glob("/proc/asound/card[0-9]*")):
        if os.path.exists(c + "/usbid"):
            cid = open(c + "/id").read().strip()
            n = c.rsplit("card", 1)[1]
            longname = ""
            for line in open("/proc/asound/cards"):
                if line.strip().startswith(n + " ["):
                    longname = line.split(" - ", 1)[-1].strip()
            st = ""
            p = c + "/pcm0p/sub0/status"
            if os.path.exists(p):
                st = open(p).read()
            level = None
            ctl = run(["amixer", "-c", cid, "scontrols"]).splitlines()
            if ctl:
                cm = re.search(r"'(.*)'", ctl[0])
                if cm:
                    lm = re.search(r"\[(\d+)%\]", run(["amixer", "-c", cid, "sget", cm.group(1)]))
                    level = int(lm.group(1)) if lm else None
            dongle = {"id": cid, "name": longname, "usbid": open(c + "/usbid").read().strip(),
                      "in_use": "RUNNING" in st, "level_pct": level,
                      "selected": f"CARD={cid}," in device}
            break

    ps = run(["ps", "-C", "shairport-sync", "-o", "%cpu=,rss="]).split()
    ver = open("/etc/stratuxaudio-version").read().strip() if os.path.exists("/etc/stratuxaudio-version") else ""
    data = {
        "version": ver,
        "speaker_name": name,
        "output_device": device,
        "shairport": run(["systemctl", "is-active", "shairport-sync"]) or "unknown",
        "avahi": run(["systemctl", "is-active", "avahi-daemon"]) or "unknown",
        "shairport_cpu_pct": float(ps[0]) if len(ps) >= 2 else None,
        "shairport_mem_mb": round(int(ps[1]) / 1024, 1) if len(ps) >= 2 else None,
        "dongle": dongle,
    }
    _health.update(at=time.time(), data=data)
    return data


def dongle_control():
    """(card id, mixer control) of the USB dongle, or (None, None)."""
    for c in sorted(glob.glob("/proc/asound/card[0-9]*")):
        if os.path.exists(c + "/usbid"):
            cid = open(c + "/id").read().strip()
            ctl = run(["amixer", "-c", cid, "scontrols"]).splitlines()
            m = re.search(r"'(.*)'", ctl[0]) if ctl else None
            return (cid, m.group(1)) if m else (cid, None)
    return None, None


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
    d = load_saved()
    d.update(level_pct=pct, level_saved_at=ts())
    tmp = SAVED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SAVED)


def apply_saved_level():
    """At start-up, and whenever the dongle reappears: put the saved level back."""
    last = None
    while True:
        pct = load_saved().get("level_pct")
        cid, ctl = dongle_control()
        key = (cid, pct)
        if pct is not None and ctl and key != last:
            if set_level(int(pct)):
                print(f"{ts()} output level {pct}% applied to {cid}", flush=True)
                last = key
        elif not ctl:
            last = None   # dongle unplugged: apply again when it's back
        time.sleep(10)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")   # page is served from port 80
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):   # CORS preflight for POST /level
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path.split("?")[0] != "/level":
            return self._send(404, b"not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 1000:
                raise ValueError("too long")
            pct = int(json.loads(self.rfile.read(n) or b"{}")["level_pct"])
            if not 0 <= pct <= 100:
                raise ValueError("out of range")
        except (ValueError, KeyError, TypeError):
            return self._send(400, b'{"error": "send {\\"level_pct\\": 0-100}"}', "application/json")
        if not set_level(pct):
            return self._send(503, b'{"error": "no USB sound dongle"}', "application/json")
        saved = True
        try:
            save_level(pct)
        except OSError as e:
            saved = False
            print(f"{ts()} couldn't save level: {e}", flush=True)
        print(f"{ts()} output level set to {pct}% from {self.client_address[0]} (saved={saved})", flush=True)
        self._send(200, json.dumps({"level_pct": pct, "saved": saved}).encode(), "application/json")

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
            body = json.dumps({"time": ts(), "now_playing": np, "health": health()}).encode()
            self._send(200, body, "application/json")
        elif path == "/cover" and cover["data"]:
            self._send(200, cover["data"], cover["type"])
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass   # quiet: the page polls every couple of seconds


if __name__ == "__main__":
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=apply_saved_level, daemon=True).start()
    print(f"{ts()} stratuxaudio-status listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
