"""Even G2 向け高レベル非同期クライアント。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import copy
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

try:
    from bleak import BleakClient
except ImportError:  # pragma: no cover - 未導入環境でも import は通す。
    BleakClient = None

from .constants import AUDIO_NOTIFY, CHAR_NOTIFY, CHAR_WRITE, INITIAL_TEXT_MAX_BYTES, SCREEN_HEIGHT, SCREEN_WIDTH, ServiceID
from .events import EventFactory, parse_dev_settings_response, parse_even_hub_response, parse_g2_setting_response
from .protocol import calendar, dashboard, dev_settings, even_ai, even_hub, g2_setting, onboarding
from .render import decode_base64_image, render_image_tiles
from .scan import G2DiscoveredDevice, G2DiscoveredPair, discover_g2_pair
from .state import ClientState, ConnectionPhase
from .transport import G2ReceiveManager, G2SendManager


LOGGER = logging.getLogger(__name__)
LAST_ERROR_CLEAR_DELAY_SEC = 15.0

EventHandler = Callable[[Dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class G2ClientConfig:
    """G2Client のランタイム設定。"""

    search_id: str = ""
    left_address: str = ""
    right_address: str = ""
    left_mac_address: str = ""
    right_mac_address: str = ""
    last_serial_number: str = ""
    scan_timeout_sec: float = 5.0
    reconnect_interval_sec: float = 5.0
    heartbeat_interval_sec: float = 5.0
    ble_packet_gap_ms: int = 8
    text_queue_interval_ms: int = 100
    image_settle_delay_ms: int = 1000
    image_fragment_interval_ms: int = 200
    image_gamma: float = 1.0
    image_dither: bool = False
    debug_raw_events: bool = False
    unpair_on_startup: bool = False


class G2Client:
    """左右 G2 レンズとの接続、初期化、表示、イベント配信を担当する。"""

    def __init__(self, config: G2ClientConfig, event_handler: Optional[EventHandler] = None) -> None:
        self._config = config
        self._event_handler = event_handler
        self._event_factory = EventFactory()
        self._state = ClientState(search_id=config.search_id, last_serial_number=config.last_serial_number)
        self._state.left.address = config.left_address
        self._state.right.address = config.right_address
        self._state.left.mac_address = config.left_mac_address
        self._state.right.mac_address = config.right_mac_address

        self._send_manager = G2SendManager()
        self._receive_manager = G2ReceiveManager()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner_task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._display_lock = asyncio.Lock()
        self._reinitialize_lock = asyncio.Lock()
        self._pending_urgent_writes = 0
        self._disconnect_lock = asyncio.Lock()
        self._disconnect_in_progress = False
        self._connection_generation = 0
        self._side_generations: Dict[str, int] = {"left": 0, "right": 0}
        self._reconnecting_sides: Set[str] = set()
        self._side_reconnect_tasks: Dict[str, asyncio.Task[None]] = {}

        self._left_client: Optional[Any] = None
        self._right_client: Optional[Any] = None
        self._left_device_name: str = ""
        self._right_device_name: str = ""

        self._pending_text_msg: Optional[bytes] = None
        self._last_even_hub_msg: Optional[bytes] = None
        self._last_even_hub_resends_remaining = 0
        self._even_hub_resend_count = 1
        self._heartbeat_counter = 0
        self._image_session_counter = 0
        self._last_audio_frame: Optional[bytes] = None

        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._dev_settings_heartbeat_task: Optional[asyncio.Task[None]] = None
        self._text_queue_task: Optional[asyncio.Task[None]] = None
        self._last_error_clear_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """接続ループを起動する。"""

        if BleakClient is None:
            raise RuntimeError("bleak is required for BLE connections")
        if self._runner_task is not None and not self._runner_task.done():
            return
        if self._config.unpair_on_startup:
            await self._unpair_known_devices_before_connect()
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._disconnect_in_progress = False
        self._connection_generation += 1
        self._runner_task = asyncio.create_task(self._connection_loop())

    def _require_bleak_client(self) -> Any:
        """BleakClient クラスを取得し、未導入時は明示的に失敗させる。"""

        if BleakClient is None:
            raise RuntimeError("bleak is required for BLE connections")
        return BleakClient

    async def stop(self) -> None:
        """全タスクと BLE 接続を停止する。"""

        self._stop_event.set()
        self._disconnected_event.set()
        self._disconnect_in_progress = True
        self._connection_generation += 1
        await self._cancel_last_error_clear_task()
        await self._cancel_side_reconnect_tasks()
        await self._stop_background_tasks()
        await self._disconnect_clients()
        if self._runner_task is not None:
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
        self._state.phase = ConnectionPhase.STOPPED
        self._state.ready = False
        await self._emit_connection_state()
        await self._emit_status_snapshot()

    async def display(self, request: Mapping[str, Any], remember: bool = True) -> Dict[str, Any]:
        """設計どおりに display payload を fast text または layout へ振り分ける。"""

        if request.get("clear"):
            await self.clear_display()
            return {"accepted": True, "mode": "clear", "queued": False}

        elements = self._extract_elements(request)
        text = request.get("text")
        image_gamma = float(request.get("gamma", self._config.image_gamma))
        image_dither = bool(request.get("dither", self._config.image_dither))

        if elements:
            await self._display_layout(elements, image_gamma=image_gamma, image_dither=image_dither, remember=remember)
            return {"accepted": True, "mode": "layout", "queued": False}

        if text is None:
            raise ValueError("display request requires text or elements")
        await self._display_fast_text(str(text), remember=remember)
        return {"accepted": True, "mode": "fast_text", "queued": True}

    async def clear_display(self) -> None:
        """ページ所有権を維持したまま空表示へ戻す。"""

        async with self._display_lock:
            self._state.page.last_display_request = None
            await self._ensure_startup_page(" ")
            if self._state.page.page_created and self._state.page.page_has_text_container:
                self._queue_even_hub_command(
                    even_hub.update_text_message(
                        container_id=self._state.page.text_container_id,
                        content_length=1,
                        content=" ",
                    )
                )
                self._state.page.current_text_content = " "
            await self._emit_status_snapshot()

    async def set_mic_enabled(self, enabled: bool) -> None:
        """目標マイク状態を更新し、初期ページ確立後なら即座に反映する。"""

        self._state.target_mic_enabled = enabled
        await self._emit_status_snapshot()
        if not self._state.ready:
            return
        await self._ensure_startup_page(" ")
        await self._send_even_hub_command(
            even_hub.audio_control_message(enable=enabled, magic_random=self._send_manager.next_magic_random())
        )
        self._state.mic_enabled = enabled
        await self._emit_status_snapshot()

    async def request_device_info(self) -> None:
        """battery / firmware 情報取得をグラスへ要求する。"""

        if not self._is_side_connected("right"):
            return
        await self._send_g2_setting_command(
            g2_setting.request_info(self._send_manager.next_magic_random())
        )

    def get_status(self) -> Dict[str, Any]:
        """現時点の client 状態を辞書で返す。"""

        return self._state.to_dict()

    async def clear_saved_addresses(self) -> None:
        """保存済みアドレスを明示的に消し、次回再接続をスキャンへ戻せるようにする。"""

        self._state.left.address = ""
        self._state.right.address = ""
        self._state.left.mac_address = ""
        self._state.right.mac_address = ""
        await self._emit_status_snapshot()

    async def _unpair_known_devices_before_connect(self) -> None:
        """起動時に既知アドレスへ best-effort の unpair を掛け、その起動では再スキャンへ戻す。"""

        known_addresses = self._collect_known_addresses_for_unpair()
        if not known_addresses:
            LOGGER.info("unpair_on_startup is enabled, but no saved glass addresses are available")
            return

        for address in known_addresses:
            bleak_client_class = self._require_bleak_client()
            client = bleak_client_class(address)
            try:
                await client.unpair()
                LOGGER.info("Requested OS unpair for G2 lens before startup: %s", address)
            except Exception as exc:
                LOGGER.warning("Startup unpair failed for %s: %s", address, exc)

        # 今回の起動は保存アドレスを使わず、OS 側の最新状態で再スキャンする。
        self._state.left.address = ""
        self._state.right.address = ""
        self._state.left.mac_address = ""
        self._state.right.mac_address = ""

    def _collect_known_addresses_for_unpair(self) -> List[str]:
        """unpair 対象になりうる既知アドレス群を重複なく集める。"""

        candidates = [
            self._state.left.address,
            self._state.right.address,
            self._state.left.mac_address,
            self._state.right.mac_address,
        ]
        addresses: List[str] = []
        seen = set()
        for candidate in candidates:
            address = str(candidate or "").strip()
            if not address or address in seen:
                continue
            seen.add(address)
            addresses.append(address)
        return addresses

    async def _connection_loop(self) -> None:
        """切断時に自動回復する接続ループ。"""

        while not self._stop_event.is_set():
            try:
                await self._connect_and_initialize()
                await self._disconnected_event.wait()
                self._disconnected_event.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc)
                if isinstance(exc, RuntimeError) and "could not discover" in message:
                    LOGGER.warning("G2 scan did not find both lenses: %s", message)
                else:
                    LOGGER.exception("G2 connection loop failed")
                await self._cancel_last_error_clear_task()
                self._state.last_error = message
                await self._cleanup_after_connection_failure()
                await self._emit("system.error", {"message": message})
                await self._set_phase(ConnectionPhase.RECOVERING)
                await asyncio.sleep(self._config.reconnect_interval_sec)

    async def _connect_and_initialize(self) -> None:
        """左右接続と初期化シーケンスを 1 回分行う。"""

        await self._set_phase(ConnectionPhase.SCANNING)
        pair = await self._resolve_pair()
        await self._set_phase(ConnectionPhase.CONNECTING)
        generation = await self._connect_pair(pair)
        self._ensure_current_connection(generation, "connect")
        await self._set_phase(ConnectionPhase.INITIALIZING)
        await self._run_init_sequence()
        self._ensure_current_connection(generation, "initialization")
        self._state.ready = True
        await self._start_background_tasks(generation)
        await self._set_phase(ConnectionPhase.READY)
        await self._restore_runtime_state()

    async def _resolve_pair(self) -> G2DiscoveredPair:
        """保存済みアドレスまたはスキャンから接続対象ペアを決める。"""

        if self._state.left.address and self._state.right.address:
            left = G2DiscoveredDevice(
                address=self._state.left.address,
                name="",
                side="left",
                serial_number=self._state.last_serial_number,
                mac_address=self._state.left.mac_address or None,
                rssi=None,
                bleak_device=self._state.left.address,
                advertisement=None,
            )
            right = G2DiscoveredDevice(
                address=self._state.right.address,
                name="",
                side="right",
                serial_number=self._state.last_serial_number,
                mac_address=self._state.right.mac_address or None,
                rssi=None,
                bleak_device=self._state.right.address,
                advertisement=None,
            )
            return G2DiscoveredPair(serial_number=self._state.last_serial_number, left=left, right=right)

        pair = await discover_g2_pair(self._config.scan_timeout_sec, self._config.search_id)
        if pair is None:
            raise RuntimeError("could not discover left and right G2 devices during scan")

        self._state.last_serial_number = pair.serial_number
        self._state.left.address = pair.left.address
        self._state.right.address = pair.right.address
        self._state.left.mac_address = pair.left.mac_address or ""
        self._state.right.mac_address = pair.right.mac_address or ""
        return pair

    async def _connect_pair(self, pair: G2DiscoveredPair) -> int:
        """左右両方へ接続し、notify/audio notify を有効化する。"""

        bleak_client_class = self._require_bleak_client()
        self._connection_generation += 1
        generation = self._connection_generation
        self._disconnect_in_progress = False
        self._reconnecting_sides.clear()
        self._side_generations["left"] += 1
        self._side_generations["right"] += 1
        left_generation = self._side_generations["left"]
        right_generation = self._side_generations["right"]
        left_client = bleak_client_class(
            pair.left.bleak_device,
            disconnected_callback=self._make_disconnect_callback("left", generation, left_generation),
        )
        right_client = bleak_client_class(
            pair.right.bleak_device,
            disconnected_callback=self._make_disconnect_callback("right", generation, right_generation),
        )
        control_gap_sec = max(self._config.ble_packet_gap_ms / 1000, 0.05)

        try:
            await left_client.connect()
            await asyncio.sleep(control_gap_sec)
            await right_client.connect()
            await asyncio.sleep(control_gap_sec)
            await left_client.start_notify(
                CHAR_NOTIFY,
                self._make_notify_callback("L", False, generation, left_generation),
            )
            await asyncio.sleep(control_gap_sec)
            await left_client.start_notify(
                AUDIO_NOTIFY,
                self._make_notify_callback("L", True, generation, left_generation),
            )
            await asyncio.sleep(control_gap_sec)
            await right_client.start_notify(
                CHAR_NOTIFY,
                self._make_notify_callback("R", False, generation, right_generation),
            )
            await asyncio.sleep(control_gap_sec)
            await right_client.start_notify(
                AUDIO_NOTIFY,
                self._make_notify_callback("R", True, generation, right_generation),
            )
        except Exception:
            for client in (left_client, right_client):
                with suppress(Exception):
                    await client.disconnect()
            raise

        self._left_client = left_client
        self._right_client = right_client
        self._left_device_name = pair.left.name
        self._right_device_name = pair.right.name
        self._state.left.name = pair.left.name
        self._state.right.name = pair.right.name
        self._state.left.connected = True
        self._state.right.connected = True
        await self._emit_status_snapshot()
        return generation

    def _ensure_current_connection(self, generation: int, operation: str) -> None:
        """接続世代が変わっていないことを write 前後で確認する。"""

        if generation != self._connection_generation:
            raise RuntimeError(f"connection changed during {operation}")
        if self._disconnect_in_progress:
            raise RuntimeError(f"disconnect is already in progress during {operation}")
        if self._reconnecting_sides:
            sides = ", ".join(sorted(self._reconnecting_sides))
            raise RuntimeError(f"partial reconnect is in progress during {operation}: {sides}")
        if self._left_client is None or self._right_client is None:
            raise RuntimeError(f"connection clients are missing during {operation}")
        for side, client in (("left", self._left_client), ("right", self._right_client)):
            if getattr(client, "is_connected", True) is False:
                raise RuntimeError(f"{side} G2 lens is not connected during {operation}")

    async def _run_init_sequence(self) -> None:
        """G2.kt と設計に基づく最小初期化シーケンスを送る。"""

        self._receive_manager.reset()
        self._state.left.authenticated = False
        self._state.right.authenticated = False
        self._state.page.reset_runtime_flags()
        self._heartbeat_counter = 0

        await self._send_dev_settings_command(
            dev_settings.auth_cmd(self._send_manager.next_magic_random()),
            left=True,
            right=False,
        )
        await asyncio.sleep(0.2)

        await self._send_dev_settings_command(
            dev_settings.auth_cmd(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)

        await self._send_dev_settings_command(
            dev_settings.pipe_role_change(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)

        await self._send_dev_settings_command(
            dev_settings.time_sync(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)

        await self._send_onboarding_command(
            onboarding.skip_onboarding(self._send_manager.next_magic_random())
        )
        await asyncio.sleep(0.2)

        await self._send_even_ai_command(
            even_ai.set_hey_even(self._send_manager.next_magic_random(), enabled=False)
        )
        await asyncio.sleep(0.2)

        await self._send_g2_setting_command(
            g2_setting.set_universe_settings(self._send_manager.next_magic_random())
        )
        await asyncio.sleep(0.2)

        await self._send_calendar_command(
            calendar.default_config_message(self._send_manager.next_magic_random())
        )
        await asyncio.sleep(0.2)

        await self._send_dashboard_command(
            dashboard.display_settings_message(self._send_manager.next_magic_random())
        )
        await asyncio.sleep(0.2)

        await self._ensure_startup_page(" ")
        await asyncio.sleep(0.2)
        await self.request_device_info()

    async def _restore_runtime_state(self) -> None:
        """再接続後に表示状態とマイク状態を復元する。"""

        cached_display = copy.deepcopy(self._state.page.last_display_request)
        if cached_display:
            await self.display(cached_display, remember=False)
        if self._state.target_mic_enabled:
            await self.set_mic_enabled(True)

    async def _disconnect_clients(self) -> None:
        """現在保持している BleakClient をすべて切断する。"""

        clients = [client for client in (self._left_client, self._right_client) if client is not None]
        self._left_client = None
        self._right_client = None
        for client in clients:
            with suppress(Exception):
                await client.disconnect()
        self._state.left.connected = False
        self._state.right.connected = False
        self._state.ready = False

    async def _disconnect_side_client(self, side: str) -> None:
        """片側だけの stale BleakClient を切断して state から外す。"""

        if side == "left":
            client = self._left_client
            self._left_client = None
            self._state.left.connected = False
            self._state.left.authenticated = False
        elif side == "right":
            client = self._right_client
            self._right_client = None
            self._state.right.connected = False
            self._state.right.authenticated = False
        else:
            return
        if client is not None:
            with suppress(Exception):
                await client.disconnect()

    def _side_client(self, side: str) -> Optional[Any]:
        """side 名から現在の BleakClient を返す。"""

        if side == "left":
            return self._left_client
        if side == "right":
            return self._right_client
        return None

    def _is_side_connected(self, side: str) -> bool:
        """state と BleakClient の両方から片側接続可否を確認する。"""

        client = self._side_client(side)
        if client is None or side in self._reconnecting_sides:
            return False
        if getattr(client, "is_connected", True) is False:
            return False
        if side == "left":
            return self._state.left.connected
        if side == "right":
            return self._state.right.connected
        return False

    async def _cleanup_after_connection_failure(self) -> None:
        """接続/初期化失敗後に stale client と stale state を残さない。"""

        async with self._disconnect_lock:
            if not self._disconnect_in_progress:
                self._disconnect_in_progress = True
                self._connection_generation += 1
            self._state.ready = False
        await self._cancel_side_reconnect_tasks()
        await self._stop_background_tasks()
        await self._disconnect_clients()
        self._state.page.reset_runtime_flags()
        self._state.mic_enabled = False
        self._state.left.authenticated = False
        self._state.right.authenticated = False

    async def _start_background_tasks(self, generation: int) -> None:
        """heartbeat と text queue の常駐タスクを起動する。"""

        await self._stop_background_tasks()
        self._heartbeat_task = self._create_background_task(
            self._even_hub_heartbeat_loop(generation),
            "g2-even-hub-heartbeat",
        )
        self._dev_settings_heartbeat_task = self._create_background_task(
            self._dev_settings_heartbeat_loop(generation),
            "g2-dev-settings-heartbeat",
        )
        self._text_queue_task = self._create_background_task(
            self._text_queue_loop(generation),
            "g2-text-queue",
        )

    def _create_background_task(self, awaitable: Awaitable[None], name: str) -> asyncio.Task[None]:
        """常駐タスクの異常終了を再接続へつなげる。"""

        task = asyncio.create_task(awaitable, name=name)
        task.add_done_callback(self._handle_background_task_done)
        return task

    def _handle_background_task_done(self, task: asyncio.Task[None]) -> None:
        """常駐タスクが write 例外などで落ちたときに切断処理を起こす。"""

        if task.cancelled() or self._stop_event.is_set():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is None:
            return
        task_name = task.get_name()
        failed_side = "background_task"
        if task_name in ("g2-even-hub-heartbeat", "g2-text-queue"):
            failed_side = "right"
        elif task_name == "g2-dev-settings-heartbeat":
            if self._is_side_connected("right"):
                failed_side = "right"
            elif self._is_side_connected("left"):
                failed_side = "left"
        LOGGER.warning(
            "G2 background task failed; forcing reconnect",
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._handle_disconnect(failed_side),
            )

    async def _cancel_side_reconnect_tasks(self) -> None:
        """片側再接続タスクを止める。"""

        tasks = list(self._side_reconnect_tasks.values())
        current_task = asyncio.current_task()
        self._side_reconnect_tasks.clear()
        self._reconnecting_sides.clear()
        for task in tasks:
            if task is not current_task:
                task.cancel()
        for task in tasks:
            if task is current_task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("G2 side reconnect task had already failed while stopping", exc_info=True)

    def _start_side_reconnect_task(self, side: str, generation: int) -> None:
        """生きている片側を維持したまま、切れた側だけ reconnect する。"""

        existing = self._side_reconnect_tasks.get(side)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._side_reconnect_loop(side, generation),
            name=f"g2-{side}-reconnect",
        )
        self._side_reconnect_tasks[side] = task

    async def _side_reconnect_loop(self, side: str, generation: int) -> None:
        """片側再接続を成功するまで繰り返す。"""

        try:
            while not self._stop_event.is_set() and generation == self._connection_generation:
                try:
                    await self._reconnect_side_once(side, generation)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc)
                    LOGGER.warning("G2 %s side reconnect failed: %s", side, message)
                    self._state.last_error = message
                    await self._emit("system.error", {"message": message, "side": side})
                    await self._emit_status_snapshot()
                    await asyncio.sleep(self._config.reconnect_interval_sec)
        finally:
            current_task = asyncio.current_task()
            if self._side_reconnect_tasks.get(side) is current_task:
                self._side_reconnect_tasks.pop(side, None)

    async def _reconnect_side_once(self, side: str, generation: int) -> None:
        """片側だけの Bleak 接続と最小初期化をやり直す。"""

        if side not in ("left", "right"):
            raise ValueError(f"unsupported reconnect side: {side}")
        self._reconnecting_sides.add(side)
        address = self._state.left.address if side == "left" else self._state.right.address
        if not address:
            raise RuntimeError(f"cannot reconnect {side} G2 lens without saved address")

        bleak_client_class = self._require_bleak_client()
        self._side_generations[side] += 1
        side_generation = self._side_generations[side]
        source_key = "L" if side == "left" else "R"
        control_gap_sec = max(self._config.ble_packet_gap_ms / 1000, 0.05)
        client = bleak_client_class(
            address,
            disconnected_callback=self._make_disconnect_callback(side, generation, side_generation),
        )

        try:
            await client.connect()
            await asyncio.sleep(control_gap_sec)
            await client.start_notify(
                CHAR_NOTIFY,
                self._make_notify_callback(source_key, False, generation, side_generation),
            )
            await asyncio.sleep(control_gap_sec)
            await client.start_notify(
                AUDIO_NOTIFY,
                self._make_notify_callback(source_key, True, generation, side_generation),
            )
        except Exception:
            with suppress(Exception):
                await client.disconnect()
            raise

        if generation != self._connection_generation or self._stop_event.is_set():
            with suppress(Exception):
                await client.disconnect()
            self._reconnecting_sides.discard(side)
            return

        if side == "left":
            self._left_client = client
            self._state.left.connected = True
            self._state.left.authenticated = False
        else:
            self._right_client = client
            self._state.right.connected = True
            self._state.right.authenticated = False

        self._reconnecting_sides.discard(side)
        try:
            await self._run_side_reconnect_init(side)
        except Exception:
            self._reconnecting_sides.add(side)
            await self._disconnect_side_client(side)
            raise
        if self._is_side_connected("left") and self._is_side_connected("right"):
            self._state.ready = True
            await self._set_phase(ConnectionPhase.READY)
            await self._restore_runtime_state()
        else:
            await self._emit_status_snapshot()
        LOGGER.info("G2 %s side reconnected", side)

    async def _run_side_reconnect_init(self, side: str) -> None:
        """片側 reconnect 後に必要な最小初期化を送る。"""

        if side == "left":
            await self._send_dev_settings_command(
                dev_settings.auth_cmd(self._send_manager.next_magic_random()),
                left=True,
                right=False,
            )
            return

        await self._send_dev_settings_command(
            dev_settings.auth_cmd(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)
        await self._send_dev_settings_command(
            dev_settings.pipe_role_change(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)
        await self._send_dev_settings_command(
            dev_settings.time_sync(self._send_manager.next_magic_random()),
            left=False,
            right=True,
        )
        await asyncio.sleep(0.2)
        await self._send_onboarding_command(onboarding.skip_onboarding(self._send_manager.next_magic_random()))
        await asyncio.sleep(0.2)
        await self._send_even_ai_command(even_ai.set_hey_even(self._send_manager.next_magic_random(), enabled=False))
        await asyncio.sleep(0.2)
        await self._send_g2_setting_command(g2_setting.set_universe_settings(self._send_manager.next_magic_random()))
        await asyncio.sleep(0.2)
        await self._send_calendar_command(calendar.default_config_message(self._send_manager.next_magic_random()))
        await asyncio.sleep(0.2)
        await self._send_dashboard_command(dashboard.display_settings_message(self._send_manager.next_magic_random()))
        await asyncio.sleep(0.2)
        await self._ensure_startup_page(" ")
        await asyncio.sleep(0.2)
        await self.request_device_info()

    async def _stop_background_tasks(self) -> None:
        """常駐タスクを安全に止める。"""

        tasks = [
            task
            for task in (self._heartbeat_task, self._dev_settings_heartbeat_task, self._text_queue_task)
            if task is not None
        ]
        self._heartbeat_task = None
        self._dev_settings_heartbeat_task = None
        self._text_queue_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("G2 background task had already failed while stopping", exc_info=True)
        self._pending_text_msg = None
        self._last_even_hub_msg = None
        self._last_even_hub_resends_remaining = 0
        self._pending_urgent_writes = 0

    async def _even_hub_heartbeat_loop(self, generation: int) -> None:
        """EvenHub heartbeat を一定周期で送り、送信遅延で周期が伸び続けないようにする。"""

        interval = self._config.heartbeat_interval_sec
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + interval
        while not self._stop_event.is_set() and generation == self._connection_generation:
            await asyncio.sleep(max(0.0, next_deadline - loop.time()))
            if generation != self._connection_generation:
                return
            if not self._is_side_connected("right"):
                next_deadline = loop.time() + interval
                continue
            await self._send_even_hub_command(
                even_hub.heartbeat_message(self._send_manager.next_magic_random()),
                urgent=True,
            )
            self._heartbeat_counter += 1
            if self._heartbeat_counter % 10 == 0:
                await self.request_device_info()
            now = loop.time()
            while next_deadline <= now:
                next_deadline += interval

    async def _dev_settings_heartbeat_loop(self, generation: int) -> None:
        """DevSettings heartbeat を一定周期で送り、送信待ちで周期が伸び続けないようにする。"""

        interval = self._config.heartbeat_interval_sec
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + interval + (interval / 2)
        while not self._stop_event.is_set() and generation == self._connection_generation:
            await asyncio.sleep(max(0.0, next_deadline - loop.time()))
            if generation != self._connection_generation:
                return
            right_connected = self._is_side_connected("right")
            left_connected = self._is_side_connected("left")
            if not right_connected and not left_connected:
                next_deadline = loop.time() + interval
                continue
            await self._send_dev_settings_command(
                dev_settings.base_heartbeat(self._send_manager.next_magic_random()),
                left=not right_connected and left_connected,
                right=right_connected,
                urgent=True,
            )
            now = loop.time()
            while next_deadline <= now:
                next_deadline += interval

    async def _text_queue_loop(self, generation: int) -> None:
        """最新 text update だけを一定間隔で排出する。"""

        while not self._stop_event.is_set() and generation == self._connection_generation:
            await asyncio.sleep(self._config.text_queue_interval_ms / 1000)
            if generation != self._connection_generation:
                return
            if not self._is_side_connected("right"):
                continue
            payload: Optional[bytes] = None
            if self._pending_text_msg is not None:
                payload = self._pending_text_msg
                self._pending_text_msg = None
                self._last_even_hub_msg = payload
                self._last_even_hub_resends_remaining = self._even_hub_resend_count
            elif self._last_even_hub_resends_remaining > 0 and self._last_even_hub_msg is not None:
                self._last_even_hub_resends_remaining -= 1
                payload = self._last_even_hub_msg
            if payload is not None:
                await self._send_even_hub_command(payload)

    async def _display_fast_text(self, text: str, remember: bool = True) -> None:
        """全画面テキストコンテナを使う高速経路。"""

        normalized_text = text if text else " "
        self._validate_text_size(normalized_text, "text")
        async with self._display_lock:
            # 重複排除: 既にフルスクリーンコンテナが存在し内容が同一なら BLE 送信をスキップ
            if (
                self._state.page.page_has_fullscreen_text_container
                and self._state.page.current_text_content == normalized_text
            ):
                if remember:
                    self._state.page.last_display_request = {"text": text}
                return
            await self._ensure_startup_page(normalized_text)
            if self._state.page.page_has_fullscreen_text_container:
                message = even_hub.update_text_message(
                    container_id=self._state.page.text_container_id,
                    content_length=len(normalized_text.encode("utf-8")),
                    content=normalized_text,
                )
                self._queue_even_hub_command(message)
                self._state.page.current_text_content = normalized_text
            if remember:
                self._state.page.last_display_request = {"text": text}
            await self._emit_status_snapshot()

    async def _display_layout(
        self,
        elements: Sequence[Mapping[str, Any]],
        image_gamma: float = 1.0,
        image_dither: bool = False,
        remember: bool = True,
    ) -> None:
        """text/image レイアウトを rebuild して反映する。"""

        async with self._display_lock:
            # 重複排除: ページ構造が既に存在し request が完全一致なら BLE リビルドをスキップ
            new_elements = [dict(e) for e in elements]
            has_user_text = any(str(element.get("type", "")).lower() == "text" for element in new_elements)
            if self._state.page.startup_page_created:
                prev = self._state.page.last_display_request or {}
                if (
                    prev.get("elements") == new_elements
                    and prev.get("gamma") == image_gamma
                    and prev.get("dither") == image_dither
                ):
                    if remember:
                        self._state.page.last_display_request = {
                            "elements": new_elements,
                            "gamma": image_gamma,
                            "dither": image_dither,
                        }
                    return

            text_specs, image_tiles = self._build_layout_specs(
                elements, image_gamma=image_gamma, image_dither=image_dither
            )
            image_specs = [
                even_hub.ImageContainerSpec(
                    x=tile.x,
                    y=tile.y,
                    width=tile.width,
                    height=tile.height,
                    container_id=tile.container_id,
                    container_name=tile.container_name,
                )
                for tile in image_tiles
            ]

            if self._state.page.startup_page_created:
                message = even_hub.rebuild_page_message(
                    text_containers=text_specs,
                    image_containers=image_specs,
                    magic_random=self._send_manager.next_magic_random(),
                )
            else:
                message = even_hub.create_page_message(
                    text_containers=text_specs,
                    image_containers=image_specs,
                    magic_random=self._send_manager.next_magic_random(),
                )
                self._state.page.startup_page_created = True

            await self._send_even_hub_command(message)
            self._state.page.page_created = True
            self._state.page.page_has_text_container = bool(text_specs)
            self._state.page.page_has_fullscreen_text_container = (
                len(text_specs) == 1
                and not image_specs
                and text_specs[0].x == 0
                and text_specs[0].y == 0
                and text_specs[0].width == SCREEN_WIDTH
                and text_specs[0].height == SCREEN_HEIGHT
            )
            self._state.page.current_text_content = ""

            if image_tiles:
                await asyncio.sleep(self._config.image_settle_delay_ms / 1000)
                for tile in image_tiles:
                    await self._send_image_tile(tile)
                if has_user_text:
                    # 画像転送時に余白も含めて text 領域を上書きするため、
                    # mixed layout では画像反映後に text を再描画する。
                    for text_spec in text_specs:
                        if text_spec.content is None:
                            continue
                        await self._send_even_hub_command(
                            even_hub.update_text_message(
                                container_id=text_spec.container_id,
                                content_length=len(text_spec.content.encode("utf-8")),
                                content=text_spec.content,
                            )
                        )

            if remember:
                self._state.page.last_display_request = {
                    "elements": new_elements,
                    "gamma": image_gamma,
                    "dither": image_dither,
                }
            await self._emit_status_snapshot()

    async def _ensure_startup_page(self, initial_text: str) -> None:
        """全画面テキストコンテナを持つ startup page を必要時に作る。"""

        if self._state.page.page_created and self._state.page.page_has_fullscreen_text_container:
            return
        normalized_text = initial_text if initial_text else " "
        self._validate_text_size(normalized_text, "text")
        text_spec = even_hub.TextContainerSpec(
            x=0,
            y=0,
            width=SCREEN_WIDTH,
            height=SCREEN_HEIGHT,
            border_width=0,
            border_color=0,
            border_radius=0,
            padding_length=4,
            container_id=self._state.page.text_container_id,
            container_name="text-main",
            is_event_capture=True,
            content=normalized_text,
        )

        if self._state.page.startup_page_created:
            message = even_hub.rebuild_page_message(
                text_containers=[text_spec],
                image_containers=[],
                magic_random=self._send_manager.next_magic_random(),
            )
        else:
            message = even_hub.create_page_message(
                text_containers=[text_spec],
                image_containers=[],
                magic_random=self._send_manager.next_magic_random(),
            )
            self._state.page.startup_page_created = True

        await self._send_even_hub_command(message)
        self._state.page.page_created = True
        self._state.page.page_has_text_container = True
        self._state.page.page_has_fullscreen_text_container = True
        self._state.page.current_text_content = normalized_text

    def _extract_elements(self, request: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        """elements 配列と単一 image shorthand を同じ形へ寄せる。"""

        elements = list(request.get("elements") or [])
        if elements:
            return elements
        image_base64 = request.get("image_base64")
        if image_base64:
            return [
                {
                    "type": "image",
                    "image_base64": image_base64,
                    "x": int(request.get("x", 0)),
                    "y": int(request.get("y", 0)),
                    "width": int(request.get("width", SCREEN_WIDTH)),
                    "height": int(request.get("height", SCREEN_HEIGHT)),
                }
            ]
        return []

    def _build_layout_specs(
        self,
        elements: Sequence[Mapping[str, Any]],
        image_gamma: float = 1.0,
        image_dither: bool = False,
    ) -> Tuple[List[even_hub.TextContainerSpec], List[Any]]:
        """HTTP layout payload を EvenHub の text/image コンテナ群へ落とす。"""

        text_elements: List[Mapping[str, Any]] = []
        image_elements: List[Mapping[str, Any]] = []
        for element in elements:
            element_type = str(element.get("type", "")).lower()
            if element_type == "text":
                text_elements.append(element)
            elif element_type == "image":
                image_elements.append(element)
            else:
                raise ValueError(f"unsupported element type: {element_type}")

        capture_indices = [index for index, element in enumerate(text_elements) if element.get("capture_events")]
        if len(capture_indices) > 1:
            raise ValueError("at most one text element may have capture_events=true")

        text_specs: List[even_hub.TextContainerSpec] = []
        next_container_id = 1

        for index, element in enumerate(text_elements):
            full_text = str(element.get("text", "")) or " "
            self._validate_text_size(full_text, f"text element {index + 1}")
            capture = False
            if capture_indices:
                capture = index == capture_indices[0]
            elif index == 0:
                capture = True

            text_specs.append(
                even_hub.TextContainerSpec(
                    x=int(element.get("x", 0)),
                    y=int(element.get("y", 0)),
                    width=int(element.get("width", SCREEN_WIDTH)),
                    height=int(element.get("height", SCREEN_HEIGHT)),
                    border_width=int(element.get("border_width", 0)),
                    border_color=int(element.get("border_color", 0)),
                    border_radius=int(element.get("border_radius", 0)),
                    padding_length=int(element.get("padding", 0)),
                    container_id=next_container_id,
                    container_name=str(element.get("container_name") or f"text-{next_container_id}"),
                    is_event_capture=capture,
                    content=full_text,
                )
            )
            next_container_id += 1

        if not text_specs:
            text_specs.append(
                even_hub.TextContainerSpec(
                    x=0,
                    y=0,
                    width=SCREEN_WIDTH,
                    height=SCREEN_HEIGHT,
                    border_width=0,
                    border_color=0,
                    border_radius=0,
                    padding_length=0,
                    container_id=next_container_id,
                    container_name=f"text-{next_container_id}",
                    is_event_capture=True,
                    content=" ",
                )
            )
            next_container_id += 1

        image_tiles = []
        for element in image_elements:
            image_bytes = decode_base64_image(str(element.get("image_base64", "")))
            tiles = render_image_tiles(
                image_bytes=image_bytes,
                x=int(element.get("x", 0)),
                y=int(element.get("y", 0)),
                width=int(element.get("width", SCREEN_WIDTH)),
                height=int(element.get("height", SCREEN_HEIGHT)),
                container_id_start=next_container_id,
                gamma=image_gamma,
                dither=image_dither,
            )
            image_tiles.extend(tiles)
            next_container_id += len(tiles)

        if len(image_tiles) > 4:
            raise ValueError("layout cannot be applied: image container count exceeds 4")
        return text_specs, image_tiles

    def _validate_text_size(self, text: str, field_name: str) -> None:
        """外部入力テキストは UTF-8 1000 byte 制限へ統一する。"""

        if len(text.encode("utf-8")) > INITIAL_TEXT_MAX_BYTES:
            raise ValueError(f"{field_name} must be {INITIAL_TEXT_MAX_BYTES} bytes or fewer in UTF-8")

    async def _send_image_tile(self, tile: Any) -> None:
        """1 タイル分の BMP を 4096 byte fragment に分けて逐次送信する。"""

        fragment_size = 4096
        self._image_session_counter += 1
        session_id = self._image_session_counter
        total_size = len(tile.bmp_data)
        fragment_index = 0
        offset = 0

        while offset < total_size:
            end = min(offset + fragment_size, total_size)
            fragment = tile.bmp_data[offset:end]
            await self._send_even_hub_command(
                even_hub.update_image_raw_data_message(
                    container_id=tile.container_id,
                    container_name=tile.container_name,
                    map_session_id=session_id,
                    map_total_size=total_size,
                    map_fragment_index=fragment_index,
                    map_fragment_packet_size=len(fragment),
                    map_raw_data=fragment,
                )
            )
            fragment_index += 1
            offset = end
            await asyncio.sleep(self._config.image_fragment_interval_ms / 1000)

    async def _send_even_hub_command(self, payload: bytes, urgent: bool = False) -> None:
        """EvenHub service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.EVEN_HUB),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True, urgent=urgent)

    async def _send_dev_settings_command(self, payload: bytes, left: bool, right: bool, urgent: bool = False) -> None:
        """DevSettings service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.DEVICE_SETTINGS),
            payload=payload,
            reserve_flag=False,
        )
        await self._send_packets(packets, left=left, right=right, urgent=urgent)

    async def _send_g2_setting_command(self, payload: bytes) -> None:
        """G2Setting service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.G2_SETTING),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True)

    async def _send_calendar_command(self, payload: bytes) -> None:
        """Calendar service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.CALENDAR),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True)

    async def _send_dashboard_command(self, payload: bytes) -> None:
        """Dashboard service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.DASHBOARD),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True)

    async def _send_onboarding_command(self, payload: bytes) -> None:
        """Onboarding service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.ONBOARDING),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True)

    async def _send_even_ai_command(self, payload: bytes) -> None:
        """Even AI service へ payload を送る。"""

        packets = self._send_manager.build_packets(
            service_id=int(ServiceID.EVEN_AI),
            payload=payload,
            reserve_flag=True,
        )
        await self._send_packets(packets, left=False, right=True)

    async def _send_packets(
        self,
        packets: Sequence[bytes],
        left: bool,
        right: bool,
        urgent: bool = False,
    ) -> None:
        """packet 群を side 指定どおりに送る。

        Heartbeat のような urgent 書き込みは packet 境界で通常トラフィックより先に流す。
        これにより、画像 fragment や連続 text update が続いていても Heartbeat が飢餓しにくくなる。
        """

        if urgent:
            self._pending_urgent_writes += 1

        try:
            for index, packet in enumerate(packets):
                if not urgent:
                    await self._wait_for_urgent_writes()
                async with self._write_lock:
                    if self._disconnect_in_progress and not self._stop_event.is_set():
                        raise RuntimeError("cannot send while disconnect is in progress")
                    writes: List[Tuple[str, Awaitable[Any]]] = []
                    missing_sides = []
                    if right and self._right_client is not None:
                        right_is_disconnected = (
                            "right" in self._reconnecting_sides
                            or getattr(self._right_client, "is_connected", True) is False
                        )
                        if right_is_disconnected:
                            missing_sides.append("right")
                        else:
                            writes.append(
                                ("right", self._right_client.write_gatt_char(CHAR_WRITE, packet, response=False))
                            )
                    elif right:
                        missing_sides.append("right")
                    if left and self._left_client is not None:
                        left_is_disconnected = (
                            "left" in self._reconnecting_sides
                            or getattr(self._left_client, "is_connected", True) is False
                        )
                        if left_is_disconnected:
                            missing_sides.append("left")
                        else:
                            writes.append(
                                ("left", self._left_client.write_gatt_char(CHAR_WRITE, packet, response=False))
                            )
                    elif left:
                        missing_sides.append("left")
                    if missing_sides:
                        raise RuntimeError(f"cannot send to disconnected G2 lens: {', '.join(missing_sides)}")
                    if writes:
                        results = await asyncio.gather(
                            *(write for _, write in writes),
                            return_exceptions=True,
                        )
                        first_exception: Optional[Exception] = None
                        for (write_side, _), result in zip(writes, results):
                            if isinstance(result, Exception):
                                asyncio.create_task(self._handle_disconnect(write_side))
                                if first_exception is None:
                                    first_exception = result
                        if first_exception is not None:
                            raise first_exception
                if index < len(packets) - 1:
                    await asyncio.sleep(self._config.ble_packet_gap_ms / 1000)
        finally:
            if urgent:
                self._pending_urgent_writes = max(0, self._pending_urgent_writes - 1)

    async def _wait_for_urgent_writes(self) -> None:
        """urgent 送信待ちがあれば packet 境界で譲る。"""

        while self._pending_urgent_writes > 0 and not self._stop_event.is_set():
            await asyncio.sleep(0)

    def _queue_even_hub_command(self, payload: bytes) -> None:
        """最新の text update だけを残すようキューイングする。"""

        self._pending_text_msg = payload

    def _make_notify_callback(
        self,
        source_key: str,
        is_audio: bool,
        generation: int,
        side_generation: int,
    ) -> Callable[[Any, bytearray], None]:
        """Bleak の通知コールバックを asyncio タスクへ橋渡しする。"""

        def callback(_: Any, data: bytearray) -> None:
            if self._loop is None:
                return
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._handle_notification(bytes(data), source_key, is_audio, generation, side_generation),
            )

        return callback

    def _make_disconnect_callback(
        self,
        side: str,
        generation: int,
        side_generation: int,
    ) -> Callable[[Any], None]:
        """Bleak 切断通知を event loop 側へ戻す。"""

        def callback(client: Any) -> None:
            if self._loop is None:
                return
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._handle_disconnect_if_current(side, client, generation, side_generation),
            )

        return callback

    async def _handle_disconnect_if_current(
        self,
        side: str,
        client: Any,
        generation: int,
        side_generation: int,
    ) -> None:
        """stale な Bleak disconnect callback を無視する。"""

        if generation != self._connection_generation:
            LOGGER.debug("Ignoring stale disconnect callback from %s lens", side)
            return
        if side_generation != self._side_generations.get(side):
            LOGGER.debug("Ignoring stale side-generation disconnect callback from %s lens", side)
            return
        current_client = self._left_client if side == "left" else self._right_client
        if current_client is not client:
            LOGGER.debug("Ignoring disconnect from non-current %s client", side)
            return
        await self._handle_disconnect(side)

    async def _await_all_or_raise(self, *awaitables: Awaitable[Any]) -> None:
        """並列 awaitable 群をすべて settle させ、例外があれば最後に送出する。"""

        results = await asyncio.gather(*awaitables, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result

    async def _handle_disconnect(self, side: str) -> None:
        """片側切断を受けて state を落とし、再接続ループを起こす。"""

        if self._stop_event.is_set():
            return
        if side not in ("left", "right"):
            await self._handle_full_disconnect(side)
            return

        old_client: Optional[Any] = None
        should_reconnect_side = False
        full_disconnect_reason = ""
        async with self._disconnect_lock:
            if self._disconnect_in_progress:
                LOGGER.debug("Ignoring duplicate disconnect while already recovering: %s", side)
                return
            if side in self._reconnecting_sides:
                LOGGER.debug("Ignoring duplicate %s side disconnect while reconnecting", side)
                return

            other_side = "right" if side == "left" else "left"
            generation = self._connection_generation
            self._reconnecting_sides.add(side)
            self._side_generations[side] += 1
            self._state.ready = False

            if side == "left":
                old_client = self._left_client
                self._left_client = None
                self._state.left.connected = False
                self._state.left.authenticated = False
            else:
                old_client = self._right_client
                self._right_client = None
                self._state.right.connected = False
                self._state.right.authenticated = False
                self._state.page.reset_runtime_flags()
                self._state.mic_enabled = False

            should_reconnect_side = self._is_side_connected(other_side)
            if not should_reconnect_side:
                full_disconnect_reason = f"{side}_disconnected_without_peer"

        if old_client is not None:
            with suppress(Exception):
                await old_client.disconnect()

        if should_reconnect_side:
            LOGGER.warning("G2 disconnected: %s; reconnecting that side only", side)
            await self._emit(
                "connection.state",
                {"phase": ConnectionPhase.RECOVERING.value, "reason": f"{side}_disconnected_partial"},
            )
            await self._set_phase(ConnectionPhase.RECOVERING)
            self._start_side_reconnect_task(side, generation)
            return

        await self._handle_full_disconnect(full_disconnect_reason or f"{side}_disconnected")

    async def _handle_full_disconnect(self, reason: str) -> None:
        """両側を落として既存の full reconnect loop へ戻す。"""

        if self._stop_event.is_set():
            return
        async with self._disconnect_lock:
            if self._disconnect_in_progress:
                LOGGER.debug("Ignoring duplicate full disconnect while already recovering: %s", reason)
                return
            self._disconnect_in_progress = True
            self._connection_generation += 1
            self._state.ready = False
            self._state.page.reset_runtime_flags()
            self._state.mic_enabled = False
            self._state.left.connected = False
            self._state.right.connected = False
            self._state.left.authenticated = False
            self._state.right.authenticated = False

        LOGGER.warning("G2 disconnected: %s", reason)
        try:
            await self._emit(
                "connection.state",
                {"phase": ConnectionPhase.RECOVERING.value, "reason": reason},
            )
            await self._set_phase(ConnectionPhase.RECOVERING)
            await self._cancel_side_reconnect_tasks()
            await self._stop_background_tasks()
            await self._disconnect_clients()
        finally:
            self._disconnected_event.set()

    async def _handle_notification(
        self,
        raw_data: bytes,
        source_key: str,
        is_audio: bool,
        generation: int,
        side_generation: int,
    ) -> None:
        """notify / audio notify を正規化イベントへ変換する。"""

        if generation != self._connection_generation or self._disconnect_in_progress:
            return
        side = "left" if source_key == "L" else "right"
        if side_generation != self._side_generations.get(side):
            return
        if side in self._reconnecting_sides:
            return

        if self._config.debug_raw_events and not is_audio:
            await self._emit(
                "glasses.raw_packet",
                {"source": source_key, "size": len(raw_data), "hex": raw_data.hex()},
            )

        if is_audio:
            await self._handle_audio_data(raw_data, source_key)
            return

        result = self._receive_manager.handle_packet(raw_data, source_key)
        if result is None:
            return

        service_id, payload = result
        if service_id == int(ServiceID.EVEN_HUB):
            await self._handle_even_hub_payload(payload)
        elif service_id == int(ServiceID.DEVICE_SETTINGS):
            await self._handle_dev_settings_payload(payload, source_key)
        elif service_id == int(ServiceID.G2_SETTING):
            await self._handle_g2_setting_payload(payload)
        else:
            LOGGER.debug("Unhandled service id: %s", service_id)

    async def _handle_even_hub_payload(self, payload: bytes) -> None:
        """EvenHub 応答を state とイベントへ反映する。"""

        parsed = parse_even_hub_response(payload)
        for event in parsed["events"]:
            await self._emit(event["kind"], event["data"])

        menu_app_id = parsed.get("menu_app_id")
        if isinstance(menu_app_id, int):
            await self._emit("glasses.dashboard", {"action": "selected", "app_id": menu_app_id})

        runtime_reset_reason: Optional[str] = None
        if parsed.get("page_shutdown"):
            runtime_reset_reason = "page_shutdown"

        if parsed.get("system_exit"):
            runtime_reset_reason = parsed.get("system_exit_reason") or "system_exit"
            # ユーザーが手動停止した可能性が高いため、同一 BLE 接続内でもコンテンツを自動復元しない。
            self._state.page.last_display_request = None

        if runtime_reset_reason is not None:
            self._state.page.reset_runtime_flags()
            self._state.mic_enabled = False
            await self._emit_status_snapshot()
            # イベントを受信できている時点で BLE 接続自体は生きているため、
            # ここでは再接続せず、page/runtime 状態だけを同一接続上で戻す。
            await self._restore_runtime_after_page_reset(runtime_reset_reason)

    async def _handle_dev_settings_payload(self, payload: bytes, source_key: str) -> None:
        """DevSettings 応答を state とイベントへ反映する。"""

        parsed = parse_dev_settings_response(payload, source_key)
        for event in parsed["events"]:
            if event["kind"] == "glasses.authentication":
                authenticated = bool(event["data"].get("authenticated"))
                if source_key == "L":
                    self._state.left.authenticated = authenticated
                elif source_key == "R":
                    self._state.right.authenticated = authenticated
            await self._emit(event["kind"], event["data"])
        await self._emit_status_snapshot()

    async def _handle_g2_setting_payload(self, payload: bytes) -> None:
        """G2 setting 応答を state とイベントへ反映する。"""

        parsed = parse_g2_setting_response(payload)
        if parsed.get("battery") is not None:
            self._state.battery_level = int(parsed["battery"])
        if parsed.get("charging") is not None:
            self._state.charging = bool(parsed["charging"])
        firmware = parsed.get("firmware") or {}
        if "left" in firmware:
            self._state.left_firmware_version = str(firmware["left"])
        if "right" in firmware:
            self._state.right_firmware_version = str(firmware["right"])
            self._state.firmware_version = str(firmware["right"])

        for event in parsed["events"]:
            await self._emit(event["kind"], event["data"])
        await self._emit_status_snapshot()

    async def _handle_audio_data(self, data: bytes, source_key: str) -> None:
        """audio notify を重複排除しつつイベント化する。"""

        usable_length = min(len(data), 200)
        if usable_length < 40:
            return
        audio_data = data[:usable_length]
        if self._last_audio_frame == audio_data:
            return
        self._last_audio_frame = audio_data
        await self._emit(
            "glasses.mic_audio",
            {
                "source": source_key,
                "frame_size": usable_length,
                "data_base64": base64.b64encode(audio_data).decode("ascii"),
            },
        )

    async def _restore_runtime_after_page_reset(self, reason: str) -> None:
        """page ownership 喪失後に、BLE 再接続なしで startup page と状態を再確立する。"""

        async with self._reinitialize_lock:
            if not self._state.ready:
                return
            await self._emit("system.reinitialize", {"reason": reason})
            await self._ensure_startup_page(" ")
            cached_display = copy.deepcopy(self._state.page.last_display_request)
            if cached_display:
                await self.display(cached_display, remember=False)
            if self._state.target_mic_enabled:
                await self.set_mic_enabled(True)

    async def _set_phase(self, phase: ConnectionPhase) -> None:
        """phase 更新と connection/state snapshot 発行をまとめる。"""

        self._state.phase = phase
        if phase == ConnectionPhase.READY:
            self._schedule_last_error_clear_if_needed()
        else:
            await self._cancel_last_error_clear_task()
        await self._emit_connection_state()
        await self._emit_status_snapshot()

    def _schedule_last_error_clear_if_needed(self) -> None:
        """正常化後しばらくしてから last_error を消す。"""

        if not self._state.last_error:
            return
        if self._last_error_clear_task is not None and not self._last_error_clear_task.done():
            self._last_error_clear_task.cancel()
        self._last_error_clear_task = asyncio.create_task(self._clear_last_error_after_delay())

    async def _cancel_last_error_clear_task(self) -> None:
        """進行中の last_error clear timer を止める。"""

        if self._last_error_clear_task is None:
            return
        task = self._last_error_clear_task
        self._last_error_clear_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _clear_last_error_after_delay(self) -> None:
        """READY が続いたときだけ遅延後に last_error を消す。"""

        try:
            await asyncio.sleep(LAST_ERROR_CLEAR_DELAY_SEC)
            if self._state.phase != ConnectionPhase.READY or not self._state.ready:
                return
            if not self._state.last_error:
                return
            self._state.last_error = ""
            await self._emit_status_snapshot()
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if self._last_error_clear_task is current_task:
                self._last_error_clear_task = None

    async def _emit_connection_state(self) -> None:
        """接続フェーズの差分イベントを流す。"""

        await self._emit(
            "connection.state",
            {
                "phase": self._state.phase.value,
                "ready": self._state.ready,
                "left_connected": self._state.left.connected,
                "right_connected": self._state.right.connected,
            },
        )

    async def _emit_status_snapshot(self) -> None:
        """最新状態の完全スナップショットを流す。"""

        await self._emit("status.snapshot", self.get_status())

    async def _emit(self, kind: str, data: Dict[str, Any]) -> None:
        """登録された event handler へイベントを流す。"""

        if kind == "glasses.touch":
            self._state.last_gesture = str(data.get("gesture", ""))

        if self._event_handler is None:
            return
        event = self._event_factory.new(kind, data)
        maybe_awaitable = self._event_handler(event)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

        if kind == "glasses.touch":
            await self._emit_status_snapshot()

    async def record_touch_gesture(self, gesture: str) -> None:
        """HTTP などサーバー側から注入したジェスチャーを state に記録し snapshot を発行する。"""

        self._state.last_gesture = gesture
        await self._emit_status_snapshot()


__all__ = ["G2Client", "G2ClientConfig"]
