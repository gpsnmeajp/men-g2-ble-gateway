"""Even G2 BLE transport の packet framing 実装。"""

from typing import Dict, List, Optional, Tuple

from .constants import DEST_GLASSES, HEADER_BYTE, MAX_PACKET_PAYLOAD, SOURCE_PHONE
from .crc import calc_crc16


class EvenBLETransport:
    """G2 payload を BLE packet 群へ分割する。"""

    @staticmethod
    def build_packets(
        sync_id: int,
        service_id: int,
        payload: bytes,
        reserve_flag: bool = False,
    ) -> List[bytes]:
        """G2.kt の buildPackets と同じ規則で packet 群を組み立てる。"""

        chunks = []
        offset = 0
        while offset < len(payload):
            end = min(offset + MAX_PACKET_PAYLOAD, len(payload))
            chunks.append(payload[offset:end])
            offset = end

        # 空 payload でも CRC 付き 1 packet は必要になる。
        if not chunks:
            chunks.append(b"")

        # 最終 chunk がちょうど上限サイズなら、CRC 専用の空 packet を追加する。
        if len(chunks[-1]) == MAX_PACKET_PAYLOAD:
            chunks.append(b"")

        total_packets = len(chunks)
        crc = calc_crc16(payload)
        packets = []

        for index, chunk in enumerate(chunks, start=1):
            is_last = index == total_packets
            status = 0x20 if reserve_flag else 0x00
            payload_length = len(chunk) + (2 if is_last else 0)

            packet = bytearray()
            packet.append(HEADER_BYTE)
            packet.append(((DEST_GLASSES << 4) | SOURCE_PHONE) & 0xFF)
            packet.append(sync_id & 0xFF)
            packet.append(payload_length & 0xFF)
            packet.append(total_packets & 0xFF)
            packet.append(index & 0xFF)
            packet.append(service_id & 0xFF)
            packet.append(status & 0xFF)
            packet.extend(chunk)

            if is_last:
                packet.append(crc & 0xFF)
                packet.append((crc >> 8) & 0xFF)

            packets.append(bytes(packet))

        return packets


class G2SendManager:
    """syncId と magicRandom の単純カウンタを管理する。"""

    def __init__(self) -> None:
        self._sync_id = 0
        self._magic_random = 0

    def next_sync_id(self) -> int:
        """送信 packet 用の syncId を 1 byte 周回で採番する。"""

        current = self._sync_id
        self._sync_id = (self._sync_id + 1) & 0xFF
        return current

    def next_magic_random(self) -> int:
        """protobuf 内の MagicRandom を 1 byte 周回で採番する。"""

        current = self._magic_random
        self._magic_random = (self._magic_random + 1) & 0xFF
        return current

    def build_packets(
        self,
        service_id: int,
        payload: bytes,
        reserve_flag: bool = False,
    ) -> List[bytes]:
        """新しい syncId を払い出して packet 群を構築する。"""

        return EvenBLETransport.build_packets(
            sync_id=self.next_sync_id(),
            service_id=service_id,
            payload=payload,
            reserve_flag=reserve_flag,
        )


class G2ReceiveManager:
    """複数 packet に分割された受信 payload を再構成する。"""

    def __init__(self) -> None:
        # Kotlin 実装と同様に、sourceKey・serviceId・syncId を束ねて partial を持つ。
        self._partials: Dict[str, bytearray] = {}

    def reset(self) -> None:
        """再接続やエラー時に partial buffer を破棄する。"""

        self._partials.clear()

    def handle_packet(
        self,
        raw_data: bytes,
        source_key: str = "",
    ) -> Optional[Tuple[int, bytes]]:
        """1 packet を受け取り、最後の packet なら再構成 payload を返す。

        初期実装では Kotlin と合わせて CRC 検証は行わず、末尾 2 byte を除去した
        payload の再構成だけを担当する。
        """

        if len(raw_data) < 8:
            return None
        if raw_data[0] != HEADER_BYTE:
            return None

        payload_length = raw_data[3] & 0xFF
        expected_length = payload_length + 8
        if len(raw_data) < expected_length:
            return None

        total_packets = raw_data[4] & 0xFF
        serial_num = raw_data[5] & 0xFF
        service_id = raw_data[6] & 0xFF
        status = raw_data[7] & 0xFF
        result_code = (status >> 1) & 0x0F
        if result_code != 0:
            return None

        is_last = serial_num == total_packets
        payload_end = 8 + payload_length - (2 if is_last else 0)
        payload = raw_data[8:payload_end]

        sync_id = raw_data[2] & 0xFF
        key = f"{source_key}-{service_id}-{sync_id}"

        if serial_num > 1:
            existing = self._partials.get(key)
            if existing is None:
                return None
            existing.extend(payload)
        elif total_packets > 1:
            self._partials[key] = bytearray(payload)

        if not is_last:
            return None

        existing = self._partials.pop(key, None)
        if existing is not None:
            return service_id, bytes(existing)
        return service_id, bytes(payload)


__all__ = ["EvenBLETransport", "G2ReceiveManager", "G2SendManager"]
