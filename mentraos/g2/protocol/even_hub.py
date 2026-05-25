"""EvenHub サービス向け protobuf builder。"""

from dataclasses import dataclass
from typing import Optional, Sequence

from ..constants import (
    CONTAINER_NAME_MAX_CHARS,
    EvenHubCmd,
    IMAGE_MAX_HEIGHT,
    IMAGE_MAX_WIDTH,
    IMAGE_MIN_HEIGHT,
    IMAGE_MIN_WIDTH,
    INITIAL_TEXT_MAX_BYTES,
    MAX_IMAGE_CONTAINERS,
    MAX_PAGE_CONTAINERS,
    MAX_TEXT_LIST_CONTAINERS,
)
from ..protobuf import ProtobufWriter


@dataclass(frozen=True)
class TextContainerSpec:
    """テキストコンテナ 1 個分の論理定義。"""

    x: int
    y: int
    width: int
    height: int
    container_id: int
    border_width: int = 0
    border_color: int = 0
    border_radius: int = 0
    padding_length: int = 0
    container_name: Optional[str] = None
    is_event_capture: bool = False
    content: Optional[str] = None

    def initial_text_size_bytes(self) -> int:
        """create/rebuild 時に載る初期テキストサイズを UTF-8 byte 数で返す。"""

        if self.content is None:
            return 0
        return len(self.content.encode("utf-8"))


@dataclass(frozen=True)
class ImageContainerSpec:
    """画像コンテナ 1 個分の論理定義。"""

    x: int
    y: int
    width: int
    height: int
    container_id: int
    container_name: Optional[str] = None


def _validate_container_name(container_name: Optional[str]) -> None:
    """containerName の長さ制約を検証する。"""

    if container_name is not None and len(container_name) > CONTAINER_NAME_MAX_CHARS:
        raise ValueError("container_name must be 16 characters or fewer")


def _validate_text_content(text: Optional[str], allow_empty: bool = False) -> None:
    """G2 へ載せるテキストが空文字でないことを確認する。"""

    if text is None:
        return
    if not allow_empty and text == "":
        raise ValueError("empty string cannot be sent per G2 spec; normalize to a single space")


def _validate_image_bounds(spec: ImageContainerSpec) -> None:
    """画像コンテナ寸法が公式制約内に収まっているかを確認する。"""

    if not (IMAGE_MIN_WIDTH <= spec.width <= IMAGE_MAX_WIDTH):
        raise ValueError("image width must be between 20 and 288")
    if not (IMAGE_MIN_HEIGHT <= spec.height <= IMAGE_MAX_HEIGHT):
        raise ValueError("image height must be between 20 and 144")


def _validate_page_constraints(
    text_containers: Sequence[TextContainerSpec],
    image_containers: Sequence[ImageContainerSpec],
) -> None:
    """ページ全体の制約をまとめて検証する。"""

    total_containers = len(text_containers) + len(image_containers)
    if total_containers > MAX_PAGE_CONTAINERS:
        raise ValueError("total container count per page must be 12 or fewer")
    if len(text_containers) > MAX_TEXT_LIST_CONTAINERS:
        raise ValueError("text/list container count must be 8 or fewer")
    if len(image_containers) > MAX_IMAGE_CONTAINERS:
        raise ValueError("image container count must be 4 or fewer")

    seen_ids = set()
    seen_names = set()
    capture_count = 0

    for spec in text_containers:
        if spec.container_id in seen_ids:
            raise ValueError("container_id must be unique within the page")
        seen_ids.add(spec.container_id)
        _validate_container_name(spec.container_name)
        _validate_text_content(spec.content)
        if spec.container_name is not None:
            if spec.container_name in seen_names:
                raise ValueError("container_name must be unique within the page")
            seen_names.add(spec.container_name)
        if spec.initial_text_size_bytes() > INITIAL_TEXT_MAX_BYTES:
            raise ValueError("initial text for create/rebuild must be 1000 bytes or fewer in UTF-8")
        if spec.is_event_capture:
            capture_count += 1

    for spec in image_containers:
        if spec.container_id in seen_ids:
            raise ValueError("container_id must be unique within the page")
        seen_ids.add(spec.container_id)
        _validate_container_name(spec.container_name)
        _validate_image_bounds(spec)
        if spec.container_name is not None:
            if spec.container_name in seen_names:
                raise ValueError("container_name must be unique within the page")
            seen_names.add(spec.container_name)

    if capture_count != 1:
        raise ValueError("exactly one text container with event capture must exist in the page")


