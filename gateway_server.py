"""Even G2 Gateway の HTTP / WebSocket / GUI サーバー。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import datetime
import json
import logging
from pathlib import Path
import queue
import threading
from typing import Any, Callable, Dict, Optional

from aiohttp import web

from gateway_config import GatewayConfig, GatewayConfigStore
from mentraos.g2 import G2Client, G2ClientConfig
from mentraos.g2.events import EventFactory


LOGGER = logging.getLogger(__name__)

WEBSOCKET_CLIENT_QUEUE_SIZE = 128


class _WebSocketClient:
    """クライアントごとの送信キューと writer task を束ねる。"""

    def __init__(self, websocket: web.WebSocketResponse) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=WEBSOCKET_CLIENT_QUEUE_SIZE)
        self.sender_task: Optional[asyncio.Task[None]] = None


def clear_saved_glass_addresses(config: GatewayConfig) -> tuple[str, str, str, str, str]:
    """保存済みの再接続ヒントだけを消す。"""

    config.glass.left_address = ""
    config.glass.right_address = ""
    config.glass.left_mac_address = ""
    config.glass.right_mac_address = ""
    return (
        config.glass.left_address,
        config.glass.right_address,
        config.glass.left_mac_address,
        config.glass.right_mac_address,
        config.glass.last_serial_number,
    )


def save_glass_identity(config_store: GatewayConfigStore, identity: tuple[str, str, str, str, str]) -> None:
    """永続設定へ再接続に必要なグラス情報だけを書き戻す。"""

    persisted_config = config_store.load()
    persisted_config.glass.left_address = identity[0]
    persisted_config.glass.right_address = identity[1]
    persisted_config.glass.left_mac_address = identity[2]
    persisted_config.glass.right_mac_address = identity[3]
    persisted_config.glass.last_serial_number = identity[4]
    config_store.save(persisted_config)


class GatewayServerApp:
    """G2Client と aiohttp を束ねるアプリケーション本体。"""

    def __init__(
        self,
        config: GatewayConfig,
        config_store: GatewayConfigStore,
        debug_raw_events: bool = False,
        image_gamma: float = 1.0,
        image_dither: bool = False,
        ui_event_queue: Optional[queue.Queue[Dict[str, Any]]] = None,
    ) -> None:
        self._config = config
        self._config_store = config_store
        self._debug_raw_events = debug_raw_events
        self._image_gamma = image_gamma
        self._image_dither = image_dither
        self._ui_event_queue = ui_event_queue
        self._server_event_factory = EventFactory()

        self._ws_clients: Dict[web.WebSocketResponse, _WebSocketClient] = {}
        self._ws_lock = asyncio.Lock()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._last_persisted_identity: Optional[tuple[str, str, str, str, str]] = None

        self._client = G2Client(self._build_g2_client_config(), event_handler=self._handle_client_event)
        self._app = web.Application()
        self._app.add_routes(
            [
                web.post("/api/display", self.handle_display),
                web.post("/api/mic", self.handle_mic),
                web.post("/api/touch", self.handle_touch),
                web.get("/api/status", self.handle_status),
                web.get(self._config.server.websocket_path, self.handle_websocket),
                web.get("/", self.handle_index),
                web.get("/app.js", self.handle_app_js),
                web.get("/styles.css", self.handle_styles_css),
            ]
        )

    async def start(self) -> None:
        """BLE クライアントと HTTP サーバーを起動する。"""

        client_started = False
        try:
            await self._client.start()
            client_started = True
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner,
                host=self._config.server.host,
                port=self._config.server.port,
            )
            await self._site.start()
            LOGGER.info(
                "Gateway server started on http://%s:%s",
                self._config.server.host,
                self._config.server.port,
            )
        except Exception:
            LOGGER.exception("Gateway server startup failed")
            self._site = None
            if self._runner is not None:
                with suppress(Exception):
                    await self._runner.cleanup()
                self._runner = None
            if client_started:
                with suppress(Exception):
                    await self._client.stop()
            raise

    async def stop(self) -> None:
        """HTTP と BLE の両方を停止する。"""

        async with self._ws_lock:
            clients = list(self._ws_clients)
        for websocket in clients:
            await self._drop_websocket_client(websocket)

        if self._site is not None:
            self._site = None
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        await self._client.stop()

    async def handle_display(self, request: web.Request) -> web.Response:
        """display JSON を受け取り、G2Client へ渡す。"""

        try:
            payload = await request.json()
            result = await self._client.display(payload)
            return web.json_response(result)
        except ValueError as exc:
            return web.json_response({"accepted": False, "error": str(exc)}, status=400)
        except Exception as exc:  # pragma: no cover - 実機依存エラーもここへ入る。
            LOGGER.exception("display request failed")
            return web.json_response({"accepted": False, "error": str(exc)}, status=500)

    async def handle_mic(self, request: web.Request) -> web.Response:
        """マイク ON/OFF を受け取り、G2Client へ渡す。"""

        try:
            payload = await request.json()
            enabled = payload["enabled"]
            if not isinstance(enabled, bool):
                raise TypeError("enabled must be a boolean")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"accepted": False, "error": f"invalid payload: {exc}"}, status=400)

        await self._client.set_mic_enabled(enabled)
        return web.json_response({"accepted": True, "enabled": enabled})

    async def handle_touch(self, request: web.Request) -> web.Response:
        """POST /api/touch: タッチイベントを合成して WebSocket クライアントへ配信する。"""

        _VALID_GESTURES = {"single_tap", "double_tap", "swipe_up", "swipe_down"}
        try:
            payload = await request.json()
            gesture = payload.get("gesture", "")
            if gesture not in _VALID_GESTURES:
                return web.json_response(
                    {"accepted": False, "error": f"gesture must be one of: {', '.join(sorted(_VALID_GESTURES))}"},
                    status=400,
                )
        except (KeyError, json.JSONDecodeError) as exc:
            return web.json_response({"accepted": False, "error": f"invalid payload: {exc}"}, status=400)

        event = self._server_event_factory.new(
            "glasses.touch",
            {"gesture": gesture, "source": "http"},
        )
        await self._handle_client_event(event)
        await self._client.record_touch_gesture(gesture)
        return web.json_response({"accepted": True, "gesture": gesture})

    async def handle_status(self, _: web.Request) -> web.Response:
        """server と glasses の状態スナップショットを返す。"""

        return web.json_response(self._status_payload())

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """イベントブロードキャスト用 WebSocket。"""

        websocket = web.WebSocketResponse(heartbeat=30.0)
        await websocket.prepare(request)

        client = _WebSocketClient(websocket)
        client.sender_task = asyncio.create_task(self._websocket_sender_loop(client))
        async with self._ws_lock:
            self._ws_clients[websocket] = client

        if not self._enqueue_websocket_event(
            client,
            self._server_event_factory.new("status.snapshot", self._status_payload()),
        ):
            await self._drop_websocket_client(websocket)
            return websocket

        async for message in websocket:
            if message.type == web.WSMsgType.ERROR:
                LOGGER.warning("WebSocket error: %s", websocket.exception())
                break

        await self._drop_websocket_client(websocket)
        return websocket

    async def handle_index(self, _: web.Request) -> web.FileResponse:
        """簡易フロントエンドの index.html を返す。"""

        return web.FileResponse(self._ui_dir() / "index.html")

    async def handle_app_js(self, _: web.Request) -> web.FileResponse:
        """UI の JavaScript を返す。"""

        return web.FileResponse(self._ui_dir() / "app.js")

    async def handle_styles_css(self, _: web.Request) -> web.FileResponse:
        """UI のスタイルシートを返す。"""

        return web.FileResponse(self._ui_dir() / "styles.css")

    async def _handle_client_event(self, event: Dict[str, Any]) -> None:
        """G2Client から受けたイベントを WebSocket と GUI へ配る。"""

        outbound_event = event
        if event.get("kind") == "status.snapshot":
            outbound_event = dict(event)
            outbound_event["data"] = self._status_payload()
            self._persist_identity_from_status(outbound_event["data"])

        if self._ui_event_queue is not None:
            with suppress(queue.Full):
                self._ui_event_queue.put_nowait(outbound_event)

        stale_clients = []
        async with self._ws_lock:
            clients = list(self._ws_clients.values())

        for client in clients:
            websocket = client.websocket
            if websocket.closed:
                stale_clients.append(websocket)
                continue
            if not self._enqueue_websocket_event(client, outbound_event):
                stale_clients.append(websocket)

        for websocket in stale_clients:
            await self._drop_websocket_client(websocket)

    async def clear_saved_glass_addresses(self) -> None:
        """次回再接続用の保存済みアドレスを明示的に消す。"""

        cleared_identity = clear_saved_glass_addresses(self._config)
        save_glass_identity(self._config_store, cleared_identity)
        self._last_persisted_identity = cleared_identity
        await self._client.clear_saved_addresses()

    def _build_g2_client_config(self) -> G2ClientConfig:
        """gateway_config と CLI オプションから G2ClientConfig を組み立てる。"""

        return G2ClientConfig(
            search_id=self._config.glass.search_id,
            left_address=self._config.glass.left_address,
            right_address=self._config.glass.right_address,
            left_mac_address=self._config.glass.left_mac_address,
            right_mac_address=self._config.glass.right_mac_address,
            last_serial_number=self._config.glass.last_serial_number,
            scan_timeout_sec=float(self._config.ble.scan_timeout_sec),
            reconnect_interval_sec=float(self._config.ble.reconnect_interval_sec),
            heartbeat_interval_sec=float(self._config.ble.heartbeat_interval_sec),
            ble_packet_gap_ms=int(self._config.ble.ble_packet_gap_ms),
            text_queue_interval_ms=int(self._config.ble.text_queue_interval_ms),
            image_settle_delay_ms=int(self._config.ble.image_settle_delay_ms),
            image_fragment_interval_ms=int(self._config.ble.image_fragment_interval_ms),
            image_gamma=self._image_gamma,
            image_dither=self._image_dither,
            debug_raw_events=self._debug_raw_events,
            unpair_on_startup=bool(self._config.ble.unpair_on_startup),
        )

    def _status_payload(self) -> Dict[str, Any]:
        """server と glasses をまとめた外向け status payload を返す。"""

        return {
            "server": {
                "host": self._config.server.host,
                "port": self._config.server.port,
                "websocket_path": self._config.server.websocket_path,
                "static_dir": self._config.server.static_dir,
            },
            "glasses": self._client.get_status(),
        }

    def _persist_identity_from_status(self, status_payload: Dict[str, Any]) -> None:
        """再接続に必要なグラス情報だけを config へ保存する。"""

        glasses = status_payload.get("glasses", {})
        left = glasses.get("left", {})
        right = glasses.get("right", {})
        identity = (
            str(left.get("address", "")),
            str(right.get("address", "")),
            str(left.get("mac_address", "")),
            str(right.get("mac_address", "")),
            str(glasses.get("last_serial_number", "")),
        )
        if identity == self._last_persisted_identity:
            return

        self._config.glass.left_address = identity[0]
        self._config.glass.right_address = identity[1]
        self._config.glass.left_mac_address = identity[2]
        self._config.glass.right_mac_address = identity[3]
        self._config.glass.last_serial_number = identity[4]
        save_glass_identity(self._config_store, identity)
        self._last_persisted_identity = identity

    def _enqueue_websocket_event(self, client: _WebSocketClient, event: Dict[str, Any]) -> bool:
        """遅い WebSocket client を BLE 処理から切り離す。"""

        try:
            client.queue.put_nowait(event)
        except asyncio.QueueFull:
            LOGGER.warning("WebSocket client queue overflowed; disconnecting slow client")
            return False
        return True

    async def _websocket_sender_loop(self, client: _WebSocketClient) -> None:
        """クライアントごとの送信タスク。"""

        try:
            while True:
                event = await client.queue.get()
                if event is None:
                    return
                await client.websocket.send_json(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("WebSocket sender loop terminated", exc_info=True)
        finally:
            with suppress(Exception):
                if not client.websocket.closed:
                    await client.websocket.close()

    async def _drop_websocket_client(self, websocket: web.WebSocketResponse) -> None:
        """切断対象クライアントを registry から外して writer task を止める。"""

        async with self._ws_lock:
            client = self._ws_clients.pop(websocket, None)
        if client is None:
            return

        current_task = asyncio.current_task()
        if client.sender_task is not None and client.sender_task is not current_task:
            client.sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await client.sender_task

        with suppress(Exception):
            if not websocket.closed:
                await websocket.close()

    def _ui_dir(self) -> Path:
        """静的 UI 配下の絶対パスを返す。"""

        return Path(__file__).resolve().parent / self._config.server.static_dir


class GatewayStatusWindow:
    """運用確認用の簡易 Tk GUI。"""

    def __init__(
        self,
        root: Any,
        event_queue: queue.Queue[Dict[str, Any]],
        on_close: Callable[[], None],
        on_clear_saved_addresses: Callable[[], None],
        port: int = 8080,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self._root = root
        self._event_queue = event_queue
        self._on_close = on_close
        self._on_clear_saved_addresses = on_clear_saved_addresses
        self._port = port

        root.title("Even G2 Gateway")
        root.geometry("860x620")
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        self._labels: Dict[str, Any] = {}
        rows = [
            ("待受", "server"),
            ("接続フェーズ", "phase"),
            ("Ready", "ready"),
            ("シリアル", "serial"),
            ("左アドレス", "left_address"),
            ("右アドレス", "right_address"),
            ("マイク", "mic"),
            ("バッテリー", "battery"),
            ("Firmware", "firmware"),
            ("最終エラー", "error"),
            ("最終ジェスチャー", "gesture"),
        ]

        for row_index, (title, key) in enumerate(rows):
            ttk.Label(frame, text=title).grid(row=row_index, column=0, sticky="w", padx=(0, 8), pady=2)
            variable = tk.StringVar(value="-")
            ttk.Label(frame, textvariable=variable).grid(row=row_index, column=1, sticky="w", pady=2)
            self._labels[key] = variable

        ttk.Button(
            frame,
            text="保存済みアドレスをクリア",
            command=self._on_clear_saved_addresses,
        ).grid(row=0, column=2, sticky="e", padx=(12, 0))

        ttk.Button(
            frame,
            text="ブラウザで開く",
            command=self._open_browser,
        ).grid(row=1, column=2, sticky="e", padx=(12, 0))

        ttk.Label(frame, text="イベントログ").grid(row=len(rows), column=0, sticky="w", pady=(12, 4))
        self._log = ScrolledText(frame, height=20, wrap=tk.WORD)
        self._log.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(len(rows) + 1, weight=1)
        self._poll_events()

    def close(self) -> None:
        """ウィンドウ終了時に server 停止も巻き取る。"""

        self._on_close()
        self._root.destroy()

    def _poll_events(self) -> None:
        """バックグラウンド server から届くイベントを反映する。"""

        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self._root.after(200, self._poll_events)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        """status snapshot ならラベル更新、それ以外はログへ追記する。"""

        kind = event.get("kind")
        if kind == "status.snapshot":
            self._apply_status_snapshot(event.get("data", {}))
        line = self._format_log_line(event)
        self._log.insert("end", f"{line}\n")
        self._log.see("end")

    def _format_log_line(self, event: Dict[str, Any]) -> str:
        """イベントを人間が読みやすい 1 行テキストへ変換する。"""

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        kind = event.get("kind", "?")
        data: Dict[str, Any] = event.get("data", {})

        if kind == "status.snapshot":
            glasses = data.get("glasses", {})
            parts = [
                f"phase={glasses.get('phase', '-')}",
                f"ready={'yes' if glasses.get('ready') else 'no'}",
            ]
            battery = glasses.get("battery_level", -1)
            if battery >= 0:
                parts.append(f"battery={battery}%")
            err = glasses.get("last_error")
            if err:
                parts.append(f"error={err}")
            return f"[{ts}] {kind}  {' '.join(parts)}"

        if kind == "glasses.audio":
            payload_len = len(data.get("payload", ""))
            return f"[{ts}] {kind}  payload_len={payload_len}"

        if data:
            pairs: list[str] = []
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    v_str = json.dumps(v, ensure_ascii=False)
                    if len(v_str) > 60:
                        v_str = v_str[:57] + "..."
                else:
                    v_str = str(v)
                pairs.append(f"{k}={v_str}")
            return f"[{ts}] {kind}  {' '.join(pairs)}"

        return f"[{ts}] {kind}"

    def _open_browser(self) -> None:
        """既定ブラウザで Web UI を開く。"""

        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{self._port}")

    def _apply_status_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """ラベル群へ最新 status を反映する。"""

        server = snapshot.get("server", {})
        glasses = snapshot.get("glasses", {})
        left = glasses.get("left", {})
        right = glasses.get("right", {})

        self._labels["server"].set(f"{server.get('host', '-')}:{server.get('port', '-')}")
        self._labels["phase"].set(glasses.get("phase", "-"))
        self._labels["ready"].set("yes" if glasses.get("ready") else "no")
        self._labels["serial"].set(glasses.get("last_serial_number", "-"))
        self._labels["left_address"].set(left.get("address", "-"))
        self._labels["right_address"].set(right.get("address", "-"))
        self._labels["mic"].set(
            f"現在={'on' if glasses.get('mic_enabled') else 'off'} / 目標={'on' if glasses.get('target_mic_enabled') else 'off'}"
        )
        battery = glasses.get("battery_level", -1)
        charging = glasses.get("charging")
        battery_text = "-" if battery < 0 else f"{battery}% ({'charging' if charging else 'discharging'})"
        self._labels["battery"].set(battery_text)
        firmware = glasses.get("firmware_version") or "-"
        self._labels["firmware"].set(firmware)
        self._labels["error"].set(glasses.get("last_error", "-"))
        self._labels["gesture"].set(glasses.get("last_gesture") or "-")


def parse_args() -> argparse.Namespace:
    """gateway_server.py の CLI 引数を定義する。"""

    parser = argparse.ArgumentParser(description="Even G2 gateway server")
    parser.add_argument("--config", default="config/gateway.yaml", help="設定 YAML のパス")
    parser.add_argument("--host", help="待受ホストを上書きする")
    parser.add_argument("--port", type=int, help="待受ポートを上書きする")
    parser.add_argument("--search-id", help="スキャン対象のシリアル識別子を上書きする")
    parser.add_argument("--clear-saved-addresses", action="store_true", help="保存済みアドレスを起動時に消去する")
    parser.add_argument(
        "--unpair-on-startup",
        action="store_true",
        help="起動時に保存済みグラスアドレスへ OS の unpair を一度だけ試み、その起動は再スキャンする",
    )
    parser.add_argument("--no-gui", action="store_true", help="Tk GUI を起動しない")
    parser.add_argument("--debug-raw-events", action="store_true", help="glasses.raw_packet を配信する")
    parser.add_argument("--image-gamma", type=float, default=1.0, help="画像ガンマ補正値 (1.0 = 無補正、<1.0 で明るい)")
    parser.add_argument("--image-dither", action="store_true", help="4bit Floyd-Steinberg ディザリングを有効化")
    parser.add_argument("--log-level", default="INFO", help="ロギングレベル")
    return parser.parse_args()


def load_config_from_args(args: argparse.Namespace) -> tuple[GatewayConfig, GatewayConfigStore]:
    """設定ファイルを読み、CLI override を反映する。"""

    config_store = GatewayConfigStore(Path(args.config))
    config = config_store.load()
    if args.clear_saved_addresses:
        clear_saved_glass_addresses(config)
        config_store.save(config)
    elif not config_store.path.exists():
        config_store.save(config)

    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.search_id is not None:
        config.glass.search_id = args.search_id
    if args.no_gui:
        config.gui.enabled = False
    return config, config_store


async def run_headless(server: GatewayServerApp) -> None:
    """GUI なしで server を起動し、停止シグナルまで待つ。"""

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        signal_obj = getattr(__import__("signal"), signame, None)
        if signal_obj is None:
            continue
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal_obj, stop_event.set)

    await server.start()
    try:
        await stop_event.wait()
    finally:
        await server.stop()


def run_with_gui(config: GatewayConfig, config_store: GatewayConfigStore, args: argparse.Namespace) -> None:
    """Tk をメインスレッド、asyncio をバックグラウンドスレッドで起動する。"""

    import tkinter as tk

    ui_queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=256)
    server = GatewayServerApp(
        config=config,
        config_store=config_store,
        debug_raw_events=args.debug_raw_events,
        image_gamma=args.image_gamma,
        image_dither=args.image_dither,
        ui_event_queue=ui_queue,
    )

    loop = asyncio.new_event_loop()

    def loop_worker() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=loop_worker, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(server.start(), loop).result()

    shutdown_started = False

    def on_close() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    def on_clear_saved_addresses() -> None:
        asyncio.run_coroutine_threadsafe(server.clear_saved_glass_addresses(), loop).result()

    root = tk.Tk()
    GatewayStatusWindow(root, ui_queue, on_close, on_clear_saved_addresses, port=config.server.port)
    root.mainloop()


def main() -> None:
    """gateway_server.py のエントリポイント。"""

    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    config, config_store = load_config_from_args(args)
    if args.unpair_on_startup:
        config.ble.unpair_on_startup = True

    if config.gui.enabled:
        run_with_gui(config, config_store, args)
        return

    server = GatewayServerApp(
        config=config,
        config_store=config_store,
        debug_raw_events=args.debug_raw_events,
        image_gamma=args.image_gamma,
        image_dither=args.image_dither,
    )
    asyncio.run(run_headless(server))


if __name__ == "__main__":
    main()
