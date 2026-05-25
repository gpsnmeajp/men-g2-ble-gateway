"""Calendar サービス向け最小 builder。"""

from ..protobuf import ProtobufWriter


def default_config_message(magic_random: int) -> bytes:
    """G2.kt が接続時に送る calendar config をそのまま構築する。"""

    config_writer = ProtobufWriter()
    config_writer.write_int32_field(1, 1)
    config_writer.write_int32_field(2, 1)
    config_writer.write_int32_field(3, 5)
    config_writer.write_int32_field(5, 1)

    writer = ProtobufWriter()
    writer.write_int32_field(1, 1)
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, config_writer.to_bytes())
    return writer.to_bytes()


__all__ = ["default_config_message"]