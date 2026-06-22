"""Record the live watch-mode UI to an animated GIF for the README.

Drives the *real* bridge: runs a genuine agy session (spends a little AI Pro
quota) while a headless Playwright Chromium loads the same localhost watch page
the real window would show, screenshots it on a timer, and assembles the frames
into an optimized GIF. The on-screen popup windows are suppressed (the launchers
are monkeypatched to no-ops) — Playwright *is* the viewer.

Capture-only deps (not runtime deps of the bridge):
    uv pip install playwright Pillow && python -m playwright install chromium

Usage:
    python tools/capture_watch_gif.py ask    assets/watch-ask.gif
    python tools/capture_watch_gif.py image  assets/watch-image.gif
    python tools/capture_watch_gif.py swarm  assets/watch-swarm.gif

Modes capture, respectively: the Agent Intern single-panel view (ask),
the same view with the generated image shown inline (image), and the Agent
Swarm dashboard with several workers progressing in parallel (swarm).
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import threading
import time

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
import swarm  # noqa: E402
import swarm_watch  # noqa: E402

# --- suppress the real browser windows: Playwright is the only viewer ----------
server._open_watch_window = lambda *a, **k: None  # type: ignore[assignment]
swarm_watch._launch = lambda *a, **k: None  # type: ignore[assignment]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Capture tuning. We render at 2x then downsample for crisp-but-small frames.
SCALE = 2
INTERVAL = 0.25  # seconds between screenshots
TAIL_S = 6.0  # keep filming this long after the run finishes (final answer/image)
SAFETY_CAP_S = 360.0  # hard stop on the capture loop
COLORS = 96  # GIF palette size (dark UI needs few colors)


def _make_workspace() -> str:
    """A throwaway workspace seeded with a few real files for agy to read, so the
    steps are genuine without ever writing into the actual repo."""
    ws = tempfile.mkdtemp(prefix="agy_gifws_")
    for name in ("README.md", "pyproject.toml", "CHANGELOG.md"):
        src = os.path.join(REPO, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(ws, name))
    return ws


def _run_thread(fn):
    done = {"v": False, "err": None}

    def runner():
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - surface but never crash capture
            done["err"] = e
        finally:
            done["v"] = True

    t = threading.Thread(target=runner, daemon=True)
    return t, done


def _capture(port: int, viewport: tuple[int, int], start_run, label: str) -> list:
    """Load the watch page, start the run, screenshot until it finishes + a tail."""
    frames: list[bytes] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]},
            device_scale_factor=SCALE,
        )
        page.goto(f"http://127.0.0.1:{port}/")
        time.sleep(0.7)  # let the page mount + start polling /events
        t, done = start_run()
        t.start()
        t0 = time.time()
        # film while running
        while True:
            frames.append(page.screenshot())
            if done["v"]:
                break
            if time.time() - t0 > SAFETY_CAP_S:
                print(f"[{label}] safety cap hit", flush=True)
                break
            time.sleep(INTERVAL)
        if done["err"]:
            print(f"[{label}] run error: {done['err']}", flush=True)
        # tail: final answer / image render + a beat to settle
        tail_end = time.time() + TAIL_S
        while time.time() < tail_end:
            frames.append(page.screenshot())
            time.sleep(INTERVAL)
        browser.close()
    print(f"[{label}] captured {len(frames)} raw frames", flush=True)
    return frames


NORMAL_MS = 220  # on-screen time for a frame whose content changed
IDLE_MS = 70  # timer-only frames (agy hasn't flushed a step yet) zip by
LEAD_IN = 4  # how many leading "empty body" frames to keep as a lead-in


def _build_gif(frames: list, out_path: str, label: str) -> None:
    """Decode, downsample, dedupe identical frames, trim the dead lead-in, speed
    up timer-only idle frames, quantize to a shared palette, and save the GIF.

    Nothing is fabricated — we only compress dead air: agy flushes its transcript
    in chunks, so early on the panel sits idle with just the clock ticking. We
    keep a short lead-in, then play idle (body-unchanged) frames fast so the real
    steps and answer are what the viewer actually watches.
    """
    imgs = []
    for raw in frames:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if SCALE != 1:
            im = im.resize((im.width // SCALE, im.height // SCALE), Image.LANCZOS)
        imgs.append(im)

    # dedupe fully-identical consecutive frames
    uniq = []
    for im in imgs:
        b = im.tobytes()
        if not uniq or b != uniq[-1][1]:
            uniq.append((im, b))
    images = [u[0] for u in uniq]

    # the "content" region = everything below the header/clock band, so a frame
    # that differs only by the ticking clock counts as idle
    top = int(images[0].height * 0.11)

    def body(im):
        return im.crop((0, top, im.width, im.height)).tobytes()

    bodies = [body(im) for im in images]

    # trim the leading run where the body never changes (empty working panel),
    # keeping only a short lead-in
    first_change = next((i for i in range(1, len(bodies)) if bodies[i] != bodies[0]), len(bodies))
    start = max(0, first_change - LEAD_IN)
    images = images[start:]
    bodies = bodies[start:]

    durs = []
    for i, _ in enumerate(images):
        idle = i > 0 and bodies[i] == bodies[i - 1]
        durs.append(IDLE_MS if idle else NORMAL_MS)
    # hold the final frame a little longer
    durs[-1] += 1700

    # one shared adaptive palette (built from a late frame with the most content)
    master = images[-1].quantize(colors=COLORS, method=Image.MEDIANCUT)
    pal = [im.quantize(palette=master, dither=Image.Dither.NONE) for im in images]

    pal[0].save(
        out_path,
        save_all=True,
        append_images=pal[1:],
        duration=durs,
        loop=0,
        optimize=True,
        disposal=1,
    )
    kb = os.path.getsize(out_path) / 1024
    print(
        f"[{label}] {len(images)} frames -> {out_path} ({kb:.0f} KB, "
        f"{images[0].width}x{images[0].height})",
        flush=True,
    )


def mode_ask(out_path: str) -> None:
    ws = _make_workspace()
    port = server._ensure_watch_server()
    prompt = (
        "Read README.md in this folder and summarize what this MCP bridge does "
        "in exactly three short markdown bullet points. Be concise."
    )

    def start_run():
        return _run_thread(lambda: server._run_agy_watched(prompt, ws, False, 150))

    frames = _capture(port, (560, 740), start_run, "ask")
    _build_gif(frames, out_path, "ask")
    shutil.rmtree(ws, ignore_errors=True)


def mode_image(out_path: str) -> None:
    ws = _make_workspace()
    port = server._ensure_watch_server()
    target = os.path.join(tempfile.gettempdir(), "agy_gif_image.png")
    user_prompt = (
        "A friendly little robot intern reading a glowing book at a desk, "
        "flat vector illustration, dark background with neon green and cyan accents."
    )
    wrapped = server._wrap_image_prompt(user_prompt, target)

    def start_run():
        return _run_thread(
            lambda: server._run_agy_image_watched(wrapped, target, ws, 240, user_prompt)
        )

    frames = _capture(port, (560, 740), start_run, "image")
    _build_gif(frames, out_path, "image")
    shutil.rmtree(ws, ignore_errors=True)


def mode_swarm(out_path: str) -> None:
    ws = _make_workspace()
    port = swarm_watch.ensure_server()
    prompts = [
        "Read README.md and summarize this project in one sentence.",
        "Read pyproject.toml and list this package's name and version.",
        "Read CHANGELOG.md and name the latest released version in one line.",
    ]

    def start_run():
        return _run_thread(lambda: swarm.swarm_ask(prompts, ws, 3, 150, watch=True))

    frames = _capture(port, (440, 640), start_run, "swarm")
    _build_gif(frames, out_path, "swarm")
    shutil.rmtree(ws, ignore_errors=True)


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    mode, out_path = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    {"ask": mode_ask, "image": mode_image, "swarm": mode_swarm}[mode](out_path)


if __name__ == "__main__":
    main()
