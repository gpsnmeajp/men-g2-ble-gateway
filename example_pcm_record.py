"""example_pcm_record.py

Tap to start / stop recording.  Saves decoded audio to a WAV file.

Controls:
  Tap  → start recording
  Tap  → stop recording and save

Output:  recordings/rec_<timestamp>.wav
         16 kHz, signed 16-bit little-endian, mono PCM

Dependencies: aiohttp
Usage:        python example_pcm_record.py


LC3 SETUP (per OS)
==================
The Even G2 transmits microphone audio compressed with the LC3 codec.
This script decodes it using Google's liblc3 reference implementation,
included as a git submodule at liblc3/.  You must compile the native
shared library once before running this script.

── Windows ──────────────────────────────────────────────────────────────
  Requirement: MSYS2 with the MinGW-w64 GCC toolchain.

  1. Install MSYS2 (https://www.msys2.org/) if not already installed.

  2. Install GCC inside the MSYS2 MinGW64 shell (one-time):
       pacman -S mingw-w64-x86_64-gcc
     Skip if C:\\msys64\\mingw64\\bin\\gcc.exe already exists.

  3. Build liblc3.dll (run from PowerShell or Command Prompt):
       $root = "D:/men-g2-ble-gateway/liblc3"  # adjust to your path
       C:\\msys64\\usr\\bin\\bash.exe -c "
         gcc -O3 -std=c11 -shared -fPIC \
           -I$root/include $root/src/*.c \
           -o $root/liblc3.dll -lm"

  Expected output: liblc3\\liblc3.dll

── Linux ────────────────────────────────────────────────────────────────
  Requirement: gcc and libc-dev (usually pre-installed).

  1. Build liblc3.so:
       cd liblc3
       gcc -O3 -std=c11 -shared -fPIC -Iinclude src/*.c -o liblc3.so -lm

  Expected output: liblc3/liblc3.so

── macOS ────────────────────────────────────────────────────────────────
  Requirement: Xcode Command Line Tools (or Homebrew gcc).

  1. Install Xcode CLT if needed:
       xcode-select --install

  2. Build liblc3.dylib:
       cd liblc3
       gcc -O3 -std=c11 -shared -fPIC -Iinclude src/*.c -o liblc3.dylib -lm
     Apple clang also works; replace gcc with clang.

  Expected output: liblc3/liblc3.dylib
"""

from __future__ import annotations

import asyncio
import base64
import ctypes.util as _ctypes_util
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

_T0 = time.monotonic()

def _log(tag: str, msg: str) -> None:
    """Print a timestamped log line to stdout."""
    elapsed = time.monotonic() - _T0
    print(f"[{elapsed:8.3f}] [{tag}] {msg}", flush=True)

import aiohttp

# ─── LC3 decoder setup (liblc3 submodule) ────────────────
import platform as _platform

_LIBLC3_DIR = Path(__file__).parent / "liblc3"

# Shared library filename differs per OS.
_LC3_DLL = str(_LIBLC3_DIR / (
    "liblc3.dll"   if _platform.system() == "Windows" else
    "liblc3.dylib" if _platform.system() == "Darwin"  else
    "liblc3.so"
))

_LC3_DT_US    = 10_000   # frame duration: 10 ms
_LC3_FRAME_SZ = 40       # bytes per LC3 frame (fixed for Even G2)

# Import the ctypes wrapper bundled with the submodule.
# On Windows, ctypes.util.find_library("c") returns None, so we
# temporarily redirect it to msvcrt before importing lc3.py.
sys.path.insert(0, str(_LIBLC3_DIR / "python"))
if _platform.system() == "Windows":
    _orig_find_library = _ctypes_util.find_library
    _ctypes_util.find_library = lambda n: "msvcrt" if n == "c" else _orig_find_library(n)
    import lc3 as _lc3
    _ctypes_util.find_library = _orig_find_library   # restore
else:
    import lc3 as _lc3

