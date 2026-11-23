"""Smoke check for scripts/aiplay.py (CI, godot.yml): the harness's eyes and hands work on the real client.

    python tests/aiplay_smoke.py ws://127.0.0.1:8770/ RUN_DIR WORK_DIR

Against a paused stub world it inhabits a villager who has company, types a line into the client's Talk box
through the virtual keyboard (over a first line it selects whole with ctrl+a, so the second replaces it), and
checks the server received exactly that line as a `say`, that the
screenshot shows the client, and that stopping the session reads as the player leaving.

The Talk line's place is the client's layout: since the P-pass (#92) the foot of the middle column, drawn at
the default --ui-scale 1.25 (the 1280x800 window is 1024x640 units). If the layout moves it, move TALK here."""
import asyncio
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import s16b  # noqa: E402

AIPLAY = os.path.join(HERE, "..", "scripts", "aiplay.py")
W, H = 1280, 800
TALK = (int(W * 0.53), H - 34)  # the "Say something..." line
TEXT = "Hello there, it's a fine day (isn't it?)"


def aiplay(*args) -> str:
    return subprocess.run([sys.executable, AIPLAY, *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


async def main(url: str, run_dir: str, work: str):
    send, recv, _ = await s16b._ws(url)
    send({"type": "hello", "protocol": 3, "scope": "god", "since": 0})

    async def step() -> dict:
        send({"type": "control", "op": "step"})
        while True:
            m = await recv()
            assert m is not None and m["type"] != "error", m
            if m["type"] == "tick":
                return m["view"]

    def company(view, aid=None):
        """Villagers awake at a place with one to five others awake there: someone to talk to, and a short
        enough "Here with you" list that the Talk line is on screen."""
        up = [a for a in view["agents"] if a["place"] is not None and not a["asleep"]]
        ok = [a["id"] for a in up if 1 <= sum(o["place"] == a["place"] for o in up) - 1 <= 5]
        return ok if aid is None else aid in ok

    view = await step()
    for _ in range(144):
        if company(view):
            break
        view = await step()
    body = company(view)[0]
    aiplay("start", work, "--url", url, "--inhabit", body, "--size", f"{W}x{H}", "--wait", 3)
    for _ in range(60):  # the client's hello, however slow the machine
        if '"inhabit"' in open(os.path.join(run_dir, "runner.jsonl")).read():
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("the client never joined: " + open(os.path.join(work, "client.log")).read()[-2000:])
    view = await step()  # the possess applies
    assert company(view, body), "the body's company left as it was possessed; pick another seed or tick"
    await asyncio.sleep(1.5)
    aiplay("click", work, *TALK)
    aiplay("type", work, "a line I think better of")  # aipt3: ctrl+a selects the line whole, and typing replaces it
    aiplay("key", work, "ctrl+a")
    aiplay("type", work, TEXT)
    aiplay("key", work, "Return")
    await asyncio.sleep(1.0)
    await step()
    await asyncio.sleep(1.0)
    shot = aiplay("shot", work)
    aiplay("stop", work)
    await asyncio.sleep(1.0)

    from PIL import Image
    img = Image.open(shot).convert("RGB")
    assert img.size == (W, H), img.size
    assert len(img.getcolors(1 << 24) or []) > 200, "the screenshot is blank"
    side = [json.loads(line) for line in open(os.path.join(run_dir, "runner.jsonl"))]
    says = [r["act"] for r in side if r["kind"] == "received" and r.get("act", {}).get("action") == "say"]
    assert [a["text"] for a in says] == [TEXT], f"the server got {says}"
    assert any(r["kind"] == "leave" and r["agent"] == body for r in side), "stopping the session did not release the body"
    print(f"aiplay ok: villager {body} said {TEXT!r} through the window; screenshot {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))
