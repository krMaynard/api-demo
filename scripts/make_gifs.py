#!/usr/bin/env python3
"""Generate showcase GIFs of the api-demo walkthrough — no ffmpeg/ttyd needed.

What it does, end to end and fully headless:

1. Seeds ``demo.db`` if it's missing, then starts ``uvicorn`` on a free port.
2. Runs ``demo.py`` against it and captures the ANSI output (the demo always
   emits colour, so a plain pipe is enough — no pseudo-tty required).
3. Replays the captured bytes through a ``pyte`` terminal emulator and renders
   each step to an animated GIF with Pillow, plus one full-walkthrough GIF.

Output lands in ``docs/gifs/``. Steps are detected by the demo's
``── Step N: …`` headers, so the per-step GIFs track the script automatically.

Usage:
    python scripts/make_gifs.py                 # full + every step
    python scripts/make_gifs.py --only 5 7      # full + just those step numbers
    python scripts/make_gifs.py --no-full       # per-step only

Dev deps: ``pip install -r requirements-dev.txt`` (pyte + Pillow).
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "gifs"

# ── Terminal / rendering geometry ─────────────────────────────────────────────
COLS, ROWS = 94, 38
FONT_SIZE = 14
MARGIN = 12
FRAME_MS = 90           # per-line "typing" frame
HOLD_MS = 1700          # hold on the last frame of each clip
STEP_MAX_FRAMES = 90    # cap frames for a per-step clip
FULL_MAX_FRAMES = 120   # cap frames for the full walkthrough

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (0x1E, 0x1E, 0x1E)
DEFAULT_FG = (0xD4, 0xD4, 0xD4)
# pyte names SGR 30-37 as below; 33 ("yellow") is reported as "brown".
PALETTE = {
    "default": DEFAULT_FG,
    "black": (0x3B, 0x3B, 0x3B),
    "red": (0xFF, 0x6B, 0x6B),
    "green": (0x6B, 0xD7, 0x6B),
    "brown": (0xE6, 0xC0, 0x5F),
    "blue": (0x6B, 0xA8, 0xFF),
    "magenta": (0xD7, 0x8B, 0xFF),
    "cyan": (0x56, 0xD0, 0xD0),
    "white": (0xF5, 0xF5, 0xF5),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STEP_RE = re.compile(r"── Step (\d+[a-z]?):\s*(.*)")


def _fixed_palette() -> Image.Image:
    """A single shared palette for every frame, so GIF frame-diffing kicks in."""
    colors = [BG, DEFAULT_FG, *PALETTE.values()]
    flat: list[int] = []
    for r, g, b in colors:
        flat += [r, g, b]
    flat += [0, 0, 0] * (256 - len(colors))  # pad to a full 256-entry palette
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


_PAL = _fixed_palette()


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Server lifecycle ──────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_db() -> None:
    if not (ROOT / "demo.db").exists():
        print("seeding demo.db …")
        subprocess.run([sys.executable, "seed.py"], cwd=ROOT, check=True)


def _start_server(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:  # fail fast if uvicorn died (bad port, import error, …)
            raise RuntimeError(f"uvicorn exited prematurely with code {proc.returncode}")
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            return proc
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("server did not come up")


def _capture_demo(port: int) -> str:
    env = {**os.environ, "DEMO_BASE_URL": f"http://127.0.0.1:{port}"}
    proc = subprocess.run(
        [sys.executable, "demo.py"], cwd=ROOT, env=env, capture_output=True, timeout=120
    )
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"demo.py exited {proc.returncode}")
    return out


# ── Segmenting the captured output by step ────────────────────────────────────

def _segment(raw: str) -> list[tuple[str, str, str]]:
    """Split into (key, title, text) clips: an intro plus one per ── Step header."""
    lines = raw.split("\n")
    starts: list[int] = [i for i, ln in enumerate(lines) if _STEP_RE.search(_strip_ansi(ln))]

    clips: list[tuple[str, str, str]] = []
    if starts and starts[0] > 0:
        clips.append(("00-intro", "Intro", "\n".join(lines[: starts[0]])))
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        m = _STEP_RE.search(_strip_ansi(lines[start]))
        num, title = m.group(1), m.group(2).strip()
        digits = re.sub(r"\D", "", num) or "0"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
        clips.append((f"step-{int(digits):02d}-{slug}", title, "\n".join(lines[start:end])))
    return clips


# ── Rendering ─────────────────────────────────────────────────────────────────

def _load_font(path: str, name: str, size: int):
    """Load a mono TTF by absolute path, then by family name, then default.

    The absolute paths are where DejaVu lives on Debian/Ubuntu; the name lets
    Pillow find it via the OS font config on macOS / other distros; the default
    bitmap font is a last resort so the script never hard-crashes.
    """
    for candidate in (path, name):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Renderer:
    def __init__(self) -> None:
        self.font = _load_font(FONT_REGULAR, "DejaVuSansMono.ttf", FONT_SIZE)
        self.font_bold = _load_font(FONT_BOLD, "DejaVuSansMono-Bold.ttf", FONT_SIZE)
        # Cell width = the font's advance, so multi-cell runs stay column-aligned.
        self.cw = max(1, round(self.font.getlength("M")))
        self.ch = int(FONT_SIZE * 1.45)
        self.w = COLS * self.cw + 2 * MARGIN
        self.h = ROWS * self.ch + 2 * MARGIN

    def frame(self, screen: pyte.Screen) -> Image.Image:
        img = Image.new("RGB", (self.w, self.h), BG)
        draw = ImageDraw.Draw(img)
        for row in range(ROWS):
            line = screen.buffer[row]
            col = 0
            while col < COLS:
                cell = line[col]
                fg = PALETTE.get(cell.fg, DEFAULT_FG) if cell.fg != "default" else DEFAULT_FG
                bold = bool(cell.bold)
                # Greedy run of same-styled cells. Spaces are kept in the run
                # (the background is uniform) so each styled span is one draw call.
                run = cell.data or " "
                nxt = col + 1
                while nxt < COLS:
                    c2 = line[nxt]
                    if c2.fg != cell.fg or bool(c2.bold) != bold:
                        break
                    run += c2.data or " "
                    nxt += 1
                if run.strip():
                    x = MARGIN + col * self.cw
                    y = MARGIN + row * self.ch
                    draw.text((x, y), run, font=self.font_bold if bold else self.font, fill=fg)
                col = nxt
        return img


def _render_clip(renderer: Renderer, text: str, out_path: Path, max_frames: int) -> None:
    screen = pyte.Screen(COLS, ROWS)
    screen.reset_mode(pyte.modes.DECAWM)  # no autowrap — clip long lines instead
    stream = pyte.Stream(screen)

    src_lines = text.split("\n")
    # Stride so long clips stay under max_frames while still animating.
    stride = max(1, (len(src_lines) + max_frames - 1) // max_frames)

    frames: list[Image.Image] = []
    for i, ln in enumerate(src_lines):
        stream.feed(ln.replace("\r", "") + "\r\n")
        if i % stride == 0 or i == len(src_lines) - 1:
            frames.append(renderer.frame(screen))

    if not frames:
        frames = [renderer.frame(screen)]

    durations = [FRAME_MS] * len(frames)
    durations[-1] = HOLD_MS
    if len(frames) > 1:
        durations[0] = 700  # let the first frame breathe

    # Quantize every frame to the same fixed palette and leave prior frames in
    # place (disposal=1) so Pillow only encodes each frame's changed region.
    paletted = [f.quantize(palette=_PAL, dither=Image.Dither.NONE) for f in frames]
    paletted[0].save(
        out_path,
        save_all=True,
        append_images=paletted[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Generate showcase GIFs of the demo.")
    ap.add_argument("--only", nargs="*", help="Only render these step numbers (full still made unless --no-full).")
    ap.add_argument("--no-full", action="store_true", help="Skip the full-walkthrough GIF.")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_db()
    port = _free_port()
    server = _start_server(port)
    try:
        raw = _capture_demo(port)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    renderer = Renderer()
    clips = _segment(raw)

    if not args.no_full:
        print("rendering full.gif …")
        _render_clip(renderer, raw, OUT_DIR / "full.gif", FULL_MAX_FRAMES)

    only = set(args.only or [])
    for key, title, text in clips:
        num = key.split("-")[1] if key.startswith("step-") else None
        if only and (num is None or num.lstrip("0") not in {o.lstrip("0") for o in only}):
            continue
        out = OUT_DIR / f"{key}.gif"
        print(f"rendering {out.name}  ({title}) …")
        _render_clip(renderer, text, out, STEP_MAX_FRAMES)

    print(f"\nDone → {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
