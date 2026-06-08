#!/usr/bin/env python3
"""Generate static PNG screenshots for the researcher workflow steps 6–10.

Steps captured:
  6 — Open terminal (shell prompt)
  7 — POST /api/query with API key → 202 + job
  8 — Response body: job_id + presigned download URLs
  9 — GET /api/jobs/{id} → status=done
  10 — GET /api/jobs/{id}/download (presigned URL, no key) → JSON data

Output: docs/screenshots/step-{N}-*.png

Usage:
    cd research-api
    python scripts/make_screenshots.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from _demo_server import ROOT, running_server

OUT_DIR = ROOT / "docs" / "screenshots"

# ── Terminal geometry ─────────────────────────────────────────────────────────
COLS, ROWS = 88, 30
FONT_SIZE  = 15
MARGIN     = 16

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG         = (0x1A, 0x1A, 0x2E)   # deep navy
DEFAULT_FG = (0xE0, 0xE0, 0xE0)
PALETTE = {
    "default": DEFAULT_FG,
    "black":   (0x3B, 0x3B, 0x3B),
    "red":     (0xFF, 0x6B, 0x6B),
    "green":   (0x6B, 0xD7, 0x6B),
    "brown":   (0xE6, 0xC0, 0x5F),   # pyte calls SGR-33 "brown"
    "blue":    (0x6B, 0xA8, 0xFF),
    "magenta": (0xD7, 0x8B, 0xFF),
    "cyan":    (0x56, 0xD0, 0xD0),
    "white":   (0xF5, 0xF5, 0xF5),
}

# Prompt colours
C_PROMPT  = "\033[1;34m"   # bold blue
C_CMD     = "\033[0;37m"   # normal white
C_YELLOW  = "\033[0;33m"
C_GREEN   = "\033[0;32m"
C_RED     = "\033[0;31m"
C_CYAN    = "\033[0;36m"
C_DIM     = "\033[2m"
C_BOLD    = "\033[1m"
C_RESET   = "\033[0m"


def _load_font(path: str, name: str, size: int):
    for candidate in (path, name):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Renderer:
    def __init__(self) -> None:
        self.font      = _load_font(FONT_REGULAR, "DejaVuSansMono.ttf", FONT_SIZE)
        self.font_bold = _load_font(FONT_BOLD,    "DejaVuSansMono-Bold.ttf", FONT_SIZE)
        self.cw = max(1, round(self.font.getlength("M")))
        self.ch = int(FONT_SIZE * 1.5)
        self.w  = COLS * self.cw + 2 * MARGIN
        self.h  = ROWS * self.ch + 2 * MARGIN

    def render(self, screen: pyte.Screen) -> Image.Image:
        img  = Image.new("RGB", (self.w, self.h), BG)
        draw = ImageDraw.Draw(img)
        for row in range(ROWS):
            line = screen.buffer[row]
            col  = 0
            while col < COLS:
                cell = line[col]
                fg   = PALETTE.get(cell.fg, DEFAULT_FG) if cell.fg != "default" else DEFAULT_FG
                bold = bool(cell.bold)
                run  = cell.data or " "
                nxt  = col + 1
                while nxt < COLS:
                    c2 = line[nxt]
                    if c2.fg != cell.fg or bool(c2.bold) != bold:
                        break
                    run += c2.data or " "
                    nxt += 1
                if run.strip():
                    x = MARGIN + col * self.cw
                    y = MARGIN + row * self.ch
                    draw.text((x, y), run,
                              font=self.font_bold if bold else self.font,
                              fill=fg)
                col = nxt
        return img


def _screen_from_text(text: str) -> pyte.Screen:
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    for ln in text.split("\n"):
        stream.feed(ln.replace("\r", "") + "\r\n")
    return screen


def _save(renderer: Renderer, text: str, out: Path) -> None:
    screen = _screen_from_text(text)
    img    = renderer.render(screen)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"  saved → {out.relative_to(ROOT)}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _api(method: str, url: str, payload=None, key: str | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"detail": str(e)}
        return e.code, body


def _poll(base: str, job_id: str, key: str) -> dict:
    for _ in range(120):
        time.sleep(0.2)
        status, body = _api("GET", f"{base}/api/jobs/{job_id}", key=key)
        if status == 200 and body.get("status") in ("done", "failed", "cancelled"):
            return body
    raise RuntimeError("timed out waiting for job")


# ── Screenshot builders ────────────────────────────────────────────────────────

def _fmt_json(obj: object, indent: int = 2) -> str:
    return json.dumps(obj, indent=indent)


def _truncate(text: str, max_lines: int = ROWS - 4) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept   = lines[:max_lines]
    hidden = len(lines) - max_lines
    kept.append(f"{C_DIM}  … ({hidden} more lines){C_RESET}")
    return "\n".join(kept)


def build_step6(renderer: Renderer, base: str) -> None:
    """Step 6 — Open terminal, ready to call the API."""
    prompt = f"{C_PROMPT}researcher@laptop{C_RESET}:{C_CYAN}~{C_RESET}$ "
    body = (
        f"{C_DIM}# DSA Transparency Research API{C_RESET}\n"
        f"{C_DIM}# Base URL: {base}{C_RESET}\n"
        f"{C_DIM}# API key obtained from the researcher portal{C_RESET}\n"
        "\n"
        f"{prompt}{C_CMD}export API_KEY=\"researcher-api-key-abc123\"{C_RESET}\n"
        f"{prompt}{C_CMD}export BASE_URL=\"{base}\"{C_RESET}\n"
        "\n"
        f"{prompt}"
    )
    _save(renderer, body, OUT_DIR / "step-06-open-terminal.png")


def build_step7(renderer: Renderer, base: str, job: dict) -> None:
    """Step 7 — POST /api/query with API key."""
    query_json = _fmt_json({
        "table": "t4_notices",
        "query": {"and": [
            {"operation": "EQ", "field_name": "category_code", "field_values": ["TOTAL"]}
        ]},
        "group_by":   ["service_name"],
        "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "notices"}],
        "sort":       [{"field_name": "notices", "order": "desc"}],
        "max_count":  10,
    })
    prompt = f"{C_PROMPT}researcher@laptop{C_RESET}:{C_CYAN}~{C_RESET}$ "
    cmd = (
        f"curl -s -X POST ${{BASE_URL}}/api/query \\\n"
        f"  -H \"X-API-Key: ${{API_KEY}}\" \\\n"
        f"  -H \"Content-Type: application/json\" \\\n"
        f"  -d '{query_json}'"
    )
    body = (
        f"{prompt}{C_CMD}{cmd}{C_RESET}\n"
    )
    _save(renderer, body, OUT_DIR / "step-07-post-query.png")


def build_step8(renderer: Renderer, base: str, job: dict) -> None:
    """Step 8 — 202 response with job_id + presigned download URLs."""
    # Mirror the actual to_public() shape; resolve relative URLs to absolute.
    resp = {
        "job_id":       job["job_id"],
        "status":       job.get("status", "queued"),
        "submitted_by": job.get("submitted_by"),
        "submitted_at": job.get("submitted_at"),
        "compiled_sql": job.get("compiled_sql"),
        "status_url":   base + job["status_url"] if job.get("status_url", "").startswith("/") else job.get("status_url"),
        "result_url":   job.get("result_url"),
        "download_urls": job.get("download_urls"),
    }
    resp_text = _fmt_json(resp)
    body = (
        f"{C_GREEN}HTTP/1.1 202 Accepted{C_RESET}\n"
        f"{C_DIM}Content-Type: application/json{C_RESET}\n"
        "\n"
        + "\n".join(f"  {ln}" for ln in resp_text.splitlines())
        + f"\n\n{C_DIM}# Save the job_id for polling{C_RESET}\n"
        f"{C_PROMPT}researcher@laptop{C_RESET}:{C_CYAN}~{C_RESET}$ "
        f"{C_CMD}JOB_ID=\"{job['job_id']}\"{C_RESET}\n"
    )
    _save(renderer, body, OUT_DIR / "step-08-receive-job-id.png")


def build_step9(renderer: Renderer, base: str, done: dict) -> None:
    """Step 9 — GET /api/jobs/{id} → status=done + download_urls."""
    prompt = f"{C_PROMPT}researcher@laptop{C_RESET}:{C_CYAN}~{C_RESET}$ "
    # Strip very long fields so it fits
    slim = {k: v for k, v in done.items() if k not in ("rows",)}
    resp_text = _truncate(_fmt_json(slim))
    body = (
        f"{prompt}{C_CMD}curl -s ${{BASE_URL}}/api/jobs/${{JOB_ID}} \\\n"
        f"  -H \"X-API-Key: ${{API_KEY}}\"{C_RESET}\n"
        "\n"
        f"{C_GREEN}HTTP/1.1 200 OK{C_RESET}\n"
        "\n"
        + "\n".join(f"  {ln}" for ln in resp_text.splitlines())
    )
    _save(renderer, body, OUT_DIR / "step-09-poll-status.png")


def build_step10(renderer: Renderer, base: str, done: dict) -> None:
    """Step 10 — open presigned download URL, receive data (no API key)."""
    raw_dl = done.get("download_urls", {}).get("json", "")
    if raw_dl.startswith("/"):
        dl_url = base + raw_dl
    elif raw_dl:
        dl_url = raw_dl
    else:
        dl_url = f"{base}/api/jobs/{done['job_id']}/download?format=json&sig=..."
    prompt  = f"{C_PROMPT}researcher@laptop{C_RESET}:{C_CYAN}~{C_RESET}$ "
    # Fetch the actual download
    try:
        _, dl_body = _api("GET", dl_url)
        rows_preview = dl_body.get("rows", dl_body)
        if isinstance(rows_preview, list):
            rows_preview = rows_preview[:5]
        preview_text = _truncate(_fmt_json({"count": len(dl_body.get("rows", [])), "rows": rows_preview}))
    except Exception:
        preview_text = '{"count": 10, "rows": [ ... ]}'

    body = (
        f"{C_DIM}# Presigned URL — no API key needed, open in browser or curl{C_RESET}\n"
        f"{prompt}{C_CMD}curl -s \\\n"
        f"  \"{dl_url}\"{C_RESET}\n"
        "\n"
        f"{C_GREEN}HTTP/1.1 200 OK{C_RESET}\n"
        f"{C_DIM}Content-Disposition: attachment; filename=\"result.json\"{C_RESET}\n"
        "\n"
        + "\n".join(f"  {ln}" for ln in preview_text.splitlines())
    )
    _save(renderer, body, OUT_DIR / "step-10-download-data.png")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    renderer = Renderer()

    with running_server() as base:
        print(f"\nServer up at {base}\n")

        # Fire the query once to get real job metadata
        query = {
            "table": "t4_notices",
            "query": {"and": [
                {"operation": "EQ", "field_name": "category_code", "field_values": ["TOTAL"]}
            ]},
            "group_by":   ["service_name"],
            "aggregates": [{"function": "SUM", "field_name": "notices", "alias": "notices"}],
            "sort":       [{"field_name": "notices", "order": "desc"}],
            "max_count":  10,
        }
        _, job = _api("POST", f"{base}/api/query", payload=query, key="alice")
        job_id: str = job["job_id"]
        print(f"  job submitted: {job_id}")

        done = _poll(base, job_id, "alice")
        print(f"  job status:    {done['status']}")
        if done.get("status") != "done":
            raise RuntimeError(f"Job failed with status '{done.get('status')}': {done.get('error')}")

        print("\nRendering screenshots …")
        build_step6(renderer, base)
        build_step7(renderer, base, job)
        build_step8(renderer, base, job)
        build_step9(renderer, base, done)
        build_step10(renderer, base, done)

    print(f"\nDone → {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