def text_container_property(spec: TextContainerSpec) -> bytes:
    """TextContainerProperty を直列化する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, spec.x)
    writer.write_int32_field(2, spec.y)
    writer.write_int32_field(3, spec.width)
    writer.write_int32_field(4, spec.height)
    writer.write_int32_field(5, spec.border_width)
    writer.write_int32_field(6, spec.border_color)
    writer.write_int32_field(7, spec.border_radius)
    writer.write_int32_field(8, spec.padding_length)
    writer.write_int32_field(9, spec.container_id)
    if spec.container_name is not None:
        writer.write_string_field(10, spec.container_name)
    writer.write_int32_field(11, 1 if spec.is_event_capture else 0)
    if spec.content is not None:
        writer.write_string_field(12, spec.content)
    return writer.to_bytes()


def image_container_property(spec: ImageContainerSpec) -> bytes:
    """ImageContainerProperty を直列化する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, spec.x)
    writer.write_int32_field(2, spec.y)
    writer.write_int32_field(3, spec.width)
    writer.write_int32_field(4, spec.height)
    writer.write_int32_field(5, spec.container_id)
    if spec.container_name is not None:
        writer.write_string_field(6, spec.container_name)
    return writer.to_bytes()


def image_raw_data_update(
    container_id: int,
    map_session_id: int,
    map_total_size: int,
    map_fragment_index: int,
    map_fragment_packet_size: int,
    map_raw_data: bytes,
    container_name: Optional[str] = None,
    compress_mode: int = 0,
) -> bytes:
    """ImageRawDataUpdate を直列化する。"""

    _validate_container_name(container_name)
    writer = ProtobufWriter()
    writer.write_int32_field(1, container_id)
    if container_name is not None:
        writer.write_string_field(2, container_name)
    writer.write_int32_field(3, map_session_id)
    writer.write_int32_field(4, map_total_size)
    writer.write_int32_field(5, compress_mode)
    writer.write_int32_field(6, map_fragment_index)
    writer.write_int32_field(7, map_fragment_packet_size)
    writer.write_bytes_field(8, map_raw_data)
    return writer.to_bytes()


def create_startup_page_container(
    text_containers: Sequence[TextContainerSpec],
    image_containers: Sequence[ImageContainerSpec],
) -> bytes:
    """CreateStartupPageContainer の中身を構築する。"""

    _validate_page_constraints(text_containers, image_containers)
    writer = ProtobufWriter()
    writer.write_int32_field(1, len(text_containers) + len(image_containers))
    for spec in image_containers:
        writer.write_message_field(4, image_container_property(spec))
    # 混在レイアウトでは、画像を先に宣言し、その後にテキストを宣言する。
    # 実機では後から反映されるコンテナが前面に来る挙動があるため、
    # text を最後に載せて、画像 raw data 更新後も文字が上に残るようにする。
    for spec in text_containers:
        writer.write_message_field(3, text_container_property(spec))
    return writer.to_bytes()


def text_container_upgrade(
    container_id: int,
    content_length: int,
    content: str,
    content_offset: int = 0,
) -> bytes:
    """TextContainerUpgrade を直列化する。"""

    _validate_text_content(content)
    if len(content.encode("utf-8")) > INITIAL_TEXT_MAX_BYTES:
        raise ValueError("text upgrade content must be 1000 bytes or fewer in UTF-8")
    if content_length < 0:
        raise ValueError("content_length must be 0 or greater")

    writer = ProtobufWriter()
    writer.write_int32_field(1, container_id)
    writer.write_int32_field(3, content_offset)
    writer.write_int32_field(4, content_length)
    writer.write_string_field(5, content)
    return writer.to_bytes()


