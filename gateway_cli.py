"""G2 Gateway を操作する CLI。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp


def _auth_headers(api_key: Optional[str]) -> Dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


def _build_http_url(server: str, path: str) -> str:
    """ベース URL とパスを結合する。"""

    return urljoin(server.rstrip("/") + "/", path.lstrip("/"))


def _build_ws_url(server: str, websocket_path: str) -> str:
    """HTTP base URL から WebSocket URL を組み立てる。"""

    parsed = urlparse(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, websocket_path, "", "", ""))


async def send_display(server: str, payload: Dict[str, Any], api_key: Optional[str] = None) -> None:
    """POST /api/display を叩く。"""

    async with aiohttp.ClientSession(headers=_auth_headers(api_key)) as session:
        async with session.post(_build_http_url(server, "/api/display"), json=payload) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"POST /api/display failed ({response.status}): {body}")
            print(body)


async def send_mic(server: str, enabled: bool, api_key: Optional[str] = None) -> None:
    """POST /api/mic を叩く。"""

    async with aiohttp.ClientSession(headers=_auth_headers(api_key)) as session:
        async with session.post(_build_http_url(server, "/api/mic"), json={"enabled": enabled}) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"POST /api/mic failed ({response.status}): {body}")
            print(body)


async def show_status(server: str, api_key: Optional[str] = None) -> None:
    """GET /api/status を表示する。"""

    async with aiohttp.ClientSession(headers=_auth_headers(api_key)) as session:
        async with session.get(_build_http_url(server, "/api/status")) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"GET /api/status failed ({response.status}): {body}")
            payload = await response.json()
            print(json.dumps(payload, ensure_ascii=False, indent=2))


async def stream_events(server: str, websocket_path: str, api_key: Optional[str] = None) -> None:
    """WebSocket イベントを標準出力へ流し続ける。"""

    async with aiohttp.ClientSession(headers=_auth_headers(api_key)) as session:
        async with session.ws_connect(_build_ws_url(server, websocket_path)) as websocket:
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                        print(json.dumps(payload, ensure_ascii=False))
                    except json.JSONDecodeError:
                        print(message.data)


def parse_args() -> argparse.Namespace:
    """CLI 引数を定義する。"""

    parser = argparse.ArgumentParser(description="G2 gateway CLI")
    parser.add_argument("--server", default="http://127.0.0.1:8765", help="gateway base URL")
    parser.add_argument("--ws-path", default="/ws", help="WebSocket path")
    parser.add_argument("--api-key", default=None, help="API key for protected gateway servers")

    subparsers = parser.add_subparsers(dest="command", required=True)

    send_text_parser = subparsers.add_parser("send-text", help="Send text")
    send_text_parser.add_argument("--text", required=True, help="Text to send")

    send_image_parser = subparsers.add_parser("send-image", help="Send an image file")
    send_image_parser.add_argument("--file", required=True, help="Image file path")
    send_image_parser.add_argument("--x", type=int, default=0)
    send_image_parser.add_argument("--y", type=int, default=0)
    send_image_parser.add_argument("--width", type=int, default=200)
    send_image_parser.add_argument("--height", type=int, default=100)
    send_image_parser.add_argument("--image-gamma", type=float, default=None, help="Gamma correction value (1.0 = no correction)")
    send_image_parser.add_argument("--image-dither", action="store_true", help="Enable 4-bit Floyd-Steinberg dithering")

    send_json_parser = subparsers.add_parser("send-json", help="Send display JSON as-is")
    send_json_parser.add_argument("--file", required=True, help="JSON file path")
    send_json_parser.add_argument("--image-gamma", type=float, default=None, help="Gamma correction value (1.0 = no correction)")
    send_json_parser.add_argument("--image-dither", action="store_true", help="Enable 4-bit Floyd-Steinberg dithering")

    mic_parser = subparsers.add_parser("mic", help="Turn the microphone on or off")
    mic_group = mic_parser.add_mutually_exclusive_group(required=True)
    mic_group.add_argument("--on", action="store_true")
    mic_group.add_argument("--off", action="store_true")

    subparsers.add_parser("events", help="Stream WebSocket events")
    subparsers.add_parser("status", help="Show status")
    return parser.parse_args()


async def dispatch(args: argparse.Namespace) -> None:
    """サブコマンドごとに処理を振り分ける。"""

    if args.command == "send-text":
        await send_display(args.server, {"text": args.text}, args.api_key)
        return

    if args.command == "send-image":
        image_bytes = Path(args.file).read_bytes()
        payload: Dict[str, Any] = {
            "elements": [
                {
                    "type": "image",
                    "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                    "x": args.x,
                    "y": args.y,
                    "width": args.width,
                    "height": args.height,
                }
            ]
        }
        if args.image_gamma is not None:
            payload["gamma"] = args.image_gamma
        if args.image_dither:
            payload["dither"] = True
        await send_display(args.server, payload, args.api_key)
        return

    if args.command == "send-json":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.image_gamma is not None:
            payload["gamma"] = args.image_gamma
        if args.image_dither:
            payload["dither"] = True
        await send_display(args.server, payload, args.api_key)
        return

    if args.command == "mic":
        await send_mic(args.server, enabled=args.on, api_key=args.api_key)
        return

    if args.command == "events":
        await stream_events(args.server, args.ws_path, args.api_key)
        return

    if args.command == "status":
        await show_status(args.server, args.api_key)
        return

    raise ValueError(f"unsupported command: {args.command}")


def main() -> None:
    """gateway_cli.py のエントリポイント。"""

    args = parse_args()
    try:
        asyncio.run(dispatch(args))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
