"""G2 応答の正規化とイベントエンベロープ生成。"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .constants import DevCfgCommandId, EvenHubResponseCmd, G2SettingCommandId, OsEventType
from .protobuf import ProtobufReader


def _timestamp_utc() -> str:
    """イベント timestamp を UTC ISO-8601 で返す。"""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class EventEnvelope:
    """WebSocket と CLI に流す標準イベント形式。"""

    seq: int
    kind: str
    timestamp: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """JSON 化しやすい辞書へ変換する。"""

        return {
            "seq": self.seq,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "data": self.data,
        }


class EventFactory:
    """単調増加する seq を付与してイベントを生成する。"""

    def __init__(self) -> None:
        self._seq = 0

    def new(self, kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """1 件分のイベントエンベロープを辞書として返す。"""

        self._seq += 1
        return EventEnvelope(
            seq=self._seq,
            kind=kind,
            timestamp=_timestamp_utc(),
            data=data,
        ).to_dict()


def map_event_type_to_gesture(event_type: OsEventType) -> Optional[str]:
    """Kotlin 実装と同じ gesture 名へ変換する。"""

    mapping = {
        OsEventType.CLICK: "single_tap",
        OsEventType.DOUBLE_CLICK: "double_tap",
        OsEventType.SCROLL_TOP: "swipe_up",
        OsEventType.SCROLL_BOTTOM: "swipe_down",
        OsEventType.FOREGROUND_ENTER: "foreground_enter",
        OsEventType.FOREGROUND_EXIT: "foreground_exit",
        OsEventType.SYSTEM_EXIT: "system_exit",
    }
    return mapping.get(event_type)


def _parse_touch_event(dev_event_data: bytes) -> Dict[str, Any]:
    """EvenHub の SendDeviceEvent を解析する。"""

    result: Dict[str, Any] = {
        "events": [],
        "system_exit": False,
        "system_exit_reason": None,
    }
    fields = ProtobufReader(dev_event_data).parse_fields()

    sys_data = fields.get(3)
    if isinstance(sys_data, bytes):
        sys_fields = ProtobufReader(sys_data).parse_fields()
        normal_type = sys_fields.get(1)
        event_type = OsEventType.from_int(normal_type if isinstance(normal_type, int) else 0)
        event_source = sys_fields.get(2) if isinstance(sys_fields.get(2), int) else None
        if event_type is not None:
            gesture = map_event_type_to_gesture(event_type)
            if gesture is not None:
                result["events"].append(
                    {
                        "kind": "glasses.touch",
                        "data": {"gesture": gesture, "source": event_source},
                    }
                )
            if event_type in (OsEventType.SYSTEM_EXIT, OsEventType.ABNORMAL_EXIT):
                result["system_exit"] = True
                result["system_exit_reason"] = event_type.name.lower()
        return result

    text_data = fields.get(2)
    if isinstance(text_data, bytes):
        text_fields = ProtobufReader(text_data).parse_fields()
        raw_type = text_fields.get(3)
        if isinstance(raw_type, int):
            event_type = OsEventType.from_int(raw_type)
            if event_type is not None:
                gesture = map_event_type_to_gesture(event_type)
                if gesture is not None:
                    result["events"].append(
                        {
                            "kind": "glasses.touch",
                            "data": {"gesture": gesture},
                        }
                    )
    return result


def parse_even_hub_response(payload: bytes) -> Dict[str, Any]:
    """EvenHub 応答 payload をイベント候補と制御フラグへ分解する。"""

    result: Dict[str, Any] = {
        "cmd": None,
        "events": [],
        "page_shutdown": False,
        "system_exit": False,
        "system_exit_reason": None,
        "menu_app_id": None,
        "errors": [],
    }
    fields = ProtobufReader(payload).parse_fields()
    cmd_value = fields.get(1)
    if not isinstance(cmd_value, int):
        return result

    result["cmd"] = cmd_value
    if cmd_value == int(EvenHubResponseCmd.OS_NOTIFY_EVENT_TO_APP):
        dev_event_data = fields.get(13)
        if isinstance(dev_event_data, bytes):
            parsed = _parse_touch_event(dev_event_data)
            result["events"].extend(parsed["events"])
            result["system_exit"] = bool(parsed["system_exit"])
            result["system_exit_reason"] = parsed["system_exit_reason"]
        return result

    if cmd_value == 17:
        select_data = fields.get(20)
        if isinstance(select_data, bytes):
            select_fields = ProtobufReader(select_data).parse_fields()
            app_id = select_fields.get(1)
            if isinstance(app_id, int):
                result["menu_app_id"] = app_id
        return result

    for response_field in (4, 6, 8, 10):
        response_data = fields.get(response_field)
        if not isinstance(response_data, bytes):
            continue
        response_fields = ProtobufReader(response_data).parse_fields()
        primary_error = response_fields.get(1)
        image_error = response_fields.get(8)
        if isinstance(primary_error, int):
            result["errors"].append(primary_error)
            if primary_error == 9:
                result["page_shutdown"] = True
        if isinstance(image_error, int):
            result["errors"].append(image_error)

    if cmd_value in (9, 10):
        result["page_shutdown"] = True
    return result


def parse_dev_settings_response(payload: bytes, source_key: str) -> Dict[str, Any]:
    """DevSettings 応答をイベント候補へ変換する。"""

    result: Dict[str, Any] = {
        "cmd": None,
        "events": [],
        "authenticated": False,
        "ring_status": None,
    }
    fields = ProtobufReader(payload).parse_fields()
    cmd_value = fields.get(1)
    if not isinstance(cmd_value, int):
        return result

    result["cmd"] = cmd_value
    if cmd_value == int(DevCfgCommandId.BASE_CONN_HEART_BEAT):
        return result

    if cmd_value == int(DevCfgCommandId.AUTHENTICATION):
        auth_data = fields.get(3)
        if isinstance(auth_data, bytes):
            auth_fields = ProtobufReader(auth_data).parse_fields()
            sec_auth = auth_fields.get(1)
            authenticated = bool(sec_auth) if isinstance(sec_auth, int) else False
            result["authenticated"] = authenticated
            result["events"].append(
                {
                    "kind": "glasses.authentication",
                    "data": {"source": source_key, "authenticated": authenticated},
                }
            )

    if cmd_value == int(DevCfgCommandId.RING_CONNECT_INFO):
        ring_data = fields.get(5)
        if isinstance(ring_data, bytes):
            ring_fields = ProtobufReader(ring_data).parse_fields()
            ring_status = ring_fields.get(4)
            if isinstance(ring_status, int):
                result["ring_status"] = ring_status

    return result


def _parse_device_request_response(data: bytes) -> Dict[str, Any]:
    """deviceReceiveRequest の応答から battery と version を抜き出す。"""

    parsed: Dict[str, Any] = {"events": [], "battery": None, "charging": None, "firmware": {}}
    fields = ProtobufReader(data).parse_fields()

    battery = fields.get(12)
    if isinstance(battery, int) and 0 <= battery <= 100:
        parsed["battery"] = battery

    charging = fields.get(13)
    if isinstance(charging, int):
        parsed["charging"] = charging != 0

    left_version = fields.get(5)
    right_version = fields.get(6)
    if isinstance(left_version, bytes):
        parsed["firmware"]["left"] = left_version.decode("utf-8", errors="replace")
    if isinstance(right_version, bytes):
        parsed["firmware"]["right"] = right_version.decode("utf-8", errors="replace")

    if parsed["battery"] is not None:
        parsed["events"].append(
            {
                "kind": "glasses.battery",
                "data": {
                    "level": parsed["battery"],
                    "charging": parsed["charging"],
                },
            }
        )

    if parsed["firmware"]:
        parsed["events"].append(
            {
                "kind": "glasses.firmware",
                "data": parsed["firmware"],
            }
        )

    return parsed


def parse_g2_setting_response(payload: bytes) -> Dict[str, Any]:
    """G2 setting 応答から battery、charging、firmware 情報を抜き出す。"""

    result: Dict[str, Any] = {
        "cmd": None,
        "events": [],
        "battery": None,
        "charging": None,
        "firmware": {},
        "silent_mode": None,
    }
    fields = ProtobufReader(payload).parse_fields()
    cmd_value = fields.get(1)
    if not isinstance(cmd_value, int):
        return result

    result["cmd"] = cmd_value
    if cmd_value in (
        int(G2SettingCommandId.DEVICE_RECEIVE_REQUEST),
        int(G2SettingCommandId.DEVICE_SEND_TO_APP),
    ):
        response_data = fields.get(4)
        if isinstance(response_data, bytes):
            parsed = _parse_device_request_response(response_data)
            result["events"].extend(parsed["events"])
            result["battery"] = parsed["battery"]
            result["charging"] = parsed["charging"]
            result["firmware"] = parsed["firmware"]

        send_to_app_data = fields.get(5)
        if isinstance(send_to_app_data, bytes):
            sub_fields = ProtobufReader(send_to_app_data).parse_fields()
            silent_mode = sub_fields.get(2)
            if isinstance(silent_mode, int):
                result["silent_mode"] = silent_mode != 0
    return result


__all__ = [
    "EventEnvelope",
    "EventFactory",
    "map_event_type_to_gesture",
    "parse_dev_settings_response",
    "parse_even_hub_response",
    "parse_g2_setting_response",
]