def shutdown_container(exit_mode: int = 0) -> bytes:
    """ShutdownContainer を直列化する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, exit_mode)
    return writer.to_bytes()


def heartbeat_packet(count: int = 0) -> bytes:
    """Heartbeat の sub-message を構築する。"""

    writer = ProtobufWriter()
    if count != 0:
        writer.write_int32_field(1, count)
    return writer.to_bytes()


def audio_ctr_cmd(enable: bool) -> bytes:
    """AudioCtrCmd を構築する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, 1 if enable else 0)
    return writer.to_bytes()


def even_hub_message(
    cmd: EvenHubCmd,
    sub_field_number: int,
    sub_message: bytes,
    magic_random: int = 0,
    app_id: Optional[int] = None,
) -> bytes:
    """EvenHubDataMsg を構築する。"""

    writer = ProtobufWriter()
    writer.write_int32_field(1, int(cmd))
    writer.write_int32_field(2, magic_random)
    writer.write_message_field(sub_field_number, sub_message)
    if app_id is not None:
        writer.write_int32_field(5, app_id)
    return writer.to_bytes()


def create_page_message(
    text_containers: Sequence[TextContainerSpec],
    image_containers: Sequence[ImageContainerSpec],
    magic_random: int = 0,
) -> bytes:
    """startup page を生成する EvenHub message を構築する。"""

    create_message = create_startup_page_container(text_containers, image_containers)
    return even_hub_message(EvenHubCmd.CREATE_STARTUP_PAGE, 3, create_message, magic_random)


def rebuild_page_message(
    text_containers: Sequence[TextContainerSpec],
    image_containers: Sequence[ImageContainerSpec],
    magic_random: int = 0,
    app_id: Optional[int] = None,
) -> bytes:
    """page rebuild 用の EvenHub message を構築する。"""

    rebuild_message = create_startup_page_container(text_containers, image_containers)
    return even_hub_message(
        EvenHubCmd.REBUILD_PAGE,
        7,
        rebuild_message,
        magic_random=magic_random,
        app_id=app_id,
    )


def update_image_raw_data_message(
    container_id: int,
    map_session_id: int,
    map_total_size: int,
    map_fragment_index: int,
    map_fragment_packet_size: int,
    map_raw_data: bytes,
    container_name: Optional[str] = None,
    compress_mode: int = 0,
) -> bytes:
    """画像 raw data 更新 message を構築する。"""

    sub_message = image_raw_data_update(
        container_id=container_id,
        container_name=container_name,
        map_session_id=map_session_id,
        map_total_size=map_total_size,
        compress_mode=compress_mode,
        map_fragment_index=map_fragment_index,
        map_fragment_packet_size=map_fragment_packet_size,
        map_raw_data=map_raw_data,
    )
    return even_hub_message(EvenHubCmd.UPDATE_IMAGE_RAW_DATA, 5, sub_message)


def update_text_message(
    container_id: int,
    content_length: int,
    content: str,
    content_offset: int = 0,
) -> bytes:
    """テキスト in-place 更新 message を構築する。"""

    sub_message = text_container_upgrade(container_id, content_length, content, content_offset)
    return even_hub_message(EvenHubCmd.UPDATE_TEXT_DATA, 9, sub_message)


def shutdown_message(exit_mode: int = 0) -> bytes:
    """ページ終了 message を構築する。"""

    return even_hub_message(EvenHubCmd.SHUTDOWN_PAGE, 11, shutdown_container(exit_mode))


def heartbeat_message(magic_random: int = 0) -> bytes:
    """EvenHub heartbeat message を構築する。"""

    return even_hub_message(EvenHubCmd.HEARTBEAT, 14, heartbeat_packet(), magic_random)


def audio_control_message(enable: bool, magic_random: int = 0) -> bytes:
    """audioControl message を構築する。"""

    return even_hub_message(EvenHubCmd.AUDIO_CONTROL, 18, audio_ctr_cmd(enable), magic_random)


__all__ = [
    "ImageContainerSpec",
    "TextContainerSpec",
    "audio_control_message",
    "audio_ctr_cmd",
    "create_page_message",
    "create_startup_page_container",
    "even_hub_message",
    "heartbeat_message",
    "heartbeat_packet",
    "image_container_property",
    "image_raw_data_update",
    "rebuild_page_message",
    "shutdown_container",
    "shutdown_message",
    "text_container_property",
    "text_container_upgrade",
    "update_image_raw_data_message",
    "update_text_message",
]
