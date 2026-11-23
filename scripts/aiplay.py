"""AI playtest: the real Godot client on a virtual screen, played through synthetic mouse and keyboard.

An agent that can read images plays the game a person plays: it looks at screenshots and clicks, scrolls and
types. Nothing here talks to the world; the client does, over protocol 2, as it does for a person. So what
the agent can and can't see or do is the client's own UI (plan/ai-playtest.md).

    python scripts/aiplay.py start DIR --url wss://POD-8765.proxy.runpod.net/ --inhabit 29 --token T
    python scripts/aiplay.py shot DIR                      # save the screen, print the PNG's path
    python scripts/aiplay.py click DIR X Y --shot 1.5      # click, wait 1.5 s, then a screenshot
    python scripts/aiplay.py click DIR X Y --shot --crop 1030,0,410,900   # the screenshot is only that part
    python scripts/aiplay.py scroll DIR X Y 5              # 5 wheel notches down at X, Y (negative: up)
    python scripts/aiplay.py type DIR "hello there" --shot
    python scripts/aiplay.py key DIR Return
    python scripts/aiplay.py key DIR ctrl+a                          # a key with ctrl, shift, alt or cmd held: here, select the Talk line whole
    python scripts/aiplay.py hold DIR d 2.5 --shot         # hold a key down for 2.5 s, as a person walks with W A S D; "w+d 2" holds both
    python scripts/aiplay.py resize DIR 1440x1100          # make the window bigger or smaller, as a person drags it
    python scripts/aiplay.py stop DIR                      # close the client (the player leaves) and the screen

--relay is for a machine whose outbound TLS passes through a gateway with its own CA (a cloud container):
Godot trusts only its bundled CAs, so the client connects to a local plain-WebSocket relay and Python, which
trusts the system store, makes the wss:// leg.

DIR holds session.json, input.jsonl (every command with its wall time), shots/NNNN.png and, with --record,
session.mp4. Needs Xvfb, godot 4.3, python-xlib and pillow (and ffmpeg for --record).
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
from urllib.parse import urlparse

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SCREEN = (1600, 1600)  # the virtual screen; the client's window sits at its top left and can grow to this
BUTTONS = {"left": 1, "middle": 2, "right": 3}


def _session(d: str) -> dict:
    with open(os.path.join(d, "session.json")) as f:
        return json.load(f)


def _log(d: str, **rec):
    with open(os.path.join(d, "input.jsonl"), "a") as f:
        f.write(json.dumps({"wall": round(time.time(), 3), **rec}, sort_keys=True) + "\n")


def _display(s: dict):
    from Xlib import display
    return display.Display(f":{s['display']}")


def _free_display() -> int:
    return next(n for n in range(90, 200) if not os.path.exists(f"/tmp/.X11-unix/X{n}"))


def _size(text: str) -> tuple[int, int]:
    w, h = (int(x) for x in text.split("x"))
    if not (640 <= w <= SCREEN[0] and 480 <= h <= SCREEN[1]):
        raise SystemExit(f"size {text}: between 640x480 and {SCREEN[0]}x{SCREEN[1]}")
    return w, h


def start(a):
    os.makedirs(os.path.join(a.dir, "shots"), exist_ok=True)
    if os.path.exists(os.path.join(a.dir, "session.json")):
        raise SystemExit(f"{a.dir} already holds a session: stop it, or use a new directory")
    w, h = _size(a.size)
    n = _free_display()
    logs = open(os.path.join(a.dir, "client.log"), "ab")
    run = lambda cmd, **kw: subprocess.Popen(cmd, stdout=logs, stderr=logs, start_new_session=True, **kw).pid
    pids = {"xvfb": run(["Xvfb", f":{n}", "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"])}
    for _ in range(50):
        if os.path.exists(f"/tmp/.X11-unix/X{n}"):
            break
        time.sleep(0.1)
    else:
        raise SystemExit(f"Xvfb did not start; see {a.dir}/client.log")
    url = a.url
    if a.relay:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        pids["relay"] = run([sys.executable, os.path.abspath(__file__), "relay", str(port), a.url])
        url = f"ws://127.0.0.1:{port}{urlparse(a.url).path or '/'}"
        for _ in range(50):  # the client never retries a refused connect, so wait until the relay listens
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit(f"the relay did not start; see {a.dir}/client.log")
    cmd = ["godot", "--path", os.path.join(REPO, "client", "godot"), "--resolution", f"{w}x{h}", "--position", "0,0",
           "--", "--url", url, "--inhabit", str(a.inhabit)]
    env = {**os.environ, "DISPLAY": f":{n}"}
    pids["godot"] = run(cmd + (["--token", a.token] if a.token else []), env=env)
    if a.record:  # a small film of the whole session: what the agent saw, for a person to check
        pids["ffmpeg"] = run(["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-framerate", "2", "-video_size",
                              f"{SCREEN[0]}x{SCREEN[1]}", "-i", f":{n}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                              os.path.join(a.dir, "session.mp4")])
    s = {"display": n, "size": [w, h], "pids": pids, "url": a.url, "inhabit": a.inhabit, "started": round(time.time(), 3)}
    with open(os.path.join(a.dir, "session.json"), "w") as f:
        json.dump(s, f, indent=1)
    _log(a.dir, cmd="start", url=a.url, inhabit=a.inhabit, size=[w, h])
    time.sleep(a.wait)  # the client connects and draws its first view
    print(shot(a.dir, s))


def stop(a):
    s = _session(a.dir)
    for name in ("ffmpeg", "godot", "relay", "xvfb"):  # ffmpeg first, so the film is closed properly
        pid = s["pids"].get(name)
        if pid:
            try:
                os.killpg(pid, signal.SIGINT if name == "ffmpeg" else signal.SIGTERM)
                time.sleep(1.0 if name == "ffmpeg" else 0.3)
            except ProcessLookupError:
                pass
    _log(a.dir, cmd="stop")
    os.replace(os.path.join(a.dir, "session.json"), os.path.join(a.dir, "session.stopped.json"))


async def relay(port: int, url: str):
    """Plain WebSocket on 127.0.0.1:port to `url`, byte for byte, with the upgrade request's Host rewritten."""
    u = urlparse(url)
    tls = u.scheme == "wss"

    async def pipe(r, w):
        try:
            while data := await r.read(1 << 16):
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    async def handle(r, w):
        try:
            ur, uw = await asyncio.open_connection(u.hostname, u.port or (443 if tls else 80),
                                                   ssl=ssl.create_default_context() if tls else None)
            head = await r.readuntil(b"\r\n\r\n")
        except (OSError, asyncio.IncompleteReadError) as e:
            print(f"relay: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            w.close()
            return
        host = u.hostname + (f":{u.port}" if u.port else "")
        uw.write(re.sub(rb"(?im)^Host:[^\r]*", b"Host: " + host.encode(), head, count=1))
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    async with await asyncio.start_server(handle, "127.0.0.1", port, limit=1 << 16) as server:
        await server.serve_forever()


def resize(a):
    """Resize the client's window. There is no window manager, so the X server applies it at once."""
    s = _session(a.dir)
    w, h = _size(a.size)
    dpy = _display(s)
    wins = [c for c in dpy.screen().root.query_tree().children if c.get_attributes().map_state == 2]  # viewable
    if not wins:
        raise SystemExit("the client has no window (did it exit? see client.log)")
    max(wins, key=lambda c: c.get_geometry().width * c.get_geometry().height).configure(x=0, y=0, width=w, height=h)
    dpy.sync()
    dpy.close()
    s["size"] = [w, h]
    with open(os.path.join(a.dir, "session.json"), "w") as f:
        json.dump(s, f, indent=1)
    _log(a.dir, cmd="resize", size=[w, h])
    time.sleep(1.0)
    print(shot(a.dir, s))


def _crop(text: str | None):
    """X,Y,W,H in window pixels. A position read off a cropped image is X, Y further right and down."""
    return [int(x) for x in text.split(",")] if text else None


def shot(d: str, s: dict, crop=None) -> str:
    """The client's window, or a crop of it, as the next numbered PNG."""
    from PIL import Image
    from Xlib import X
    dpy = _display(s)
    w, h = s["size"]
    x, y, cw, ch = crop or (0, 0, w, h)
    raw = dpy.screen().root.get_image(x, y, cw, ch, X.ZPixmap, 0xFFFFFFFF)
    dpy.close()
    n = len(os.listdir(os.path.join(d, "shots"))) + 1
    path = os.path.abspath(os.path.join(d, "shots", f"{n:04d}.png"))
    Image.frombytes("RGB", (cw, ch), raw.data, "raw", "BGRX").save(path)
    _log(d, cmd="shot", n=n, crop=crop)
    return path


def _fake(dpy, kind, detail=0, **kw):
    from Xlib.ext import xtest
    xtest.fake_input(dpy, kind, detail, **kw)
    dpy.sync()


def _move(dpy, x: int, y: int):
    from Xlib import X
    _fake(dpy, X.MotionNotify, x=x, y=y)
    time.sleep(0.12)  # Godot picks the hovered control on the next frame


def _press(dpy, button: int):
    from Xlib import X
    _fake(dpy, X.ButtonPress, button)
    time.sleep(0.06)
    _fake(dpy, X.ButtonRelease, button)
    time.sleep(0.06)


def _key(dpy, keysym: int):
    """One key by keysym, with shift when the keymap has it on the shifted level."""
    from Xlib import X, XK
    codes = dpy.keysym_to_keycodes(keysym)
    hit = next(((c, i) for c, i in codes if i in (0, 1)), None)
    if hit is None:
        return False
    code, shifted = hit
    shift = dpy.keysym_to_keycode(XK.XK_Shift_L)
    if shifted:
        _fake(dpy, X.KeyPress, shift)
    _fake(dpy, X.KeyPress, code)
    _fake(dpy, X.KeyRelease, code)
    if shifted:
        _fake(dpy, X.KeyRelease, shift)
    time.sleep(0.02)
    return True


MODS = {"ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "cmd": "Super_L",
        "meta": "Super_L", "super": "Super_L"}


def _chord(dpy, name: str) -> bool:
    """A key with modifiers held, as `ctrl+a` (aipt3: the tester had no way to select a kept line whole, and typed
    into the middle of it). The last part is the key; the parts before it are held while it is pressed."""
    from Xlib import X, XK
    *mods, key = name.split("+")
    held = []
    for m in mods:
        if m.lower() not in MODS:
            raise SystemExit(f"unknown modifier {m!r} in {name!r}; one of {sorted(MODS)}")
        held.append(dpy.keysym_to_keycode(XK.string_to_keysym(MODS[m.lower()])))
    for code in held:
        _fake(dpy, X.KeyPress, code)
    ok = _key(dpy, keysym(key))
    for code in reversed(held):
        _fake(dpy, X.KeyRelease, code)
    return ok


def keysym(name: str) -> int:
    """A key's name (Return, BackSpace, Escape, Tab, Down, a, ...) or a single character."""
    from Xlib import XK
    k = XK.string_to_keysym(name)
    if k == 0 and len(name) == 1:
        k = ord(name) if ord(name) < 256 else 0
    if k == 0:
        raise SystemExit(f"unknown key {name!r}")
    return k


def do(a):
    s = _session(a.dir)
    dpy = _display(s)
    rec = {"cmd": a.cmd}
    if a.cmd == "click":
        _move(dpy, a.x, a.y)
        for _ in range(2 if a.double else 1):
            _press(dpy, BUTTONS[a.button])
        rec.update(x=a.x, y=a.y, button=a.button, double=a.double)
    elif a.cmd == "scroll":
        _move(dpy, a.x, a.y)
        for _ in range(abs(a.notches)):
            _press(dpy, 5 if a.notches > 0 else 4)
        rec.update(x=a.x, y=a.y, notches=a.notches)
    elif a.cmd == "type":
        missed = [ch for ch in a.text if not _key(dpy, keysym(ch))]
        rec.update(text=a.text, missed=missed)
        if missed:
            print(f"not on this keyboard, left out: {''.join(missed)!r}", file=sys.stderr)
    elif a.cmd == "key":
        for name in a.keys:
            chord = len(name) > 2 and "+" in name[1:-1]  # ctrl+a; "+" and "a" alone are plain keys
            if not (_chord(dpy, name) if chord else _key(dpy, keysym(name))):
                raise SystemExit(f"no key for {name!r}")
        rec.update(keys=a.keys)
    elif a.cmd == "hold":
        from Xlib import X
        codes = [dpy.keysym_to_keycode(keysym(k)) for k in a.keys.split("+")]
        if 0 in codes or not 0 < a.seconds <= 60:
            raise SystemExit(f"hold takes keys on this keyboard and 0 to 60 seconds, not {a.keys!r} {a.seconds}")
        for code in codes:
            _fake(dpy, X.KeyPress, code)
        time.sleep(a.seconds)
        for code in reversed(codes):
            _fake(dpy, X.KeyRelease, code)
        rec.update(keys=a.keys, seconds=a.seconds)
    dpy.close()
    _log(a.dir, **rec)
    if a.shot is not None:
        time.sleep(a.shot)
        print(shot(a.dir, s, _crop(a.crop)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("dir")
    p.add_argument("--url", required=True)
    p.add_argument("--inhabit", type=int, required=True)
    p.add_argument("--token", default="")
    p.add_argument("--size", default="1280x800", help="the client's window; the default is the project's own")
    p.add_argument("--record", action="store_true", help="film the session at 2 fps to DIR/session.mp4 (ffmpeg)")
    p.add_argument("--wait", type=float, default=8.0, help="seconds before the first screenshot")
    p.add_argument("--relay", action="store_true", help="connect through a local relay (see above); for wss:// from a cloud container")
    p = sub.add_parser("relay")
    p.add_argument("port", type=int)
    p.add_argument("url")
    p = sub.add_parser("shot")
    p.add_argument("dir")
    p.add_argument("--crop", help="X,Y,W,H: only this part of the window")
    p = sub.add_parser("resize")
    p.add_argument("dir")
    p.add_argument("size")
    sub.add_parser("stop").add_argument("dir")
    for name in ("click", "scroll", "type", "key", "hold"):
        p = sub.add_parser(name)
        p.add_argument("dir")
        if name in ("click", "scroll"):
            p.add_argument("x", type=int)
            p.add_argument("y", type=int)
        if name == "click":
            p.add_argument("--button", choices=list(BUTTONS), default="left")
            p.add_argument("--double", action="store_true")
        if name == "scroll":
            p.add_argument("notches", type=int)
        if name == "type":
            p.add_argument("text")
        if name == "key":
            p.add_argument("keys", nargs="+")
        if name == "hold":
            p.add_argument("keys", help="a key, or several joined by +, held together")
            p.add_argument("seconds", type=float)
        p.add_argument("--shot", type=float, nargs="?", const=1.0, default=None,
                       help="then wait this many seconds (default 1) and take a screenshot")
        p.add_argument("--crop", help="with --shot: X,Y,W,H, only this part of the window")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        for tool in ("Xvfb", "godot") + (("ffmpeg",) if a.record else ()):
            if shutil.which(tool) is None:
                raise SystemExit(f"{tool} is not installed")
        start(a)
    elif a.cmd == "stop":
        stop(a)
    elif a.cmd == "resize":
        resize(a)
    elif a.cmd == "relay":
        asyncio.run(relay(a.port, a.url))
    elif a.cmd == "shot":
        print(shot(a.dir, _session(a.dir), _crop(a.crop)))
    else:
        do(a)


if __name__ == "__main__":
    main()
