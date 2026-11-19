#!/usr/bin/python3
"""Add (or remove) the StratuxAudio "Audio" page in the Stratux web UI.

  patch-webui.py install [WWW_DIR]   copy the page files in and wire them up
  patch-webui.py remove  [WWW_DIR]   take them out again

WWW_DIR defaults to /opt/stratux/www. Safe to run more than once: every line it
adds carries a "stratuxaudio" marker, and it only adds what isn't there yet.
A Stratux update replaces the web UI, so run install.sh again afterward.
"""

import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "stratuxaudio"
FILES = {  # repo file -> path under the web root
    "audio.html": "plates/audio.html",
    "audio-help.html": "plates/audio-help.html",
    "js/audio.js": "plates/js/audio.js",
}

MENU = ('\t\t\t\t\t<a class="list-group-item" href="#/audio"><i class="fa fa-music"></i>    Audio '
        '<i class="fa fa-chevron-right pull-right"></i></a><!-- stratuxaudio -->\n')
SCRIPT = '\t<script src="plates/js/audio.js"></script><!-- stratuxaudio -->\n'
STATE = ("\t\t.state('audio', { url: '/audio', templateUrl: 'plates/audio.html', "
         "controller: 'AudioCtrl', reloadOnSearch: false }) // stratuxaudio\n")


def read(p):
    with open(p, encoding="utf-8", newline="") as f:   # keep the file's own line endings
        return f.read()


def write(p, s):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    shutil.copymode(p, tmp)
    os.replace(tmp, p)


def strip_marked(s):
    return "".join(l for l in s.splitlines(True) if MARK not in l)


def strip_cache(s):
    """Manifest without our entries (cache lines can't carry a marker comment)."""
    ours = {"/" + dst for dst in FILES.values()}
    return "".join(l for l in s.splitlines(True)
                   if MARK not in l and l.strip() not in ours and not l.startswith("# removed "))


def insert_before(s, pattern, text, what):
    m = re.search(pattern, s, re.M)
    if not m:
        sys.exit(f"patch-webui: couldn't find where to add the {what}; this Stratux version's web UI "
                 f"is laid out differently. Nothing changed.")
    return s[:m.start()] + text + s[m.start():]


def install(www):
    index, main, cache = (os.path.join(www, p) for p in ("index.html", "js/main.js", "stratux.appcache"))
    for p in (index, main):
        if not os.path.exists(p):
            sys.exit(f"patch-webui: {p} not found; is this a Stratux?")

    # Work out all three edits first, so a surprise leaves nothing half done.
    s_index = strip_marked(read(index))
    # Menu: after the Map entry (or, failing that, before the Developer block).
    m = re.search(r'^.*href="#/map".*\n', s_index, re.M)
    s_index = (s_index[:m.end()] + MENU + s_index[m.end():]) if m else \
        insert_before(s_index, r'^\s*<div ng-show="DeveloperMode">', MENU, "menu item")
    s_index = insert_before(s_index, r"^\s*</head>", SCRIPT, "script tag")

    s_main = strip_marked(read(main))
    s_main = insert_before(s_main, r"^\s*\.state\('developer'", STATE, "page route")

    for src, dst in FILES.items():
        d = os.path.join(www, dst)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(os.path.join(HERE, src), d)
        os.chmod(d, 0o644)
    write(index, s_index)
    write(main, s_main)

    # Offline cache: list our files and change the manifest, so phones and tablets
    # that cached the old page fetch the new index.html and main.js.
    if os.path.exists(cache):
        lines = strip_cache(read(cache)).splitlines(True)
        net = next((i for i, l in enumerate(lines) if l.strip() == "NETWORK:"), len(lines))
        at = net
        while at > 0 and not lines[at - 1].strip():
            at -= 1          # go in above the blank line before NETWORK:
        lines[at:at] = [f"/{dst}\n" for dst in FILES.values()]
        lines.append(f"# {MARK} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        write(cache, "".join(lines))
    print("patch-webui: Audio page added to the Stratux menu")


def remove(www):
    index, main, cache = (os.path.join(www, p) for p in ("index.html", "js/main.js", "stratux.appcache"))
    for p in (index, main):
        if os.path.exists(p):
            write(p, strip_marked(read(p)))
    if os.path.exists(cache):
        s = strip_cache(read(cache))
        s += f"# removed {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"   # change the manifest so clients refresh
        write(cache, s)
    for dst in FILES.values():
        p = os.path.join(www, dst)
        if os.path.exists(p):
            os.remove(p)
    print("patch-webui: Audio page removed")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "remove"):
        sys.exit(__doc__)
    www = sys.argv[2] if len(sys.argv) > 2 else "/opt/stratux/www"
    install(www) if sys.argv[1] == "install" else remove(www)
