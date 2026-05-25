"""G2.kt 由来の最小 protobuf 実装。"""

from typing import Dict, Optional, Tuple, Union


ParsedFieldValue = Union[int, bytes]


def to_signed_32(value: int) -> int:
    """Kotlin の Long.toInt() と同じ 32bit 符号付き変換を行う。"""

    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        return value - 0x100000000
    return value


class ProtobufWriter:
    """必要最小限の protobuf field を手書きで直列化する。"""

    def __init__(self) -> None:
        # Kotlin の ByteArrayOutputStream 相当として bytearray を使う。
        self._buffer = bytearray()

    def write_varint(self, value: int) -> None:
        """protobuf varint を書き込む。

        負数は Kotlin 実装と同じく 64bit の 2 の補数として扱い、10 byte の
        varint になるよう unsigned 右シフト相当で進める。
        """

        unsigned_value = value & 0xFFFFFFFFFFFFFFFF if value < 0 else value
        while unsigned_value > 0x7F:
            self._buffer.append((unsigned_value & 0x7F) | 0x80)
            unsigned_value >>= 7
        self._buffer.append(unsigned_value & 0x7F)

    def write_int32_field(self, field_number: int, value: int) -> None:
        """wire type 0 の int32 field を書き込む。"""

        self.write_varint(field_number << 3)
        self.write_varint(value)

    def write_string_field(self, field_number: int, value: str) -> None:
        """UTF-8 文字列 field を書き込む。"""

        encoded = value.encode("utf-8")
        self.write_varint((field_number << 3) | 2)
        self.write_varint(len(encoded))
        self._buffer.extend(encoded)

    def write_bytes_field(self, field_number: int, value: bytes) -> None:
        """bytes field を書き込む。"""

        self.write_varint((field_number << 3) | 2)
        self.write_varint(len(value))
        self._buffer.extend(value)

    def write_message_field(self, field_number: int, sub_message: bytes) -> None:
        """length-delimited の sub-message field を書き込む。"""

        self.write_bytes_field(field_number, sub_message)

    def write_bool_field(self, field_number: int, value: bool) -> None:
        """bool field を int32 と同じ扱いで書き込む。"""

        self.write_int32_field(field_number, 1 if value else 0)

    def to_bytes(self) -> bytes:
        """書き込んだ内容を bytes として返す。"""

        return bytes(self._buffer)


class ProtobufReader:
    """応答解析に必要な最小限の protobuf reader。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    @property
    def has_more(self) -> bool:
        """まだ未読データが残っているかを返す。"""

        return self._offset < len(self._data)

    def read_varint(self) -> Optional[int]:
        """varint を 1 つ読む。壊れた入力では None を返す。"""

        result = 0
        shift = 0
        while self._offset < len(self._data):
            byte = self._data[self._offset]
            self._offset += 1
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return result
            shift += 7
            if shift > 63:
                return None
        return None

    def read_tag(self) -> Optional[Tuple[int, int]]:
        """field number と wire type を返す。"""

        tag = self.read_varint()
        if tag is None:
            return None
        return tag >> 3, tag & 0x07

    def read_int32(self) -> Optional[int]:
        """varint を int32 として読む。"""

        value = self.read_varint()
        if value is None:
            return None
        return to_signed_32(value)

    def read_bytes(self) -> Optional[bytes]:
        """length-delimited field を bytes として読む。"""

        length = self.read_varint()
        if length is None:
            return None
        if self._offset + length > len(self._data):
            return None
        result = self._data[self._offset : self._offset + length]
        self._offset += length
        return result

    def read_string(self) -> Optional[str]:
        """UTF-8 bytes を文字列として読む。"""

        raw = self.read_bytes()
        if raw is None:
            return None
        return raw.decode("utf-8")

    def skip_field(self, wire_type: int) -> None:
        """未知 field を読み飛ばす。"""

        if wire_type == 0:
            self.read_varint()
        elif wire_type == 1:
            self._offset = min(self._offset + 8, len(self._data))
        elif wire_type == 2:
            self.read_bytes()
        elif wire_type == 5:
            self._offset = min(self._offset + 4, len(self._data))

    def parse_fields(self) -> Dict[int, ParsedFieldValue]:
        """field number をキーにした単純辞書へ展開する。

        repeated field は最後に見つかった値で上書きする。初期実装では、G2 の
        応答で必要な最低限の読み取りに絞る。
        """

        fields = {}
        while self.has_more:
            tag = self.read_tag()
            if tag is None:
                break
            field_number, wire_type = tag
            if wire_type == 0:
                value = self.read_int32()
                if value is not None:
                    fields[field_number] = value
            elif wire_type == 2:
                value = self.read_bytes()
                if value is not None:
                    fields[field_number] = value
            else:
                self.skip_field(wire_type)
        return fields


__all__ = [
    "ParsedFieldValue",
    "ProtobufReader",
    "ProtobufWriter",
    "to_signed_32",
]
