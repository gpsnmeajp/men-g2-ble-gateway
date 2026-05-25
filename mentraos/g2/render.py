"""G2 画像表示のためのデコード、リサイズ、BMP 変換。"""

from dataclasses import dataclass
import base64
from io import BytesIO
from math import ceil
from typing import List, Sequence, Tuple

from .constants import IMAGE_MAX_HEIGHT, IMAGE_MAX_WIDTH, IMAGE_MIN_HEIGHT, IMAGE_MIN_WIDTH

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - 未導入でも import 自体は通す。
    Image = None
    ImageOps = None


@dataclass(frozen=True)
class RenderedImageTile:
    """内部タイル分割後の 1 コンテナ相当データ。"""

    x: int
    y: int
    width: int
    height: int
    container_id: int
    container_name: str
    bmp_data: bytes


def _require_pillow() -> None:
    """Pillow が未導入なら明示的な例外へ変換する。"""

    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is required for image rendering")


def decode_base64_image(image_base64: str) -> bytes:
    """plain base64 または data URL を bytes へ戻す。"""

    payload = image_base64.strip()
    if payload.startswith("data:"):
        comma = payload.find(",")
        if comma < 0:
            raise ValueError("malformed data URL")
        payload = payload[comma + 1 :]
    return base64.b64decode(payload)


def split_dimension(total_size: int, max_size: int, min_size: int) -> List[int]:
    """合計サイズを min/max 制約内の区間へ分割する。"""

    if total_size <= 0:
        raise ValueError("size must be 1 or greater")
    if total_size <= max_size:
        if total_size < min_size:
            raise ValueError("image container dimensions are below the minimum")
        return [total_size]

    segments = ceil(total_size / max_size)
    base_size = total_size // segments
    remainder = total_size % segments
    sizes = [base_size + (1 if index < remainder else 0) for index in range(segments)]
    if any(size < min_size or size > max_size for size in sizes):
        raise ValueError("cannot split image into valid container dimensions")
    return sizes


def render_image_to_grayscale_canvas(
    image_bytes: bytes,
    width: int,
    height: int,
    gamma: float = 1.0,
    dither: bool = False,
) -> bytes:
    """指定サイズの黒背景キャンバスへ画像を収め、8bit 灰度で返す。

    gamma: 1.0 = 無補正。1.0 未満で明るく、1.0 超で暗くなる。
    dither: True のとき Floyd-Steinberg ディザリングを適用する。
    """

    _require_pillow()
    with Image.open(BytesIO(image_bytes)) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGBA")
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        contained = ImageOps.contain(normalized, (width, height), Image.Resampling.LANCZOS)
        offset_x = (width - contained.width) // 2
        offset_y = (height - contained.height) // 2
        canvas.paste(contained, (offset_x, offset_y), contained)
        grayscale = canvas.convert("L")

    if gamma != 1.0:
        gamma_table = [min(255, round(255.0 * (i / 255.0) ** gamma)) for i in range(256)]
        grayscale = grayscale.point(gamma_table)

    pixels = grayscale.tobytes()

    if dither:
        pixels = _floyd_steinberg_dither_4bit(pixels, width, height)

    return pixels


def _floyd_steinberg_dither_4bit(pixels: bytes, width: int, height: int) -> bytes:
    """Floyd-Steinberg ディザリングを 16 段階 (4bit) へ適用する。

    入力は 8bit グレースケール (0-255)。出力は量子化後の 8bit 値 (0, 17, 34, ..., 255)。
    """

    buf = [float(p) for p in pixels]

    for row in range(height):
        for col in range(width):
            idx = row * width + col
            old = buf[idx]
            level = max(0, min(15, round(old / 17.0)))
            new_val = level * 17
            buf[idx] = float(new_val)
            error = old - new_val

            if col + 1 < width:
                buf[idx + 1] += error * (7.0 / 16.0)
            if row + 1 < height:
                if col - 1 >= 0:
                    buf[idx + width - 1] += error * (3.0 / 16.0)
                buf[idx + width] += error * (5.0 / 16.0)
                if col + 1 < width:
                    buf[idx + width + 1] += error * (1.0 / 16.0)

    return bytes(max(0, min(255, round(v))) for v in buf)


