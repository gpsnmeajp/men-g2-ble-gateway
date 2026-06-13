"""G2 Gateway の HTTP / WebSocket / GUI サーバー。"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
import datetime
import hmac
import json
import logging
from pathlib import Path
import queue
import signal
import threading
from typing import Any, Callable, Dict, Optional

from aiohttp import web

from gateway_config import GatewayConfig, GatewayConfigStore
from mentraos.g2 import G2Client, G2ClientConfig
from mentraos.g2.events import EventFactory


LOGGER = logging.getLogger(__name__)

WEBSOCKET_CLIENT_QUEUE_SIZE = 128
MCP_STOP_TIMEOUT_SEC = 3.0
CLIENT_STOP_TIMEOUT_SEC = 5.0
SERVER_STOP_TIMEOUT_SEC = 8.0


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
        self._mcp_task: Optional[asyncio.Task[None]] = None  # FastMCP サーバータスク（有効時のみ）
        self._last_persisted_identity: Optional[tuple[str, str, str, str, str]] = None
        # タッチイベント待機中のコルーチン一覧（MCP wait_for_touch 用）
        self._touch_waiters: list[tuple[asyncio.Future[Dict[str, Any]], Optional[set[str]]]] = []

        self._client = G2Client(self._build_g2_client_config(), event_handler=self._handle_client_event)
        @web.middleware
        async def security_middleware(request: web.Request, handler: Callable[[web.Request], Any]) -> web.StreamResponse:
            return await self._handle_security(request, handler)

        self._app = web.Application(middlewares=[security_middleware])
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
            await self._start_mcp_server()
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
            await self._stop_mcp_server()
            if client_started:
                with suppress(Exception):
                    await self._client.stop()
            raise

    async def stop(self) -> None:
        """HTTP と BLE の両方を停止する。"""

        await self._stop_mcp_server()
        async with self._ws_lock:
            clients = list(self._ws_clients)
        for websocket in clients:
            await self._drop_websocket_client(websocket)

        if self._site is not None:
            with suppress(Exception):
                await self._site.stop()
            self._site = None
        if self._runner is not None:
            with suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
        try:
            await asyncio.wait_for(self._client.stop(), timeout=CLIENT_STOP_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            LOGGER.warning("Timed out stopping G2 client after %.1f seconds", CLIENT_STOP_TIMEOUT_SEC)
        except Exception:
            LOGGER.exception("Failed to stop G2 client")

    async def _start_mcp_server(self) -> None:
        """AI エージェント制御用のオプショナル FastMCP HTTP サーバーを起動する。"""

        if not self._config.mcp.enabled:
            return

        from gateway_mcp import create_gateway_mcp

        mcp = create_gateway_mcp(self)
        self._mcp_task = asyncio.create_task(
            mcp.run_async(
                transport="http",
                host=self._config.mcp.host,
                port=self._config.mcp.port,
                path=self._config.mcp.path,
            ),
            name="g2-fastmcp-server",
        )
        await asyncio.sleep(0)
        if self._mcp_task.done():
            self._mcp_task.result()
        self._mcp_task.add_done_callback(self._handle_mcp_task_done)
        LOGGER.info(
            "FastMCP server started on http://%s:%s%s",
            self._config.mcp.host,
            self._config.mcp.port,
            self._config.mcp.path,
        )

    async def _stop_mcp_server(self) -> None:
        """実行中の FastMCP サーバータスクを停止する。"""

        if self._mcp_task is None:
            return
        task = self._mcp_task
        self._mcp_task = None
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=MCP_STOP_TIMEOUT_SEC)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            LOGGER.warning("Timed out stopping FastMCP server after %.1f seconds", MCP_STOP_TIMEOUT_SEC)
        except Exception:
            LOGGER.debug("FastMCP server task failed during shutdown", exc_info=True)

    def _handle_mcp_task_done(self, task: asyncio.Task[None]) -> None:
        """FastMCP サーバータスクが終了した際のコールバック。予期しない終了をログ出力する。"""
        if task.cancelled() or self._mcp_task is None:
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("FastMCP server stopped unexpectedly")

    async def _handle_security(self, request: web.Request, handler: Callable[[web.Request], Any]) -> web.StreamResponse:
        """Apply CORS preflight handling and API key checks before route handlers."""

        if request.method == "OPTIONS":
            return self._apply_cors_headers(request, web.Response(status=204))

        if not self._is_authorized_request(request):
            response = web.json_response(
                {"accepted": False, "error": "invalid or missing API key"},
                status=401,
            )
            response.headers["WWW-Authenticate"] = 'Bearer realm="g2-gateway"'
            return self._apply_cors_headers(request, response)

        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        if isinstance(response, web.WebSocketResponse):
            return response
        return self._apply_cors_headers(request, response)

    def _is_authorized_request(self, request: web.Request) -> bool:
        expected_api_key = self._config.auth.api_key
        if not expected_api_key or not self._requires_api_key(request):
            return True

        provided_api_key = self._request_api_key(request)
        return bool(provided_api_key) and hmac.compare_digest(
            provided_api_key.encode("utf-8"),
            expected_api_key.encode("utf-8"),
        )

    def _requires_api_key(self, request: web.Request) -> bool:
        path = request.path
        return path.startswith("/api/") or path == self._config.server.websocket_path

    def _request_api_key(self, request: web.Request) -> str:
        header_name = self._config.auth.header_name.strip() or "X-API-Key"
        header_api_key = request.headers.get(header_name, "").strip()
        if header_api_key:
            return header_api_key

        authorization = request.headers.get("Authorization", "").strip()
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()

        query_parameter = self._config.auth.query_parameter.strip()
        if query_parameter:
            return request.query.get(query_parameter, "").strip()
        return ""

    def _apply_cors_headers(self, request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
        origin_value = self._cors_origin_value(request)
        if origin_value is None:
            return response

        response.headers["Access-Control-Allow-Origin"] = origin_value
        response.headers["Access-Control-Allow-Methods"] = ", ".join(self._config.cors.allow_methods)
        response.headers["Access-Control-Allow-Headers"] = ", ".join(self._config.cors.allow_headers)
        if self._config.cors.allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"
        if self._config.cors.max_age >= 0:
            response.headers["Access-Control-Max-Age"] = str(self._config.cors.max_age)
        if origin_value != "*":
            self._append_vary_origin(response)
        return response

    def _cors_origin_value(self, request: web.Request) -> Optional[str]:
        if not self._config.cors.enabled:
            return None

        origin = request.headers.get("Origin")
        if not origin:
            return None

        allowed_origins = self._config.cors.allow_origins
        if "*" in allowed_origins:
            return origin if self._config.cors.allow_credentials else "*"
        if origin in allowed_origins:
            return origin
        return None

    @staticmethod
    def _append_vary_origin(response: web.StreamResponse) -> None:
        vary = response.headers.get("Vary", "")
        if not vary:
            response.headers["Vary"] = "Origin"
            return
        values = {value.strip().lower() for value in vary.split(",")}
        if "origin" not in values:
            response.headers["Vary"] = f"{vary}, Origin"

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

    async def display_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/display と同じ経路で表示ペイロードを送信する。MCP ツールから呼ばれる。"""

        return await self._client.display(payload)

    def status_payload(self) -> Dict[str, Any]:
        """HTTP および MCP 呼び出し元が使用する現在のステータスペイロードを返す。"""

        return self._status_payload()

    async def wait_for_touch(
        self,
        timeout_sec: float = 60.0,
        allowed_gestures: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """次のタッチイベントを待機する。オプションで特定のジェスチャーのみに限定できる。
        
        Args:
            timeout_sec: タイムアウト秒数
            allowed_gestures: 許可するジェスチャーの集合（None の場合は全て許可）
        
        Returns:
            タッチイベントの辞書
        
        Raises:
            asyncio.TimeoutError: タイムアウト時
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Dict[str, Any]] = loop.create_future()
        normalized_allowed = set(allowed_gestures) if allowed_gestures else None
        self._touch_waiters.append((future, normalized_allowed))
        try:
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            # 完了したタスクを待機リストから削除
            self._touch_waiters = [
                (waiter, gestures)
                for waiter, gestures in self._touch_waiters
                if waiter is not future
            ]

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
        elif event.get("kind") == "glasses.touch":
            self._resolve_touch_waiters(event)

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

    def _resolve_touch_waiters(self, event: Dict[str, Any]) -> None:
        """タッチイベントが発生した際、待機中のコルーチンを解決する。
        
        allowed_gestures が指定されている場合は、マッチするジェスチャーの待機者のみ解決する。
        """
        gesture = str(event.get("data", {}).get("gesture", ""))
        remaining_waiters: list[tuple[asyncio.Future[Dict[str, Any]], Optional[set[str]]]] = []
        for future, allowed_gestures in self._touch_waiters:
            if future.done():
                continue
            # 許可ジェスチャーが指定されていて、現在のジェスチャーが含まれない場合はスキップ
            if allowed_gestures is not None and gesture not in allowed_gestures:
                remaining_waiters.append((future, allowed_gestures))
                continue
            # マッチしたのでイベントをセットして待機解除
            future.set_result(event)
        self._touch_waiters = remaining_waiters

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
                "cors_enabled": bool(self._config.cors.enabled),
                "auth_required": bool(self._config.auth.api_key),
                "mcp": {
                    "enabled": bool(self._config.mcp.enabled),
                    "host": self._config.mcp.host,
                    "port": self._config.mcp.port,
                    "path": self._config.mcp.path,
                },
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
        self._normal_label_foreground = ttk.Style().lookup("TLabel", "foreground") or "black"

        root.title("G2 Gateway")
        root.geometry("860x620")
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        self._labels: Dict[str, Any] = {}
        self._label_widgets: Dict[str, Any] = {}
        rows = [
            ("Server", "server"),
            ("Connection Phase", "phase"),
            ("Ready", "ready"),
            ("Serial", "serial"),
            ("Left Address", "left_address"),
            ("Right Address", "right_address"),
            ("Microphone", "mic"),
            ("Battery", "battery"),
            ("Firmware", "firmware"),
            ("Last Error", "error"),
            ("Pairing", "pairing_warning"),
            ("Last Gesture", "gesture"),
        ]

        for row_index, (title, key) in enumerate(rows):
            ttk.Label(frame, text=title).grid(row=row_index, column=0, sticky="w", padx=(0, 8), pady=2)
            variable = tk.StringVar(value="-")
            value_label = ttk.Label(frame, textvariable=variable)
            value_label.grid(row=row_index, column=1, sticky="w", pady=2)
            self._labels[key] = variable
            self._label_widgets[key] = value_label

        ttk.Button(
            frame,
            text="Clear Saved Addresses",
            command=self._on_clear_saved_addresses,
        ).grid(row=0, column=2, sticky="e", padx=(12, 0))

        ttk.Button(
            frame,
            text="Open in Browser",
            command=self._open_browser,
        ).grid(row=1, column=2, sticky="e", padx=(12, 0))

        ttk.Label(frame, text="Event Log").grid(row=len(rows), column=0, sticky="w", pady=(12, 4))
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

        if kind == "glasses.mic_audio":
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
            f"Current={'on' if glasses.get('mic_enabled') else 'off'} / Target={'on' if glasses.get('target_mic_enabled') else 'off'}"
        )
        battery = glasses.get("battery_level", -1)
        charging = glasses.get("charging")
        battery_text = "-" if battery < 0 else f"{battery}% ({'charging' if charging else 'discharging'})"
        self._labels["battery"].set(battery_text)
        firmware = glasses.get("firmware_version") or "-"
        self._labels["firmware"].set(firmware)
        self._labels["error"].set(glasses.get("last_error", "-"))
        pairing_warning = glasses.get("pairing_warning") or ""
        self._labels["pairing_warning"].set(pairing_warning or "-")
        pairing_label = self._label_widgets.get("pairing_warning")
        if pairing_label is not None:
            if pairing_warning:
                pairing_label.configure(foreground="#b42318", font=("TkDefaultFont", 10, "bold"))
            else:
                pairing_label.configure(
                    foreground=self._normal_label_foreground,
                    font=("TkDefaultFont", 10, "normal"),
                )
        self._labels["gesture"].set(glasses.get("last_gesture") or "-")


def _split_cli_values(values: Optional[list[str]]) -> list[str]:
    items: list[str] = []
    for value in values or []:
        items.extend(item.strip() for item in value.split(",") if item.strip())
    return items


def parse_args() -> argparse.Namespace:
    """gateway_server.py の CLI 引数を定義する。"""

    parser = argparse.ArgumentParser(description="G2 gateway server")
    parser.add_argument("--config", default="config/gateway.yaml", help="Path to the settings YAML file")
    parser.add_argument("--host", help="Override the listening host")
    parser.add_argument("--port", type=int, help="Override the listening port")
    parser.add_argument("--search-id", help="Override the target serial identifier for scanning")
    parser.add_argument("--clear-saved-addresses", action="store_true", help="Clear saved glass addresses on startup")
    parser.add_argument(
        "--unpair-on-startup",
        action="store_true",
        help="Attempt OS unpair once for saved glass addresses at startup, then rescan for this run",
    )
    parser.add_argument("--no-gui", action="store_true", help="Do not start the Tk GUI")
    parser.add_argument("--mcp", dest="mcp_enabled", action="store_true", default=None, help="Start the FastMCP HTTP server")
    parser.add_argument("--no-mcp", dest="mcp_enabled", action="store_false", default=None, help="Do not start the FastMCP HTTP server")
    parser.add_argument("--mcp-host", help="Override the FastMCP listening host")
    parser.add_argument("--mcp-port", type=int, help="Override the FastMCP listening port")
    parser.add_argument("--mcp-path", help="Override the FastMCP HTTP path")
    parser.add_argument("--debug-raw-events", action="store_true", help="Emit glasses.raw_packet events")
    parser.add_argument("--image-gamma", type=float, default=1.0, help="Image gamma correction value (1.0 = no correction, <1.0 = brighter)")
    parser.add_argument("--image-dither", action="store_true", help="Enable 4-bit Floyd-Steinberg dithering")
    parser.add_argument("--api-key", default=None, help="Require this API key for /api requests and WebSocket connections")
    parser.add_argument(
        "--cors-allow-origin",
        action="append",
        default=None,
        help="Allow CORS requests from this origin. Repeat or use commas; use * to allow any origin.",
    )
    parser.add_argument("--cors-allow-credentials", action="store_true", help="Send Access-Control-Allow-Credentials: true")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
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
    if args.mcp_enabled is not None:
        config.mcp.enabled = bool(args.mcp_enabled)
    if args.mcp_host:
        config.mcp.host = args.mcp_host
    if args.mcp_port:
        config.mcp.port = args.mcp_port
    if args.mcp_path:
        config.mcp.path = args.mcp_path
    if args.api_key is not None:
        config.auth.api_key = args.api_key
    cors_allow_origins = _split_cli_values(args.cors_allow_origin)
    if cors_allow_origins:
        config.cors.enabled = True
        config.cors.allow_origins = cors_allow_origins
    if args.cors_allow_credentials:
        config.cors.allow_credentials = True
    return config, config_store


async def run_headless(server: GatewayServerApp) -> None:
    """GUI なしで server を起動し、停止シグナルまで待つ。"""

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        signal_obj = getattr(signal, signame, None)
        if signal_obj is None:
            continue
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal_obj, stop_event.set)
    await server.start()
    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        LOGGER.info("Shutdown requested")
    finally:
        await stop_server_with_timeout(server)


async def stop_server_with_timeout(server: GatewayServerApp) -> None:
    """Stop the server without letting shutdown awaitables block process exit."""

    try:
        await asyncio.wait_for(server.stop(), timeout=SERVER_STOP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        LOGGER.warning("Timed out stopping gateway server after %.1f seconds", SERVER_STOP_TIMEOUT_SEC)
    except Exception:
        LOGGER.exception("Failed to stop gateway server")


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
        future = asyncio.run_coroutine_threadsafe(stop_server_with_timeout(server), loop)
        try:
            future.result(timeout=SERVER_STOP_TIMEOUT_SEC + 1.0)
        except FutureTimeoutError:
            LOGGER.warning("Timed out waiting for GUI shutdown")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    def on_clear_saved_addresses() -> None:
        asyncio.run_coroutine_threadsafe(server.clear_saved_glass_addresses(), loop).result()

    root = tk.Tk()
    window = GatewayStatusWindow(root, ui_queue, on_close, on_clear_saved_addresses, port=config.server.port)

    def request_close_from_signal(signum: int, frame: Any) -> None:
        LOGGER.info("Shutdown requested by signal %s", signum)
        with suppress(Exception):
            root.after(0, window.close)

    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    signal.signal(signal.SIGINT, request_close_from_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_close_from_signal)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested")
        window.close()
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        if hasattr(signal, "SIGTERM") and previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


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
    try:
        asyncio.run(run_headless(server))
    except KeyboardInterrupt:
        LOGGER.info("Shutdown interrupted")


if __name__ == "__main__":
    main()
