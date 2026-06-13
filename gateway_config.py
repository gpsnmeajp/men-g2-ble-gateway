"""gateway.yaml の読み書きを担う設定層。"""

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, MutableMapping, Optional


DEFAULT_CONFIG_PATH = Path("config") / "gateway.yaml"


def _require_yaml():
    """PyYAML を遅延 import し、未導入時は意味のある例外へ変換する。"""

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required. Install it before using gateway_config.py."
        ) from exc
    return yaml


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    """dict 相当の設定節だけを受け入れる。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("each section in the config file must be a mapping")
    return value


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("config value must be a string or list of strings")


@dataclass
class ServerConfig:
    """HTTP/WebSocket/静的配信に関する設定。"""

    host: str = "0.0.0.0"
    port: int = 8765
    websocket_path: str = "/ws"
    static_dir: str = "ui"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ServerConfig":
        """mapping から ServerConfig を復元する。"""

        return cls(
            host=str(data.get("host", cls.host)),
            port=int(data.get("port", cls.port)),
            websocket_path=str(data.get("websocket_path", cls.websocket_path)),
            static_dir=str(data.get("static_dir", cls.static_dir)),
        )


@dataclass
class GlassConfig:
    """左右グラスとシリアル番号の永続化設定。"""

    search_id: str = ""
    left_address: str = ""
    right_address: str = ""
    left_mac_address: str = ""
    right_mac_address: str = ""
    last_serial_number: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GlassConfig":
        """mapping から GlassConfig を復元する。"""

        return cls(
            search_id=str(data.get("search_id", cls.search_id)),
            left_address=str(data.get("left_address", cls.left_address)),
            right_address=str(data.get("right_address", cls.right_address)),
            left_mac_address=str(data.get("left_mac_address", cls.left_mac_address)),
            right_mac_address=str(data.get("right_mac_address", cls.right_mac_address)),
            last_serial_number=str(data.get("last_serial_number", cls.last_serial_number)),
        )


@dataclass
class BleConfig:
    """BLE タイミング系設定。"""

    scan_timeout_sec: int = 5
    reconnect_interval_sec: int = 5
    heartbeat_interval_sec: int = 5
    ble_packet_gap_ms: int = 8
    text_queue_interval_ms: int = 100
    image_settle_delay_ms: int = 1000
    image_fragment_interval_ms: int = 200
    unpair_on_startup: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "BleConfig":
        """mapping から BleConfig を復元する。"""

        return cls(
            scan_timeout_sec=int(data.get("scan_timeout_sec", cls.scan_timeout_sec)),
            reconnect_interval_sec=int(data.get("reconnect_interval_sec", cls.reconnect_interval_sec)),
            heartbeat_interval_sec=int(data.get("heartbeat_interval_sec", cls.heartbeat_interval_sec)),
            ble_packet_gap_ms=int(data.get("ble_packet_gap_ms", cls.ble_packet_gap_ms)),
            text_queue_interval_ms=int(data.get("text_queue_interval_ms", cls.text_queue_interval_ms)),
            image_settle_delay_ms=int(data.get("image_settle_delay_ms", cls.image_settle_delay_ms)),
            image_fragment_interval_ms=int(data.get("image_fragment_interval_ms", cls.image_fragment_interval_ms)),
            unpair_on_startup=bool(data.get("unpair_on_startup", cls.unpair_on_startup)),
        )


@dataclass
class GuiConfig:
    """Tk GUI 有効/無効だけを今は保持する。"""

    enabled: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GuiConfig":
        """mapping から GuiConfig を復元する。"""

        return cls(enabled=bool(data.get("enabled", cls.enabled)))


@dataclass
class McpConfig:
    """AI エージェントアクセス用の FastMCP サーバー設定。"""

    enabled: bool = False  # FastMCP サーバーを有効化するかどうか
    host: str = "127.0.0.1"  # FastMCP サーバーの待受ホスト（セキュリティ上、既定は localhost）
    port: int = 8766  # FastMCP サーバーの待受ポート
    path: str = "/mcp"  # FastMCP サーバーの HTTP パス

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "McpConfig":
        defaults = cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled"), defaults.enabled),
            host=str(data.get("host", defaults.host)),
            port=int(data.get("port", defaults.port)),
            path=str(data.get("path", defaults.path)),
        )


@dataclass
class CorsConfig:
    """CORS settings for browser clients served from a different origin."""

    enabled: bool = False
    allow_origins: list[str] = field(default_factory=list)
    allow_methods: list[str] = field(default_factory=lambda: ["GET", "POST", "OPTIONS"])
    allow_headers: list[str] = field(default_factory=lambda: ["Content-Type", "Authorization", "X-API-Key"])
    allow_credentials: bool = False
    max_age: int = 600

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CorsConfig":
        defaults = cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled"), defaults.enabled),
            allow_origins=_coerce_string_list(data.get("allow_origins", defaults.allow_origins)),
            allow_methods=_coerce_string_list(data.get("allow_methods", defaults.allow_methods)),
            allow_headers=_coerce_string_list(data.get("allow_headers", defaults.allow_headers)),
            allow_credentials=_coerce_bool(data.get("allow_credentials"), defaults.allow_credentials),
            max_age=int(data.get("max_age", defaults.max_age)),
        )


@dataclass
class AuthConfig:
    """API key authentication settings."""

    api_key: str = ""
    header_name: str = "X-API-Key"
    query_parameter: str = "api_key"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AuthConfig":
        defaults = cls()
        return cls(
            api_key=str(data.get("api_key", defaults.api_key)),
            header_name=str(data.get("header_name", defaults.header_name)),
            query_parameter=str(data.get("query_parameter", defaults.query_parameter)),
        )


@dataclass
class GatewayConfig:
    """gateway.yaml 全体の型付き表現。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    glass: GlassConfig = field(default_factory=GlassConfig)
    ble: BleConfig = field(default_factory=BleConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    cors: CorsConfig = field(default_factory=CorsConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GatewayConfig":
        """YAML 読み込み結果から型付き設定へ変換する。"""

        return cls(
            server=ServerConfig.from_mapping(_coerce_mapping(data.get("server"))),
            glass=GlassConfig.from_mapping(_coerce_mapping(data.get("glass"))),
            ble=BleConfig.from_mapping(_coerce_mapping(data.get("ble"))),
            gui=GuiConfig.from_mapping(_coerce_mapping(data.get("gui"))),
            mcp=McpConfig.from_mapping(_coerce_mapping(data.get("mcp"))),
            cors=CorsConfig.from_mapping(_coerce_mapping(data.get("cors"))),
            auth=AuthConfig.from_mapping(_coerce_mapping(data.get("auth"))),
        )

    def to_dict(self) -> MutableMapping[str, Any]:
        """保存用の辞書へ変換する。"""

        return asdict(self)


class GatewayConfigStore:
    """gateway.yaml の load/save を担う小さなストア。"""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = Path(path)

    def load(self) -> GatewayConfig:
        """設定ファイルがあれば読み、無ければ既定値を返す。"""

        if not self.path.exists():
            return GatewayConfig()

        yaml = _require_yaml()
        with self.path.open("r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file) or {}
        mapping = _coerce_mapping(loaded)
        return GatewayConfig.from_mapping(mapping)

    def save(self, config: GatewayConfig) -> None:
        """一時ファイル経由で安全に設定を保存する。"""

        yaml = _require_yaml()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        file_descriptor, temp_path_str = tempfile.mkstemp(
            prefix=f"{self.path.stem}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temp_path = Path(temp_path_str)

        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
                yaml.safe_dump(
                    config.to_dict(),
                    file,
                    allow_unicode=True,
                    sort_keys=False,
                )
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)


def load_gateway_config(path: Optional[Path] = None) -> GatewayConfig:
    """単発利用向けの load ヘルパー。"""

    store = GatewayConfigStore(path or DEFAULT_CONFIG_PATH)
    return store.load()


def save_gateway_config(config: GatewayConfig, path: Optional[Path] = None) -> None:
    """単発利用向けの save ヘルパー。"""

    store = GatewayConfigStore(path or DEFAULT_CONFIG_PATH)
    store.save(config)


__all__ = [
    "AuthConfig",
    "BleConfig",
    "CorsConfig",
    "DEFAULT_CONFIG_PATH",
    "GatewayConfig",
    "GatewayConfigStore",
    "GlassConfig",
    "GuiConfig",
    "McpConfig",
    "ServerConfig",
    "load_gateway_config",
    "save_gateway_config",
]
