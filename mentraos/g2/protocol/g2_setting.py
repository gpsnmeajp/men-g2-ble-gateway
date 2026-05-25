"""g2_setting サービス向け protobuf builder。"""

from ..constants import G2SettingCommandId
from ..protobuf import ProtobufWriter


def set_brightness(magic_random: int, level: int, auto_adjust: bool) -> bytes:
    """明るさ設定 message を構築する。"""

    brightness_writer = ProtobufWriter()
    brightness_writer.write_int32_field(1, 1 if auto_adjust else 0)
    brightness_writer.write_int32_field(2, level)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(1, brightness_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


def request_info(magic_random: int) -> bytes:
    """基本デバイス情報要求 message を構築する。"""

    request_writer = ProtobufWriter()
    request_writer.write_int32_field(1, 1)

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_REQUEST))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(4, request_writer.to_bytes())
    return writer.to_bytes()


def set_universe_settings(
    magic_random: int,
    unit_format: int = 0,
    distance_unit: int = 0,
    time_format: int = 1,
    date_format: int = 0,
    temperature_unit: int = 1,
) -> bytes:
    """G2.kt が接続時に送る universe settings を構築する。"""

    universe_writer = ProtobufWriter()
    universe_writer.write_int32_field(1, unit_format)
    universe_writer.write_int32_field(2, distance_unit)
    universe_writer.write_int32_field(3, time_format)
    universe_writer.write_int32_field(4, date_format)
    universe_writer.write_int32_field(5, temperature_unit)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(9, universe_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


def set_head_up_switch(magic_random: int, enabled: bool) -> bytes:
    """head up switch 設定 message を構築する。"""

    head_up_writer = ProtobufWriter()
    head_up_writer.write_int32_field(1, 1 if enabled else 0)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(4, head_up_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


def set_head_up_angle(magic_random: int, angle: int) -> bytes:
    """head up angle 設定 message を構築する。"""

    head_up_writer = ProtobufWriter()
    head_up_writer.write_int32_field(2, angle)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(4, head_up_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


def set_screen_height(magic_random: int, level: int) -> bytes:
    """Y 座標補正 message を構築する。"""

    y_writer = ProtobufWriter()
    y_writer.write_int32_field(1, level)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(2, y_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


def set_screen_depth(magic_random: int, level: int) -> bytes:
    """X 座標補正 message を構築する。"""

    x_writer = ProtobufWriter()
    x_writer.write_int32_field(1, level)

    info_writer = ProtobufWriter()
    info_writer.write_message_field(3, x_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(G2SettingCommandId.DEVICE_RECEIVE_INFO))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, info_writer.to_bytes())
    return writer.to_bytes()


__all__ = [
    "request_info",
    "set_brightness",
    "set_head_up_angle",
    "set_head_up_switch",
    "set_screen_depth",
    "set_screen_height",
    "set_universe_settings",
]
