"""menu サービス向け protobuf builder。

初回サーバーマイルストーンでは HTTP/CLI/UI からは使わないが、
G2.kt 由来の pure builder としてモジュールだけ先に用意しておく。
"""

from dataclasses import dataclass
import math
from typing import Dict, List, Sequence, Tuple

from ..protobuf import ProtobufWriter, to_signed_32

MIN_MENU_SIZE = 5
MAX_MENU_SIZE = 10
MAX_NAME_LENGTH = 15
PLACEHOLDER_APP_IDS = [10535, 10536, 10537, 10538, 10539]


@dataclass(frozen=True)
class MenuItem:
    """外部アプリ menu 項目 1 件分の定義。"""

    package_name: str
    name: str
    running: bool


def _kotlin_abs_int(value: int) -> int:
    """Kotlin の kotlin.math.abs(Int) に寄せた絶対値計算。"""

    if value == -0x80000000:
        return value
    return abs(value)


def _kotlin_remainder(dividend: int, divisor: int) -> int:
    """Kotlin/Java の剰余と同じく 0 方向へ丸めた除算を前提に余りを返す。"""

    return dividend - math.trunc(dividend / divisor) * divisor


def package_name_to_app_id(package_name: str) -> int:
    """Kotlin 実装と同じ決定的 appId へ package 名を写像する。"""

    hash_value = 0
    for char in package_name:
        hash_value = to_signed_32((hash_value << 5) - hash_value + ord(char))
    remainder = _kotlin_remainder(_kotlin_abs_int(hash_value), 506)
    return 10029 + remainder


def send_menu_info(magic_random: int, items: Sequence[MenuItem]) -> Tuple[bytes, Dict[int, str]]:
    """APP_SEND_MENU_INFO message を構築し、逆引き用 appId map も返す。"""

    app_id_map = {}
    wire_items: List[Tuple[str, int, bool]] = []

    # 先頭は常に built-in の Notification 項目。
    wire_items.append(("", 4, True))

    for item in items[: MAX_MENU_SIZE - 1]:
        app_id = package_name_to_app_id(item.package_name)
        app_id_map[app_id] = item.package_name
        truncated = item.name[:MAX_NAME_LENGTH]
        prefix = "● " if item.running else "  "
        wire_items.append((prefix + truncated, app_id, False))

    while len(wire_items) < MIN_MENU_SIZE:
        placeholder_index = len(wire_items) - 1
        wire_items.append(("  ---", PLACEHOLDER_APP_IDS[placeholder_index], False))

    menu_writer = ProtobufWriter()
    menu_writer.write_int32_field(1, len(wire_items))

    for display_name, app_id, is_built_in in wire_items:
        item_writer = ProtobufWriter()
        if is_built_in:
            item_writer.write_int32_field(1, 0)
            item_writer.write_int32_field(4, app_id)
        else:
            item_writer.write_int32_field(1, 1)
            item_writer.write_int32_field(2, 1)
            item_writer.write_string_field(3, display_name)
            item_writer.write_int32_field(4, app_id)
        menu_writer.write_message_field(2, item_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, 0)
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, menu_writer.to_bytes())
    return writer.to_bytes(), app_id_map


__all__ = [
    "MAX_MENU_SIZE",
    "MAX_NAME_LENGTH",
    "MIN_MENU_SIZE",
    "MenuItem",
    "PLACEHOLDER_APP_IDS",
    "package_name_to_app_id",
    "send_menu_info",
]
