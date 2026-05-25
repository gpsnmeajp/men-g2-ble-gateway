"""G2.kt と設計文書から確定している定数定義。

このモジュールは、BLE transport と protocol builder の両方から参照される
共通値を 1 か所へ集める。
"""

from enum import IntEnum
from uuid import UUID

# EvenHub BLE characteristic UUIDs。
CHAR_WRITE = UUID("00002760-08C2-11E1-9073-0E8AC72E5401")
CHAR_NOTIFY = UUID("00002760-08C2-11E1-9073-0E8AC72E5402")
AUDIO_NOTIFY = UUID("00002760-08C2-11E1-9073-0E8AC72E6402")
SERVICE_UUID = UUID("00002760-08C2-11E1-9073-0E8AC72E0000")
CLIENT_CHARACTERISTIC_CONFIG = UUID("00002902-0000-1000-8000-00805F9B34FB")

# G2 BLE パケット基本定数。
HEADER_BYTE = 0xAA
SOURCE_PHONE = 0x01
DEST_GLASSES = 0x02
MAX_PACKET_PAYLOAD = 236

# Even Hub の公式制約から採用した表示定数。
SCREEN_WIDTH = 576
SCREEN_HEIGHT = 288
DISPLAY_BIT_DEPTH = 4
DISPLAY_GRAYSCALE_LEVELS = 16
MAX_PAGE_CONTAINERS = 12
MAX_TEXT_LIST_CONTAINERS = 8
MAX_IMAGE_CONTAINERS = 4
CONTAINER_NAME_MAX_CHARS = 16
IMAGE_MIN_WIDTH = 20
IMAGE_MAX_WIDTH = 288
IMAGE_MIN_HEIGHT = 20
IMAGE_MAX_HEIGHT = 144
INITIAL_TEXT_MAX_BYTES = 1000


class ServiceID(IntEnum):
    """service_id_def.proto に対応する service ID。"""

    DASHBOARD = 0x01
    CALENDAR = 0x04
    MENU = 0x03
    EVEN_AI = 0x07
    G2_SETTING = 0x09
    GESTURE_CTRL = 0x0D
    ONBOARDING = 0x10
    DEVICE_SETTINGS = 0x80
    EVEN_HUB_CTRL = 0x81
    EVEN_HUB = 0xE0

    @classmethod
    def from_byte(cls, value: int):
        """符号付き/符号なしを問わず 1 byte 値から enum を引く。"""

        try:
            return cls(value & 0xFF)
        except ValueError:
            return None


class EvenHubCmd(IntEnum):
    """EvenHub.proto の主要コマンド ID。"""

    CREATE_STARTUP_PAGE = 0
    UPDATE_IMAGE_RAW_DATA = 3
    UPDATE_TEXT_DATA = 5
    REBUILD_PAGE = 7
    SHUTDOWN_PAGE = 9
    HEARTBEAT = 12
    AUDIO_CONTROL = 15


class EvenHubResponseCmd(IntEnum):
    """グラス側から返る EvenHub 応答コマンド。"""

    OS_NOTIFY_EVENT_TO_APP = 2


class OsEventType(IntEnum):
    """EvenHub.proto に定義される OS イベント種別。"""

    CLICK = 0
    SCROLL_TOP = 1
    SCROLL_BOTTOM = 2
    DOUBLE_CLICK = 3
    FOREGROUND_ENTER = 4
    FOREGROUND_EXIT = 5
    ABNORMAL_EXIT = 6
    SYSTEM_EXIT = 7

    @classmethod
    def from_int(cls, value: int):
        """整数値からイベント種別を安全に逆引きする。"""

        try:
            return cls(value)
        except ValueError:
            return None


class G2SettingCommandId(IntEnum):
    """g2_setting.proto の commandId。"""

    NONE = 0
    DEVICE_RECEIVE_INFO = 1
    DEVICE_RECEIVE_REQUEST = 2
    DEVICE_SEND_TO_APP = 3
    DEVICE_RESPOND_TO_APP = 4


class DevCfgCommandId(IntEnum):
    """dev_config_protocol.proto の commandId。"""

    AUTHENTICATION = 4
    PIPE_ROLE_CHANGE = 5
    RING_CONNECT_INFO = 6
    BASE_CONN_HEART_BEAT = 14
    TIME_SYNC = 128


__all__ = [
    "AUDIO_NOTIFY",
    "CHAR_NOTIFY",
    "CHAR_WRITE",
    "CLIENT_CHARACTERISTIC_CONFIG",
    "CONTAINER_NAME_MAX_CHARS",
    "DEST_GLASSES",
    "DISPLAY_BIT_DEPTH",
    "DISPLAY_GRAYSCALE_LEVELS",
    "DevCfgCommandId",
    "EvenHubCmd",
    "EvenHubResponseCmd",
    "G2SettingCommandId",
    "HEADER_BYTE",
    "IMAGE_MAX_HEIGHT",
    "IMAGE_MAX_WIDTH",
    "IMAGE_MIN_HEIGHT",
    "IMAGE_MIN_WIDTH",
    "INITIAL_TEXT_MAX_BYTES",
    "MAX_IMAGE_CONTAINERS",
    "MAX_PACKET_PAYLOAD",
    "MAX_PAGE_CONTAINERS",
    "MAX_TEXT_LIST_CONTAINERS",
    "OsEventType",
    "SCREEN_HEIGHT",
    "SCREEN_WIDTH",
    "SERVICE_UUID",
    "SOURCE_PHONE",
    "ServiceID",
]
