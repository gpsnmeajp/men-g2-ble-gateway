"""even_ai サービス向け protobuf builder。"""

from ..protobuf import ProtobufWriter


def set_hey_even(magic_random: int, enabled: bool) -> bytes:
    """Hey Even の有効/無効を切り替える CONFIG message を構築する。"""

    config_writer = ProtobufWriter()
    config_writer.write_int32_field(1, 1 if enabled else 0)
    config_writer.write_int32_field(2, 80)

    writer = ProtobufWriter()
    writer.write_int32_field(1, 10)
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(13, config_writer.to_bytes())
    return writer.to_bytes()


__all__ = ["set_hey_even"]
