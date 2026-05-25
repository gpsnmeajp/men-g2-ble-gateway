"""Dashboard サービス向け最小 builder。"""

from ..protobuf import ProtobufWriter


def display_settings_message(magic_random: int) -> bytes:
    """G2.kt の接続時 dashboard display settings を構築する。"""

    display_writer = ProtobufWriter()
    display_writer.write_int32_field(1, 4)
    display_writer.write_int32_field(2, 3)
    display_writer.write_bytes_field(3, bytes([1, 2, 3]))
    display_writer.write_int32_field(4, 4)
    display_writer.write_bytes_field(5, bytes([1, 3, 2, 2]))
    display_writer.write_int32_field(6, 1)
    display_writer.write_int32_field(7, 1)

    receive_writer = ProtobufWriter()
    receive_writer.write_message_field(2, display_writer.to_bytes())

    writer = ProtobufWriter()
    writer.write_int32_field(1, 2)
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(4, receive_writer.to_bytes())
    return writer.to_bytes()


__all__ = ["display_settings_message"]