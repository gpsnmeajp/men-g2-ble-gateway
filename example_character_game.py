"""
example_character_game.py

Character game UI sample.

Layout (Canvas 576×288):
  ┌──────────┬────────────────────────────────────────────┐
  │  Icon    │ Dialogue text                              │
  │ 100×100  │ (dialogue)                                 │
  ├──────────┴────────────────────────────────────────────┤
  │ Choices text (choices, capture_events=True)           │
  │   > Talk                                             │
  │     Use item                                         │
  │     Leave                                            │
  └───────────────────────────────────────────────────────┘

Controls:
  Swipe up    → Move cursor up
  Swipe down  → Move cursor down
  Tap         → Confirm selection

Dependencies: aiohttp, Pillow
Usage: python example_character_game.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw
from pathlib import Path

if hasattr(Image, "Resampling"):
    LANCZOS_RESAMPLE = Image.Resampling.LANCZOS
else:
    LANCZOS_RESAMPLE = getattr(Image, "LANCZOS")

# ─── Gateway endpoints ─────────────────────────────────
GATEWAY_HTTP = "http://127.0.0.1:8765"
GATEWAY_WS   = "ws://127.0.0.1:8765/ws"

# ─── Character definition ───────────────────────────────
CHARACTER_NAME = "Alto"
DIALOGUE       = "Hey!\nWhat will you do?"
CHOICES        = [
    "Talk",
    "Use item",
    "Leave",
]

# ─── Layout constants ───────────────────────────────────
# Canvas 576×288
IMG_W,  IMG_H  = 100, 100
IMG_X,  IMG_Y  = 0,   0

DLG_MARGIN = 8                          # gap between icon and dialogue
DLG_X = IMG_W + DLG_MARGIN
DLG_Y = 0
DLG_W = 576 - DLG_X
DLG_H = IMG_H

CHO_MARGIN = 8                          # gap between icon bottom and choices
CHO_X = 0
CHO_Y = IMG_H + CHO_MARGIN
CHO_W = 576
CHO_H = 288 - CHO_Y

# Icon image file path (loaded preferentially when it exists)
ICON_PATH = Path("icon.png")


# ─── Icon loading / generation ─────────────────────────
def _load_icon_from_file(path: Path) -> str:
    """Load an image file, resize to IMG_W×IMG_H, and return as Base64 PNG."""
    with Image.open(path) as src:
        img = src.convert("L").resize((IMG_W, IMG_H), LANCZOS_RESAMPLE)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _generate_icon() -> str:
    """Generate a simple 100×100 face icon and return as Base64 PNG string."""
    img = Image.new("L", (IMG_W, IMG_H), color=0)  # black background
    draw = ImageDraw.Draw(img)

    # face outline (ellipse)
    draw.ellipse([8, 6, 92, 94], outline=220, width=3)
    # left eye
    draw.ellipse([26, 30, 42, 46], fill=220)
    # right eye
    draw.ellipse([58, 30, 74, 46], fill=220)
    # smiling mouth (arc)
    draw.arc([30, 54, 70, 80], start=15, end=165, fill=220, width=3)
    # head highlight
    draw.ellipse([42, 12, 58, 22], fill=180)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def make_icon_b64() -> str:
    """Load icon from ICON_PATH if it exists, otherwise auto-generate one."""
    if ICON_PATH.exists():
        print(f"Loading icon image: {ICON_PATH.resolve()}")
        return _load_icon_from_file(ICON_PATH)
    print(f"Icon image not found ({ICON_PATH}). Generating automatically.")
    return _generate_icon()


# ─── UI helpers ─────────────────────────────────────────
def choices_text(cursor: int) -> str:
    """Build choices text. Prefix the cursor row with >."""
    lines = [
        ("> " if i == cursor else "  ") + choice
        for i, choice in enumerate(CHOICES)
    ]
    return "\n".join(lines)


def build_layout(icon_b64: str, cursor: int) -> dict:
    """Return a 3-element layout payload: image, dialogue, and choices.

    NOTE: The Gateway HTTP API has no partial text update endpoint.
    The full layout including the image is resent on every cursor move.
    """
    return {
        "elements": [
            # ── image container (icon 100×100) ──
            {
                "type":         "image",
                "image_base64": icon_b64,
                "x":            IMG_X,
                "y":            IMG_Y,
                "width":        IMG_W,
                "height":       IMG_H,
            },
            # ── dialogue text (right of icon) ──
            {
                "type":           "text",
                "text":           f"{CHARACTER_NAME}: {DIALOGUE}",
                "x":              DLG_X,
                "y":              DLG_Y,
                "width":          DLG_W,
                "height":         DLG_H,
                "border_width":   1,
                "border_color":   15,
                "padding":        4,
                "container_name": "dialogue",
                "capture_events": False,
            },
            # ── choices text (bottom row, receives input) ──
            {
                "type":           "text",
                "text":           choices_text(cursor),
                "x":              CHO_X,
                "y":              CHO_Y,
                "width":          CHO_W,
                "height":         CHO_H,
                "border_width":   1,
                "border_color":   15,
                "padding":        6,
                "container_name": "choices",
                "capture_events": True,   # this container receives touch/swipe events
            },
        ]
    }


# ─── API helpers ────────────────────────────────────────
async def send_display(session: aiohttp.ClientSession, payload: dict) -> None:
    """Send POST /api/display."""
    async with session.post(
        f"{GATEWAY_HTTP}/api/display", json=payload
    ) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"POST /api/display failed ({resp.status}): {body}")


async def wait_for_ready(session: aiohttp.ClientSession, timeout: float = 30.0) -> None:
    """Wait until the gateway reports the glasses as ready."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with session.get(f"{GATEWAY_HTTP}/api/status") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("glasses", {}).get("ready"):
                        return
        except aiohttp.ClientConnectorError:
            pass
        await asyncio.sleep(1.0)
    raise TimeoutError("Glasses did not become ready within the timeout.")


