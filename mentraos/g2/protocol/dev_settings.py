"""DevSettings サービス向け protobuf builder。"""

from datetime import datetime
from typing import Optional

from ..constants import DevCfgCommandId
from ..protobuf import ProtobufWriter


def auth_cmd(magic_random: int) -> bytes:
    """AUTHENTICATION コマンドを構築する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(DevCfgCommandId.AUTHENTICATION))
    writer.write_int32_field(2, magic_random)

    auth_writer = ProtobufWriter()
    auth_writer.write_bool_field(1, True)
    auth_writer.write_int32_field(2, 4)

    writer.write_message_field(3, auth_writer.to_bytes())
    return writer.to_bytes()


def pipe_role_change(magic_random: int) -> bytes:
    """RIGHT 側を command role とする pipe role change を構築する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(DevCfgCommandId.PIPE_ROLE_CHANGE))
    writer.write_int32_field(2, magic_random)

    role_writer = ProtobufWriter()
    role_writer.write_int32_field(1, 1)

    writer.write_message_field(4, role_writer.to_bytes())
    return writer.to_bytes()


def time_sync(
    magic_random: int,
    timestamp_seconds: Optional[int] = None,
    timezone_offset_hours: Optional[int] = None,
) -> bytes:
    """TIME_SYNC コマンドを構築する。

    引数を省略した場合は現在時刻とローカルタイムゾーンから値を計算する。
    テスト容易性のため、時刻とタイムゾーンは上書きできるようにしている。
    """

    now = datetime.now().astimezone()
    offset = now.utcoffset()
    offset_seconds = 0 if offset is None else int(offset.total_seconds())
    if timestamp_seconds is None:
        # G2 の dashboard clock は timestamp を UTC epoch として再解釈せず、
        # 壁時計秒として使う挙動があるため、ローカル時刻へ補正して送る。
        timestamp_seconds = int(now.timestamp()) + offset_seconds
    if timezone_offset_hours is None:
        # timestamp 側に offset を畳み込んでいるので、端末側の二重補正を避ける。
        timezone_offset_hours = 0

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(DevCfgCommandId.TIME_SYNC))
    writer.write_int32_field(2, magic_random)

    time_writer = ProtobufWriter()
    time_writer.write_int32_field(1, timestamp_seconds)
    time_writer.write_int32_field(2, timezone_offset_hours)

    writer.write_message_field(128, time_writer.to_bytes())
    return writer.to_bytes()


def base_heartbeat(magic_random: int) -> bytes:
    """BASE_CONN_HEART_BEAT コマンドを構築する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(DevCfgCommandId.BASE_CONN_HEART_BEAT))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(13, ProtobufWriter().to_bytes())
    return writer.to_bytes()


def ring_connect_info(
    magic_random: int,
    connect: bool,
    ring_mac: bytes,
    ring_name: str = "",
) -> bytes:
    """RING_CONNECT_INFO コマンドを構築する。"""

    if len(ring_mac) != 6:
        raise ValueError("ring_mac must be a 6-byte MAC address")

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(DevCfgCommandId.RING_CONNECT_INFO))
    writer.write_int32_field(2, magic_random)

    ring_writer = ProtobufWriter()
    ring_writer.write_bool_field(1, connect)
    ring_writer.write_bytes_field(2, ring_mac)
    if ring_name:
        ring_writer.write_bytes_field(3, ring_name.encode("utf-8"))

    writer.write_message_field(5, ring_writer.to_bytes())
    return writer.to_bytes()


__all__ = [
    "auth_cmd",
    "base_heartbeat",
    "pipe_role_change",
    "ring_connect_info",
    "time_sync",
]
