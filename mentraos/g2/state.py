"""G2 クライアントのランタイム状態を保持する。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ConnectionPhase(str, Enum):
    """接続フェーズを外部へ公開するための列挙体。"""

    IDLE = "idle"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    INITIALIZING = "initializing"
    READY = "ready"
    RECOVERING = "recovering"
    STOPPED = "stopped"


@dataclass
class LensState:
    """左右いずれか片側レンズの接続状態。"""

    side: str
    address: str = ""
    mac_address: str = ""
    name: str = ""
    connected: bool = False
    authenticated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """JSON 化しやすい辞書へ変換する。"""

        return {
            "side": self.side,
            "address": self.address,
            "mac_address": self.mac_address,
            "name": self.name,
            "connected": self.connected,
            "authenticated": self.authenticated,
        }


@dataclass
class PageState:
    """現在グラス上に持っているページ状態。"""

    startup_page_created: bool = False
    page_created: bool = False
    page_has_text_container: bool = False
    page_has_fullscreen_text_container: bool = False
    text_container_id: int = 1
    current_text_content: str = ""
    last_display_request: Optional[Dict[str, Any]] = None

    def reset_runtime_flags(self) -> None:
        """切断や system exit 後にページ所有状態だけを落とす。"""

        self.startup_page_created = False
        self.page_created = False
        self.page_has_text_container = False
        self.page_has_fullscreen_text_container = False
        self.current_text_content = ""


@dataclass
class ClientState:
    """G2Client 全体の外向け状態。"""

    phase: ConnectionPhase = ConnectionPhase.IDLE
    ready: bool = False
    search_id: str = ""
    last_serial_number: str = ""
    mic_enabled: bool = False
    target_mic_enabled: bool = False
    battery_level: int = -1
    charging: bool = False
    left_firmware_version: str = ""
    right_firmware_version: str = ""
    firmware_version: str = ""
    last_error: str = ""
    last_gesture: str = ""
    left: LensState = field(default_factory=lambda: LensState(side="left"))
    right: LensState = field(default_factory=lambda: LensState(side="right"))
    page: PageState = field(default_factory=PageState)

    def to_dict(self) -> Dict[str, Any]:
        """REST と WebSocket へ返す状態スナップショットを作る。"""

        return {
            "phase": self.phase.value,
            "ready": self.ready,
            "search_id": self.search_id,
            "last_serial_number": self.last_serial_number,
            "mic_enabled": self.mic_enabled,
            "target_mic_enabled": self.target_mic_enabled,
            "battery_level": self.battery_level,
            "charging": self.charging,
            "left_firmware_version": self.left_firmware_version,
            "right_firmware_version": self.right_firmware_version,
            "firmware_version": self.firmware_version,
            "last_error": self.last_error,
            "last_gesture": self.last_gesture,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "page": {
                "startup_page_created": self.page.startup_page_created,
                "page_created": self.page.page_created,
                "page_has_text_container": self.page.page_has_text_container,
                "page_has_fullscreen_text_container": self.page.page_has_fullscreen_text_container,
                "text_container_id": self.page.text_container_id,
                "current_text_content": self.page.current_text_content,
                "has_cached_display_request": self.page.last_display_request is not None,
            },
        }


__all__ = ["ClientState", "ConnectionPhase", "LensState", "PageState"]
