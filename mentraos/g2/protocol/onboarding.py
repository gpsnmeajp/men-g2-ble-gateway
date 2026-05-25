"""onboarding サービス向け protobuf builder。"""

from ..protobuf import ProtobufWriter


def skip_onboarding(magic_random: int) -> bytes:
    """onboarding 完了扱いの CONFIG message を構築する。"""

    config_writer = ProtobufWriter()
    config_writer.write_int32_field(1, 4)

    writer = ProtobufWriter()
    writer.write_int32_field(1, 1)
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(3, config_writer.to_bytes())
    return writer.to_bytes()


__all__ = ["skip_onboarding"]