# ─── Main loop ──────────────────────────────────────────
async def main() -> None:
    icon_b64: str = make_icon_b64()
    cursor: int = 0
    selected: Optional[int] = None

    async with aiohttp.ClientSession() as session:
        # wait for glasses connection
        print("Waiting for gateway connection...")
        await wait_for_ready(session)
        print("Connected. Sending initial layout.")

        # initial display (image + dialogue + choices)
        await send_display(session, build_layout(icon_b64, cursor))
        print(
            "Display ready.\n"
            "  Swipe up/down → move cursor\n"
            "  Tap           → confirm\n"
        )

        # receive touch events via WebSocket
        async with session.ws_connect(GATEWAY_WS) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"WebSocket error: {ws.exception()}", file=sys.stderr)
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if event.get("kind") != "glasses.touch":
                    continue

                gesture = event.get("data", {}).get("gesture", "")
                print(f"  gesture: {gesture}")

                if gesture == "swipe_up":
                    cursor = (cursor - 1) % len(CHOICES)
                    print(f"  cursor → {cursor} ({CHOICES[cursor]})")
                    await send_display(session, build_layout(icon_b64, cursor))

                elif gesture == "swipe_down":
                    cursor = (cursor + 1) % len(CHOICES)
                    print(f"  cursor → {cursor} ({CHOICES[cursor]})")
                    await send_display(session, build_layout(icon_b64, cursor))

                elif gesture == "single_tap":
                    selected = cursor
                    print(f"\nConfirmed: [{selected}] {CHOICES[selected]}")
                    # briefly show the result, then return to choices screen
                    await send_display(
                        session,
                        {"text": f"You selected: {CHOICES[selected]}"},
                    )
                    await asyncio.sleep(1.5)
                    # reset cursor and return to choices screen
                    cursor = 0
                    selected = None
                    await send_display(session, build_layout(icon_b64, cursor))
                    print("Returned to choices screen.\n")


async def clear_display() -> None:
    """Clear the display via POST /api/display."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{GATEWAY_HTTP}/api/display", json={"clear": True}
        ) as resp:
            pass  # ignore errors (shutdown path)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Clearing display...")
        asyncio.run(clear_display())
        print("Display cleared.")
    except TimeoutError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