# ─── Gateway endpoints ───────────────────────────────────
GATEWAY_HTTP = "http://127.0.0.1:8765"
GATEWAY_WS   = "ws://127.0.0.1:8765/ws"

# ─── Audio format (Even G2 fixed) ────────────────────────
SAMPLE_RATE  = 16000
SAMPLE_WIDTH = 2        # bytes  (signed 16-bit)
CHANNELS     = 1        # mono

# ─── Output directory ────────────────────────────────────
OUTPUT_DIR = Path("recordings")

# How often (seconds) to refresh the "Recording... Xs" display
DISPLAY_REFRESH_SEC = 1.0


# ─── API helpers ─────────────────────────────────────────
async def send_display(session: aiohttp.ClientSession, text: str) -> None:
    """Send POST /api/display (fast-text mode)."""
    async with session.post(
        f"{GATEWAY_HTTP}/api/display", json={"text": text}
    ) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"POST /api/display failed ({resp.status}): {body}")


async def set_mic(session: aiohttp.ClientSession, enabled: bool) -> None:
    """Enable or disable the microphone via POST /api/mic."""
    async with session.post(
        f"{GATEWAY_HTTP}/api/mic", json={"enabled": enabled}
    ) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"POST /api/mic failed ({resp.status}): {body}")


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


# ─── WAV writer ───────────────────────────────────────────
def save_wav(pcm_chunks: list[bytes], path: Path) -> tuple[int, float]:
    """Write collected PCM chunks to a WAV file.

    Returns (sample_count, duration_seconds).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(pcm_chunks)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    n_samples = len(raw) // (SAMPLE_WIDTH * CHANNELS)
    duration  = n_samples / SAMPLE_RATE
    return n_samples, duration


def elapsed_from_chunks(chunks: list[bytes]) -> float:
    """Calculate elapsed recording time (seconds) from accumulated decoded PCM chunks."""
    total_bytes = sum(len(c) for c in chunks)
    return total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


# ─── Main loop ───────────────────────────────────────────
async def main() -> None:
    async with aiohttp.ClientSession() as session:
        print("Waiting for gateway connection...")
        await wait_for_ready(session)
        print("Connected.")

        recording   = False
        pcm_chunks: list[bytes] = []
        display_task: Optional[asyncio.Task] = None

        # LC3 decoder (created once, reused across recordings)
        decoder = _lc3.Decoder(
            _LC3_DT_US, SAMPLE_RATE, num_channels=CHANNELS, libpath=_LC3_DLL
        )
        _log("INIT", f"Decoder ready. DLL={_LC3_DLL}")

        async def recording_display_loop() -> None:
            """Background task: refresh elapsed time on the display once per second."""
            task_id = id(asyncio.current_task())
            _log("DISP_TASK", f"started task_id=0x{task_id:x} list_id=0x{id(pcm_chunks):x}")
            try:
                while True:
                    chunks_id   = id(pcm_chunks)
                    chunks_len  = len(pcm_chunks)
                    total_bytes = sum(len(c) for c in pcm_chunks)
                    elapsed     = total_bytes / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
                    _log("DISP_TASK",
                         f"task=0x{task_id:x} list=0x{chunks_id:x} "
                         f"chunks={chunks_len} bytes={total_bytes} elapsed={elapsed:.3f}s")
                    await send_display(session, f"Recording...\n{elapsed:.1f}s\n\nTap to stop.")
                    await asyncio.sleep(DISPLAY_REFRESH_SEC)
            except asyncio.CancelledError:
                _log("DISP_TASK", f"cancelled task_id=0x{task_id:x}")
                raise

        await send_display(session, "Tap to start recording.")
        print("Ready. Tap to start recording.")

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

                kind = event.get("kind", "")

                # ── Log all events; suppress per-frame audio spam when not recording ──
                if kind == "glasses.mic_audio":
                    src        = event.get("data", {}).get("source", "?")
                    frame_size = event.get("data", {}).get("frame_size", 0)
                    if recording:
                        raw        = base64.b64decode(event["data"]["data_base64"])
                        n_complete = sum(
                            1 for off in range(0, len(raw), _LC3_FRAME_SZ)
                            if len(raw[off:off + _LC3_FRAME_SZ]) == _LC3_FRAME_SZ
                        )
                        total_before = sum(len(c) for c in pcm_chunks)
                        for offset in range(0, len(raw), _LC3_FRAME_SZ):
                            frame = raw[offset : offset + _LC3_FRAME_SZ]
                            if len(frame) == _LC3_FRAME_SZ:
                                pcm_frame = decoder.decode(frame, bit_depth=16)
                                pcm_chunks.append(pcm_frame)
                        total_after  = sum(len(c) for c in pcm_chunks)
                        elapsed_now  = total_after / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
                        _log("AUDIO",
                             f"src={src} raw={len(raw)}B frames={n_complete} "
                             f"pcm+={total_after-total_before}B "
                             f"total={total_after}B elapsed={elapsed_now:.3f}s "
                             f"list_id=0x{id(pcm_chunks):x}")
                    else:
                        _log("AUDIO_SKIP", f"src={src} frame_size={frame_size} (not recording)")
                    continue

                # ── Log and skip all non-touch events ──
                if kind != "glasses.touch":
                    _log("EVENT", f"kind={kind} data={str(event.get('data',''))[:120]}")
                    continue

                gesture = event.get("data", {}).get("gesture", "")
                _log("TOUCH",
                     f"gesture={gesture} recording={recording} "
                     f"list_id=0x{id(pcm_chunks):x} chunks={len(pcm_chunks)}")
                if gesture != "single_tap":
                    continue

                # ── Tap while idle → start recording ──
                if not recording:
                    old_list_id = id(pcm_chunks)
                    recording   = True
                    pcm_chunks  = []
                    _log("START_REC",
                         f"old_list=0x{old_list_id:x} new_list=0x{id(pcm_chunks):x}")
                    await set_mic(session, True)
                    await send_display(session, "Recording...\n0.0s\n\nTap to stop.")
                    display_task = asyncio.create_task(recording_display_loop())
                    _log("START_REC", "display_task created. Recording started.")

                # ── Tap while recording → stop and save ──
                else:
                    stop_elapsed = elapsed_from_chunks(pcm_chunks)
                    stop_chunks  = len(pcm_chunks)
                    _log("STOP_REC",
                         f"elapsed={stop_elapsed:.3f}s chunks={stop_chunks} "
                         f"list_id=0x{id(pcm_chunks):x}")
                    recording = False

                    if display_task is not None:
                        display_task.cancel()
                        display_task = None

                    await set_mic(session, False)

                    if pcm_chunks:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        out_path  = OUTPUT_DIR / f"rec_{timestamp}.wav"
                        n_samples, duration = save_wav(pcm_chunks, out_path)
                        _log("SAVE", f"{out_path}  ({duration:.2f}s, {n_samples} samples)")
                        print(f"Saved: {out_path}  ({duration:.2f}s, {n_samples} samples)")
                        await send_display(
                            session,
                            f"Saved:\n{out_path.name}\n{duration:.2f}s\n\nTap to record again.",
                        )
                    else:
                        _log("SAVE", "No audio captured.")
                        print("No audio captured.")
                        await send_display(
                            session,
                            "No audio captured.\n\nTap to record again.",
                        )


# ─── Cleanup on exit ─────────────────────────────────────
async def cleanup() -> None:
    """Disable mic and clear display on exit."""
    async with aiohttp.ClientSession() as session:
        try:
            await set_mic(session, False)
        except Exception:
            pass
        try:
            async with session.post(
                f"{GATEWAY_HTTP}/api/display", json={"clear": True}
            ) as resp:
                pass
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up...")
        asyncio.run(cleanup())
        print("Done.")
    except TimeoutError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
