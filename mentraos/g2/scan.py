"""G2 の BLE スキャン補助。"""

from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from bleak import BleakScanner
except ImportError:  # pragma: no cover - 未導入環境でも import は通す。
    BleakScanner = None


@dataclass(frozen=True)
class G2DiscoveredDevice:
    """スキャン結果から使う最小限のデバイス情報。"""

    address: str
    name: str
    side: str
    serial_number: str
    mac_address: Optional[str]
    rssi: Optional[int]
    bleak_device: Any
    advertisement: Any


@dataclass(frozen=True)
class G2DiscoveredPair:
    """左右 1 組として採用された G2 ペア。"""

    serial_number: str
    left: G2DiscoveredDevice
    right: G2DiscoveredDevice


def extract_serial_from_manufacturer_bytes(data: bytes) -> Optional[str]:
    """manufacturer data 先頭 14 byte から ASCII シリアルを抜き出す。"""

    if len(data) < 14:
        return None
    raw = data[:14]
    serial_number = raw.decode("ascii", errors="ignore")
    serial_number = re.sub(r"[\x00-\x1F\x7F]", "", serial_number)
    return serial_number or None


def extract_mac_from_manufacturer_bytes(data: bytes) -> Optional[str]:
    """manufacturer data の SN(14) 後ろ 6 byte を BLE MAC として読む。"""

    if len(data) < 20:
        return None
    mac_le = data[14:20]
    return ":".join(f"{byte:02X}" for byte in reversed(mac_le))


def infer_side_from_name(name: str) -> Optional[str]:
    """広告名の _L_ / _R_ から左右を推定する。"""

    upper_name = name.upper()
    if "_L_" in upper_name:
        return "left"
    if "_R_" in upper_name:
        return "right"
    return None


def _iter_scan_results(raw_results: Any) -> Iterable[Tuple[Any, Any]]:
    """Bleak の discover 返り値差分を吸収して (device, advertisement) へ揃える。"""

    if isinstance(raw_results, dict):
        for value in raw_results.values():
            if isinstance(value, tuple) and len(value) == 2:
                yield value[0], value[1]
            else:
                yield value, None
        return

    for device in raw_results or []:
        yield device, None


def _get_manufacturer_bytes(device: Any, advertisement: Any) -> Optional[bytes]:
    """Bleak の advertisement/device metadata から manufacturer data を拾う。"""

    sources = []
    if advertisement is not None:
        sources.append(getattr(advertisement, "manufacturer_data", None))
    sources.append(getattr(device, "metadata", {}).get("manufacturer_data"))

    for source in sources:
        if isinstance(source, dict) and source:
            first_value = next(iter(source.values()))
            if isinstance(first_value, (bytes, bytearray)):
                return bytes(first_value)
    return None


async def discover_g2_devices(timeout: float, search_id: str = "") -> List[G2DiscoveredDevice]:
    """タイムアウトまで G2 を走査し、利用しやすい形へ整形して返す。"""

    if BleakScanner is None:
        raise RuntimeError("bleak is required for BLE scan")

    try:
        raw_results = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:
        raw_results = await BleakScanner.discover(timeout=timeout)

    discovered: List[G2DiscoveredDevice] = []
    for device, advertisement in _iter_scan_results(raw_results):
        name = getattr(device, "name", None) or getattr(advertisement, "local_name", None) or ""
        if "G2" not in name.upper():
            continue
        side = infer_side_from_name(name)
        if side is None:
            continue
        manufacturer_bytes = _get_manufacturer_bytes(device, advertisement)
        if manufacturer_bytes is None:
            continue
        serial_number = extract_serial_from_manufacturer_bytes(manufacturer_bytes)
        if not serial_number:
            continue
        if search_id and search_id not in serial_number:
            continue
        discovered.append(
            G2DiscoveredDevice(
                address=str(getattr(device, "address", "")),
                name=name,
                side=side,
                serial_number=serial_number,
                mac_address=extract_mac_from_manufacturer_bytes(manufacturer_bytes),
                rssi=getattr(advertisement, "rssi", None),
                bleak_device=device,
                advertisement=advertisement,
            )
        )
    return discovered


async def discover_g2_pair(timeout: float, search_id: str = "") -> Optional[G2DiscoveredPair]:
    """左右 1 組の G2 を選び出す。"""

    devices = await discover_g2_devices(timeout=timeout, search_id=search_id)
    grouped: Dict[str, Dict[str, G2DiscoveredDevice]] = {}
    for device in devices:
        grouped.setdefault(device.serial_number, {})[device.side] = device

    serial_candidates = sorted(grouped)
    for serial_number in serial_candidates:
        pair = grouped[serial_number]
        left = pair.get("left")
        right = pair.get("right")
        if left is not None and right is not None:
            return G2DiscoveredPair(serial_number=serial_number, left=left, right=right)
    return None


__all__ = [
    "G2DiscoveredDevice",
    "G2DiscoveredPair",
    "discover_g2_devices",
    "discover_g2_pair",
    "extract_mac_from_manufacturer_bytes",
    "extract_serial_from_manufacturer_bytes",
    "infer_side_from_name",
]