def build_4bit_bmp(grayscale_pixels: bytes, width: int, height: int) -> bytes:
    """G2 が受け取れる 4bit BMP を手組みする。"""

    bytes_per_row_4bit = (width + 1) // 2
    padded_row_size = (bytes_per_row_4bit + 3) & ~0x03
    pixel_data_size = padded_row_size * height
    header_size = 14 + 40 + 64
    file_size = header_size + pixel_data_size

    output = bytearray()
    output.extend(b"BM")
    output.extend(file_size.to_bytes(4, "little"))
    output.extend((0).to_bytes(2, "little"))
    output.extend((0).to_bytes(2, "little"))
    output.extend(header_size.to_bytes(4, "little"))

    output.extend((40).to_bytes(4, "little"))
    output.extend(width.to_bytes(4, "little", signed=True))
    output.extend(height.to_bytes(4, "little", signed=True))
    output.extend((1).to_bytes(2, "little"))
    output.extend((4).to_bytes(2, "little"))
    output.extend((0).to_bytes(4, "little"))
    output.extend(pixel_data_size.to_bytes(4, "little"))
    output.extend((2835).to_bytes(4, "little"))
    output.extend((2835).to_bytes(4, "little"))
    output.extend((16).to_bytes(4, "little"))
    output.extend((0).to_bytes(4, "little"))

    for palette_index in range(16):
        value = palette_index * 17
        output.extend((value, value, value, 0))

    for row in range(height):
        src_row = height - 1 - row
        src_offset = src_row * width
        row_buffer = bytearray(padded_row_size)
        for column in range(width):
            gray8 = grayscale_pixels[src_offset + column]
            index4 = gray8 >> 4
            byte_pos = column // 2
            if column % 2 == 0:
                row_buffer[byte_pos] = index4 << 4
            else:
                row_buffer[byte_pos] |= index4
        output.extend(row_buffer)
    return bytes(output)


def render_image_tiles(
    image_bytes: bytes,
    x: int,
    y: int,
    width: int,
    height: int,
    container_id_start: int,
    container_name_prefix: str = "img",
    gamma: float = 1.0,
    dither: bool = False,
) -> List[RenderedImageTile]:
    """論理画像を公式制約内の image コンテナ群へ分割する。"""

    x_splits = split_dimension(width, IMAGE_MAX_WIDTH, IMAGE_MIN_WIDTH)
    y_splits = split_dimension(height, IMAGE_MAX_HEIGHT, IMAGE_MIN_HEIGHT)
    grayscale_canvas = render_image_to_grayscale_canvas(image_bytes, width, height, gamma=gamma, dither=dither)

    rendered_tiles: List[RenderedImageTile] = []
    tile_id = container_id_start
    current_y = y
    source_y = 0
    for tile_height in y_splits:
        current_x = x
        source_x = 0
        for tile_width in x_splits:
            tile_pixels = bytearray(tile_width * tile_height)
            for row in range(tile_height):
                source_offset = (source_y + row) * width + source_x
                target_offset = row * tile_width
                tile_pixels[target_offset : target_offset + tile_width] = grayscale_canvas[
                    source_offset : source_offset + tile_width
                ]
            rendered_tiles.append(
                RenderedImageTile(
                    x=current_x,
                    y=current_y,
                    width=tile_width,
                    height=tile_height,
                    container_id=tile_id,
                    container_name=f"{container_name_prefix}-{tile_id}",
                    bmp_data=build_4bit_bmp(bytes(tile_pixels), tile_width, tile_height),
                )
            )
            tile_id += 1
            current_x += tile_width
            source_x += tile_width
        current_y += tile_height
        source_y += tile_height
    return rendered_tiles


__all__ = [
    "RenderedImageTile",
    "build_4bit_bmp",
    "decode_base64_image",
    "render_image_tiles",
    "render_image_to_grayscale_canvas",
    "split_dimension",
]
