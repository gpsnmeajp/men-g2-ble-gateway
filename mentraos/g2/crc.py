"""G2.kt と同じ CRC16 実装。"""


def calc_crc16(data: bytes) -> int:
    """Kotlin 実装に合わせて CRC16 を計算する。

    ここでは一般的なライブラリ実装へ寄せず、G2.kt のビット操作順序を
    そのまま Python へ移した。これによりパケット末尾の CRC が一致しやすい。
    """

    crc = 0xFFFF
    for byte in data:
        crc = ((crc >> 8) | ((crc << 8) & 0xFF00)) ^ (byte & 0xFF)
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return crc & 0xFFFF


__all__ = ["calc_crc16"]
