# Even G2 BLE Gateway

> 日本語版は [README_ja.md](README_ja.md) をご覧ください。

A Python gateway that bridges [Even Realities G2](https://www.evenrealities.com/) smart glasses to HTTP, WebSocket, and a local browser UI.  
The BLE communication layer is ported from the [MentraOS](https://github.com/Mentra-Community/MentraOS) `G2.kt` implementation.

---

## Features

- **BLE connection management** — automatic pairing, reconnection, and heartbeat for left/right lenses
- **Fast text path** — low-latency full-screen text display via in-place update
- **Layout path** — multi-element pages with positioned text and image containers
- **Image rendering** — base64/data-URL images converted to 4-bit BMP, tiled within device constraints
- **Microphone control** — enable/disable the glasses microphone; audio frames streamed over WebSocket
- **WebSocket broadcast** — all normalised device events delivered to every connected client
- **Tk GUI** — live status window (connection phase, battery, mic, firmware, event log)
- **Browser UI** — static HTML frontend served from the same process, including a layout composer for sending image + text together
- **CLI** — scriptable command-line client for text, images, mic control, and event streaming
- **Config persistence** — `config/gateway.yaml` stores the last-connected pair for fast reconnect

---

## Requirements

- Python 3.11+
- Bluetooth adapter accessible via [Bleak](https://github.com/hbldh/bleak)

### Python dependencies

```
aiohttp
bleak
Pillow
PyYAML
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Quick Start

### 1. Start the gateway server

```bash
python gateway_server.py
```

With the Tk GUI disabled (headless):

```bash
python gateway_server.py --no-gui
```

Additional options:

| Flag | Description |
|---|---|
| `--config PATH` | Path to the YAML config file (default: `config/gateway.yaml`) |
| `--host HOST` | Override the listen host |
| `--port PORT` | Override the listen port |
| `--search-id ID` | Restrict BLE scan to a specific serial prefix |
| `--no-gui` | Disable the Tk status window |
| `--debug-raw-events` | Include `glasses.raw_packet` events in WebSocket output |
| `--log-level LEVEL` | Python logging level (default: `INFO`) |
| `--clear-saved-addresses` | Clear saved glass addresses on startup and rescan |
| `--unpair-on-startup` | Attempt OS-level unpair of saved addresses at startup, then rescan |
| `--image-gamma FLOAT` | Default gamma correction for all images (1.0 = none, <1.0 = brighter; default: `1.0`) |
| `--image-dither` | Enable 4-bit Floyd-Steinberg dithering for all images |

On first run the gateway scans for a G2 pair, connects, runs the initialisation sequence, and saves the discovered addresses to `config/gateway.yaml` for fast reconnect on subsequent launches.

### 2. Open the browser UI

Navigate to `http://127.0.0.1:8765` for the built-in status and test interface.
The layout composer can send an image and positioned text together without hand-editing JSON.

---

## Configuration

`config/gateway.yaml` is created automatically if it does not exist. All values can be overridden via CLI flags.

```yaml
server:
  host: 0.0.0.0
  port: 8765
  websocket_path: /ws
  static_dir: ui

glass:
  search_id: ""          # optional serial prefix filter
  left_address: ""       # populated automatically after first connection
  right_address: ""
  left_mac_address: ""
  right_mac_address: ""
  last_serial_number: ""

ble:
  scan_timeout_sec: 5
  reconnect_interval_sec: 5
  heartbeat_interval_sec: 5
  ble_packet_gap_ms: 8
  text_queue_interval_ms: 100
  image_settle_delay_ms: 1000
  image_fragment_interval_ms: 200
  unpair_on_startup: false

gui:
  enabled: true
```

---

## HTTP API

### `POST /api/display`

Send text or a layout to the glasses.

**Fast text** (lowest latency):

```json
{ "text": "Hello, world!" }
```

**Layout** (positioned text and images):

```json
{
  "elements": [
    {
      "type": "text",
      "text": "Header",
      "x": 0, "y": 0, "width": 576, "height": 50,
      "capture_events": true
    },
    {
      "type": "image",
      "image_base64": "<base64 or data URL>",
      "x": 0, "y": 60, "width": 288, "height": 144
    }
  ]
}
```

**Clear display:**

```json
{ "clear": true }
```

Response:

```json
{ "accepted": true, "mode": "fast_text", "queued": true }
```

Possible `mode` values: `fast_text`, `layout`, `clear`.

---

### `POST /api/mic`

```json
{ "enabled": true }
```

---

### `POST /api/touch`

Synthesise a touch gesture event and broadcast it to all WebSocket clients.

Valid `gesture` values: `single_tap`, `double_tap`, `swipe_up`, `swipe_down`.

```json
{ "gesture": "single_tap" }
```

Response:

```json
{ "accepted": true, "gesture": "single_tap" }
```

---

### `GET /api/status`

Returns a snapshot of the server and glasses state:

```json
{
  "server": { "host": "0.0.0.0", "port": 8765, ... },
  "glasses": {
    "phase": "ready",
    "ready": true,
    "last_serial_number": "G2_...",
    "mic_enabled": false,
    "target_mic_enabled": false,
    "battery_level": 85,
    "charging": false,
    "firmware_version": "...",
    "last_error": "",
    "last_gesture": "single_tap",
    "display_surface": "app",
    "pairing_warning": "",
    "left": { "address": "...", "mac_address": "...", "connected": true, "authenticated": true },
    "right": { "address": "...", "mac_address": "...", "connected": true, "authenticated": true }
  }
}
```

---

## WebSocket

Connect to `ws://127.0.0.1:8765/ws`.  
An initial `status.snapshot` event is sent on connection, followed by all device events in real time.

### Event envelope

```json
{
  "seq": 42,
  "kind": "glasses.touch",
  "timestamp": "2026-05-26T12:34:56.123Z",
  "data": { "gesture": "single_tap", "source": 0 }
}
```

### Event kinds

| Kind | Description |
|---|---|
| `status.snapshot` | Full server + glasses state |
| `connection.state` | BLE phase change |
| `glasses.touch` | Tap / swipe gesture |
| `glasses.mic_audio` | Microphone audio frame (base64-encoded) |
| `glasses.battery` | Battery level and charging state |
| `glasses.firmware` | Firmware version info |
| `glasses.authentication` | Per-lens authentication result |
| `glasses.dashboard` | Dashboard menu selection (reserved) |
| `glasses.raw_packet` | Raw BLE packet (debug mode only) |
| `system.error` | Connection or internal error |
| `system.reinitialize` | Post-exit re-initialisation |

---

## CLI

```bash
python gateway_cli.py [--server URL] [--ws-path PATH] <command>
```

Default server: `http://127.0.0.1:8765`

| Command | Description |
|---|---|
| `send-text --text "hello"` | Send fast text to the glasses |
| `send-image --file img.png [--x N] [--y N] [--width N] [--height N] [--image-gamma FLOAT] [--image-dither]` | Send an image |
| `send-json --file payload.json [--image-gamma FLOAT] [--image-dither]` | Send a raw display JSON file |
| `mic --on` / `mic --off` | Enable or disable the microphone |
| `status` | Print current gateway status |
| `events` | Stream all WebSocket events to stdout |

---

## Display constraints (Even Hub)

| Constraint | Value |
|---|---|
| Canvas | 576 × 288 px, 4-bit greyscale (16 levels) |
| Max containers per page | 12 total (≤ 8 text/list, ≤ 4 image) |
| Container name max length | 16 characters |
| Image container width | 20 – 288 px |
| Image container height | 20 – 144 px |
| Initial text per container | ≤ 1 000 UTF-8 bytes |
| In-place text update | ≤ 2 000 characters |

Images larger than a single container are tiled automatically.  
Every page must have exactly one event-capturing text/list container; the gateway inserts one automatically when needed.

---

## Examples

### `example_character_game.py` — Character game UI

A self-contained demo that renders a character dialogue screen on the glasses
and lets the user navigate a choice menu with swipe gestures.

**Layout (576 × 288 canvas):**

```
┌──────────┬─────────────────────────────────────┐
│ icon     │ dialogue text                       │
│ 100×100  │                                     │
├──────────┴─────────────────────────────────────┤
│ choice list  (capture_events=True)             │
│   > Talk                                       │
│     Use item                                   │
│     Leave                                      │
└────────────────────────────────────────────────┘
```

**Controls:**

| Gesture | Action |
|---|---|
| Swipe up | Move cursor up |
| Swipe down | Move cursor down |
| Single tap | Confirm selection |

**Prerequisites:** gateway server running (`python gateway_server.py`)

```bash
# Optional: place a custom icon image
cp your_icon.png icon.png

python example_character_game.py
```

If `icon.png` is not present, a simple face icon is generated automatically.

---

### `example_pcm_record.py` — Microphone recording

Tap to start / stop recording.  Decoded audio is saved as a WAV file in `recordings/`.

**Controls:**

| Gesture | Action |
|---|---|
| Single tap | Start recording |
| Single tap | Stop recording and save |

**Output:** `recordings/rec_<timestamp>.wav` — 16 kHz, signed 16-bit little-endian, mono PCM

**Prerequisites:** gateway server running (`python gateway_server.py`)

**LC3 codec setup (liblc3 submodule):**

The Even G2 transmits audio compressed with the LC3 codec.  Build the native shared library
once before running:

**Windows (MSYS2 + MinGW-w64):**

```powershell
# Install GCC inside the MSYS2 MinGW64 shell (one-time):
#   pacman -S mingw-w64-x86_64-gcc

$root = "D:/men-g2-ble-gateway/liblc3"   # adjust to your path
C:\msys64\usr\bin\bash.exe -c "
  gcc -O3 -std=c11 -shared -fPIC \
    -I$root/include $root/src/*.c \
    -o $root/liblc3.dll -lm"
```

Expected output: `liblc3\liblc3.dll`

**Linux:**

```bash
cd liblc3
gcc -O3 -std=c11 -shared -fPIC -Iinclude src/*.c -o liblc3.so -lm
```

**macOS:**

```bash
cd liblc3
gcc -O3 -std=c11 -shared -fPIC -Iinclude src/*.c -o liblc3.dylib -lm
# Apple clang works too; replace gcc with clang
```

```bash
python example_pcm_record.py
```

---

## Project structure

```
mentraos/              BLE communication library (ported from G2.kt)
  g2/
    constants.py       UUIDs, command enums, display constraints
    crc.py             CRC16 (matches Kotlin implementation)
    protobuf.py        Minimal protobuf writer / reader
    transport.py       BLE packet framing and reassembly
    scan.py            BLE device discovery and pairing
    render.py          Image decode, resize, 4-bit BMP generation
    events.py          Normalised event types and factory
    state.py           Runtime connection and page state
    client.py          High-level async G2 client
    protocol/
      even_hub.py      Page, text, image, heartbeat, audio builders
      dev_settings.py  Auth, time sync, pipe role, base heartbeat
      g2_setting.py    Device info request
      onboarding.py    Onboarding skip
      even_ai.py       Hey Even toggle
      menu.py          Dashboard menu (passive only)
  LICENSES/
    MentraOS_LICENSE   Original MentraOS licence
    NOTICE.md          Attribution notice

gateway_config.py           YAML config load / save
gateway_server.py           aiohttp server + Tk GUI entry point
gateway_cli.py              CLI client
example_character_game.py   Character game UI demo
example_pcm_record.py       Tap-to-record microphone demo (LC3 → WAV)
config/gateway.yaml         Runtime configuration
ui/                         Static browser frontend
```

---

## Licence

This project is released under the terms in [LICENSE](LICENSE).  
The BLE protocol implementation in `mentraos/` is derived from the MentraOS project.  
See [mentraos/LICENSES/MentraOS_LICENSE](mentraos/LICENSES/MentraOS_LICENSE) and [mentraos/LICENSES/NOTICE.md](mentraos/LICENSES/NOTICE.md) for attribution details.
