"""AI エージェントから G2 ゲートウェイを制御するための FastMCP ツール群。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal, Optional, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context


VALID_GESTURES = {"single_tap", "double_tap", "swipe_up", "swipe_down"}  # 有効なタッチジェスチャー
CHOICE_GESTURES = ["single_tap", "double_tap", "swipe_up", "swipe_down"]  # 選択肢にマッピングされるジェスチャー順
CHOICE_LABELS = {  # ユーザー表示用のジェスチャーラベル
    "single_tap": "single tap",
    "double_tap": "double tap",
    "swipe_up": "swipe up",
    "swipe_down": "swipe down",
}
MENU_GESTURES = {"single_tap", "double_tap", "swipe_up", "swipe_down"}  # メニュー操作用ジェスチャー
DIALOGUE_ICON_SIZE = 100  # ゲーム風対話UIのアイコンサイズ（px）


READY_WAIT_TIMEOUT_SEC = 15.0
READY_WAIT_INTERVAL_SEC = 0.5


class GatewayControl(Protocol):
    """ゲートウェイ制御インターフェース。
    
    インプロセス制御と HTTP 経由制御の両方で実装される。
    """

    async def display(self, payload: dict[str, Any]) -> dict[str, Any]:
        """display ペイロードを送信する。"""
        ...

    async def status(self) -> dict[str, Any]:
        """ゲートウェイとグラスのステータスを取得する。"""
        ...

    async def wait_for_touch(
        self,
        timeout_sec: float,
        allowed_gestures: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """タッチイベントを待機する。"""
        ...


class InProcessGatewayControl:
    """GatewayServerApp 内で実行されるツール用のアダプター。"""

    def __init__(self, gateway_app: Any) -> None:
        self._gateway_app = gateway_app

    async def display(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._gateway_app.display_payload(payload)

    async def status(self) -> dict[str, Any]:
        return self._gateway_app.status_payload()

    async def wait_for_touch(
        self,
        timeout_sec: float,
        allowed_gestures: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        return await self._gateway_app.wait_for_touch(timeout_sec, allowed_gestures)


class HttpGatewayControl:
    """既存のゲートウェイを制御するスタンドアロン MCP サーバー用のアダプター。"""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8765",
        api_key: Optional[str] = None,
        websocket_path: str = "/ws",
    ) -> None:
        self._server_url = server_url
        self._api_key = api_key
        self._websocket_path = websocket_path

    async def display(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("POST", "/api/display", payload)

    async def status(self) -> dict[str, Any]:
        return await self._request_json("GET", "/api/status")

    async def wait_for_touch(
        self,
        timeout_sec: float,
        allowed_gestures: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession(headers=self._auth_headers()) as session:
            async with session.ws_connect(self._ws_url()) as websocket:
                async with asyncio.timeout(timeout_sec):
                    async for message in websocket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        event = json.loads(message.data)
                        if event.get("kind") != "glasses.touch":
                            continue
                        gesture = str(event.get("data", {}).get("gesture", ""))
                        # 許可されたジェスチャーが指定されている場合、マッチするまでスキップ
                        if allowed_gestures is not None and gesture not in allowed_gestures:
                            continue
                        return event
        raise TimeoutError("WebSocket closed before a matching touch event was received")

    async def _request_json(
        self,
        method: Literal["GET", "POST"],
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession(headers=self._auth_headers()) as session:
            async with session.request(method, _build_http_url(self._server_url, path), json=payload) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"{method} {path} failed ({response.status}): {body}")
                return json.loads(body)

    def _auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"X-API-Key": self._api_key}

    def _ws_url(self) -> str:
        parsed = urlparse(self._server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, self._websocket_path, "", "", ""))


class ReadyWaitingGatewayControl:
    """Wait until glasses are ready before MCP operations that need a connection."""

    def __init__(self, inner: GatewayControl) -> None:
        self._inner = inner

    async def display(self, payload: dict[str, Any]) -> dict[str, Any]:
        ready, error = await self._wait_until_ready()
        if not ready:
            return {"accepted": False, "error": error}
        return await self._inner.display(payload)

    async def status(self) -> dict[str, Any]:
        return await self._inner.status()

    async def wait_for_touch(
        self,
        timeout_sec: float,
        allowed_gestures: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        ready, error = await self._wait_until_ready()
        if not ready:
            raise asyncio.TimeoutError(error)
        return await self._inner.wait_for_touch(timeout_sec, allowed_gestures)

    async def _wait_until_ready(self) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_WAIT_TIMEOUT_SEC
        last_error = ""
        while True:
            try:
                status = await self._inner.status()
                glasses = status.get("glasses", {})
                if glasses.get("ready"):
                    return True, ""
                phase = str(glasses.get("phase", "unknown"))
                last_error = f"glasses not ready yet (phase={phase})"
            except Exception as exc:
                last_error = str(exc)

            now = loop.time()
            if now >= deadline:
                break
            await asyncio.sleep(min(READY_WAIT_INTERVAL_SEC, max(0.0, deadline - now)))

        message = "glasses did not become ready within 15 seconds"
        if last_error:
            message = f"{message}: {last_error}"
        return False, message


def create_gateway_mcp(gateway_app: Any = None) -> FastMCP:
    """エージェントフレンドリーなゲートウェイツールを公開する FastMCP サーバーを作成する。
    
    Args:
        gateway_app: インプロセス制御の場合は GatewayServerApp インスタンス。
                     None の場合は HTTP 経由で制御するスタンドアロンモード。
    
    Returns:
        設定済みの FastMCP インスタンス
    """
    control: GatewayControl
    if gateway_app is None:
        # スタンドアロンモード: 環境変数から接続先を読み込む
        control = HttpGatewayControl(
            server_url=os.environ.get("G2_GATEWAY_URL", "http://127.0.0.1:8765"),
            api_key=os.environ.get("G2_GATEWAY_API_KEY") or None,
            websocket_path=os.environ.get("G2_GATEWAY_WS_PATH", "/ws"),
        )
    else:
        # インプロセスモード: ゲートウェイアプリを直接呼び出す
        control = InProcessGatewayControl(gateway_app)

    control = ReadyWaitingGatewayControl(control)

    mcp = FastMCP("G2 BLE Gateway")

    @mcp.tool(
        name="display_text",
        description="Display full-screen text on the glasses.",
        tags={"display", "text"},
    )
    async def display_text(text: str) -> dict[str, Any]:
        """text: 表示するテキスト内容"""
        return await control.display({"text": text})

    @mcp.tool(
        name="display_image",
        description="Display an image on the glasses from a data URL, base64 string, or local file path.",
        tags={"display", "image"},
    )
    async def display_image(
        image: str,
        x: int = 0,
        y: int = 0,
        width: int = 288,
        height: int = 144,
        gamma: Optional[float] = None,
        dither: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            image: data URL、base64 文字列、またはローカルファイルパス
            x, y: 表示位置
            width, height: 表示サイズ
            gamma: ガンマ補正値（オプショナル）
            dither: ディザリングを有効にするか
        """
        payload: dict[str, Any] = {
            "elements": [
                {
                    "type": "image",
                    "image_base64": _coerce_image_reference(image),
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                }
            ]
        }
        if gamma is not None:
            payload["gamma"] = gamma
        if dither:
            payload["dither"] = True
        return await control.display(payload)

    @mcp.tool(
        name="display_layout",
        description="Display an arbitrary mixed layout of text and image elements on the glasses.",
        tags={"display", "layout"},
    )
    async def display_layout(
        elements: list[dict[str, Any]],
        gamma: Optional[float] = None,
        dither: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            elements: 表示要素のリスト（type, x, y, width, height 等を含む辞書）
            gamma: ガンマ補正値（オプショナル）
            dither: ディザリングを有効にするか
        """
        payload: dict[str, Any] = {"elements": [_normalize_layout_element(element) for element in elements]}
        if gamma is not None:
            payload["gamma"] = gamma
        if dither:
            payload["dither"] = True
        return await control.display(payload)

    @mcp.tool(
        name="clear_display",
        description="Clear the visible content on the glasses.",
        tags={"display"},
    )
    async def clear_display() -> dict[str, Any]:
        return await control.display({"clear": True})

    @mcp.tool(
        name="get_status",
        description="Return gateway, connection, battery, firmware, display, and microphone status.",
        tags={"status"},
    )
    async def get_status() -> dict[str, Any]:
        return await control.status()

    @mcp.tool(
        name="wait_for_touch",
        description="Wait for the next glasses touch gesture and return the matching event.",
        tags={"interaction", "touch"},
    )
    async def wait_for_touch(
        timeout_sec: float = 60.0,
        allowed_gestures: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Args:
            timeout_sec: タイムアウト秒数
            allowed_gestures: 許可するジェスチャーのリスト（None の場合は全て許可）
        
        Returns:
            {"accepted": bool, "event": dict} または {"accepted": False, "timeout": True}
        """
        allowed = _normalize_gestures(allowed_gestures)
        try:
            event = await control.wait_for_touch(timeout_sec=timeout_sec, allowed_gestures=allowed)
            return {"accepted": True, "event": event}
        except TimeoutError:
            return {"accepted": False, "timeout": True}
        except asyncio.TimeoutError:
            return {"accepted": False, "timeout": True}

    @mcp.tool(
        name="ask_user_on_glasses",
        description="Show a question on the glasses, resolve the answer from touch gestures, then clear the display.",
        tags={"interaction", "display", "touch"},
    )
    async def ask_user_on_glasses(
        question: str,
        choices: Optional[list[str]] = None,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """
        Args:
            question: 表示する質問文
            choices: 選択肢のリスト（最大4個まで。single_tap, double_tap, swipe_up, swipe_down にマッピング）
            timeout_sec: タイムアウト秒数
        
        Returns:
            {"accepted": bool, "gesture": str, "choice": str, "event": dict}
        """
        choice_map = _build_choice_map(choices)
        prompt = _format_question(question, choice_map)
        display_result = await control.display({"text": prompt})
        if _display_failed(display_result):
            return {"accepted": False, "display": display_result}
        allowed = set(choice_map) if choice_map else None
        try:
            event = await control.wait_for_touch(timeout_sec=timeout_sec, allowed_gestures=allowed)
        except (TimeoutError, asyncio.TimeoutError):
            return await _clear_interaction_display(
                control,
                {"accepted": False, "timeout": True, "display": display_result},
            )

        gesture = str(event.get("data", {}).get("gesture", ""))
        result: dict[str, Any] = {
            "accepted": True,
            "gesture": gesture,
            "event": event,
            "display": display_result,
        }
        if choice_map:
            result["choice"] = choice_map[gesture]
        return await _clear_interaction_display(control, result)

    @mcp.tool(
        name="notify_user_on_glasses",
        description="Show a short notification on the glasses for a fixed duration, then optionally clear it.",
        tags={"interaction", "display", "notification"},
    )
    async def notify_user_on_glasses(
        message: str,
        title: Optional[str] = None,
        duration_sec: float = 3.0,
        after: Literal["clear", "keep"] = "clear",
    ) -> dict[str, Any]:
        """
        Args:
            message: 通知メッセージ
            title: タイトル（オプショナル）
            duration_sec: 表示時間（秒）
            after: 表示後の動作（"clear" でクリア、"keep" で保持）
        """
        text = f"{title}\n\n{message}" if title else message
        display_result = await control.display({"text": text})
        if _display_failed(display_result):
            return {"accepted": False, "display": display_result}
        await asyncio.sleep(max(0.0, duration_sec))
        result: dict[str, Any] = {"accepted": True, "display": display_result}
        if after == "clear":
            result["clear"] = await control.display({"clear": True})
        return result

    @mcp.tool(
        name="ask_menu_on_glasses",
        description="Show a swipe-controlled menu on the glasses, return the selected choice, then clear the display.",
        tags={"interaction", "display", "menu"},
    )
    async def ask_menu_on_glasses(
        prompt: str,
        choices: list[str],
        title: Optional[str] = None,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """
        Args:
            prompt: プロンプトテキスト
            choices: 選択肢のリスト
            title: タイトル（オプショナル）
            timeout_sec: タイムアウト秒数
        
        操作: スワイプ上下で移動、タップで決定、ダブルタップでキャンセル
        
        Returns:
            {"accepted": bool, "choice": str, "index": int} または {"cancelled": True}
        """
        cleaned_choices = _clean_choices(choices)
        cursor = 0
        display_result = await control.display({"text": _format_menu_text(prompt, cleaned_choices, cursor, title)})
        if _display_failed(display_result):
            return {"accepted": False, "display": display_result}

        while True:
            try:
                event = await control.wait_for_touch(timeout_sec=timeout_sec, allowed_gestures=MENU_GESTURES)
            except (TimeoutError, asyncio.TimeoutError):
                return await _clear_interaction_display(
                    control,
                    {"accepted": False, "timeout": True, "display": display_result},
                )

            gesture = str(event.get("data", {}).get("gesture", ""))
            if gesture == "double_tap":
                return await _clear_interaction_display(
                    control,
                    {"accepted": False, "cancelled": True, "gesture": gesture, "event": event},
                )
            if gesture == "single_tap":
                return await _clear_interaction_display(
                    control,
                    {
                        "accepted": True,
                        "choice": cleaned_choices[cursor],
                        "index": cursor,
                        "gesture": gesture,
                        "event": event,
                    },
                )
            if gesture == "swipe_up":
                cursor = (cursor - 1) % len(cleaned_choices)
            elif gesture == "swipe_down":
                cursor = (cursor + 1) % len(cleaned_choices)
            display_result = await control.display({"text": _format_menu_text(prompt, cleaned_choices, cursor, title)})

    @mcp.tool(
        name="ask_character_on_glasses",
        description="Show a game-style dialogue UI with speaker icon and swipe-controlled choices, return the selection, then clear the display.",
        tags={"interaction", "display", "menu", "dialogue", "game"},
    )
    async def ask_character_on_glasses(
        character_name: str,
        dialogue: str,
        choices: list[str],
        icon: Optional[str] = None,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """
        Args:
            character_name: 話者名（ゲーム風対話UIの発言者名）
            dialogue: 発言内容（セリフ）
            choices: 選択肢のリスト
            icon: 話者アイコン（data URL、base64、ファイルパス、または短いテキスト/絵文字。省略時はデフォルトアイコン）
            timeout_sec: タイムアウト秒数
        
        操作: スワイプ上下で移動、タップで決定、ダブルタップでキャンセル
        
        Returns:
            {"accepted": bool, "choice": str, "index": int} または {"cancelled": True}
        """
        cleaned_choices = _clean_choices(choices)
        icon_data = _coerce_dialogue_icon(icon)
        cursor = 0
        display_result = await control.display(
            _build_dialogue_layout(character_name, dialogue, cleaned_choices, cursor, icon_data)
        )
        if _display_failed(display_result):
            return {"accepted": False, "display": display_result}

        while True:
            try:
                event = await control.wait_for_touch(timeout_sec=timeout_sec, allowed_gestures=MENU_GESTURES)
            except (TimeoutError, asyncio.TimeoutError):
                return await _clear_interaction_display(
                    control,
                    {"accepted": False, "timeout": True, "display": display_result},
                )

            gesture = str(event.get("data", {}).get("gesture", ""))
            if gesture == "double_tap":
                return await _clear_interaction_display(
                    control,
                    {"accepted": False, "cancelled": True, "gesture": gesture, "event": event},
                )
            if gesture == "single_tap":
                return await _clear_interaction_display(
                    control,
                    {
                        "accepted": True,
                        "choice": cleaned_choices[cursor],
                        "index": cursor,
                        "gesture": gesture,
                        "event": event,
                    },
                )
            if gesture == "swipe_up":
                cursor = (cursor - 1) % len(cleaned_choices)
            elif gesture == "swipe_down":
                cursor = (cursor + 1) % len(cleaned_choices)
            display_result = await control.display(
                _build_dialogue_layout(character_name, dialogue, cleaned_choices, cursor, icon_data)
            )

    @mcp.tool(
        name="ask_client_user",
        description="Ask the MCP client UI for structured user input when the client supports elicitation.",
        tags={"interaction", "elicitation"},
    )
    async def ask_client_user(
        message: str,
        response_kind: Literal["text", "integer", "number", "boolean", "approval"] = "text",
        choices: Optional[list[str]] = None,
        ctx: Context = CurrentContext(),
    ) -> dict[str, Any]:
        """
        Args:
            message: ユーザーへのメッセージ
            response_kind: 応答の種類（text, integer, number, boolean, approval）
            choices: 選択肢のリスト（指定時は response_kind を上書き）
            ctx: FastMCP コンテキスト（自動注入）
        
        Returns:
            {"action": "accept", "data": ...} または {"action": "reject"} または {"accepted": False, "error": str}
        """
        response_type = _client_user_response_type(response_kind, choices)

        try:
            result = await ctx.elicit(message=message, response_type=response_type)
        except Exception as exc:
            return {"accepted": False, "error": str(exc)}

        return _format_elicit_result(result)

    return mcp


def _build_http_url(server: str, path: str) -> str:
    """server URL とパスを結合して完全な HTTP URL を構築する。"""
    return urljoin(server.rstrip("/") + "/", path.lstrip("/"))


def _coerce_image_reference(image: str) -> str:
    """画像参照を data URL に変換する。
    
    Args:
        image: data URL、base64 文字列、またはローカルファイルパス
    
    Returns:
        data URL 形式の文字列
    """
    value = image.strip()
    if value.startswith("data:"):
        return value

    path = Path(value).expanduser()
    try:
        if path.is_file():
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
    except OSError:
        pass

    # ファイルではない場合は base64 文字列としてそのまま返す
    return value


def _coerce_dialogue_icon(icon: Optional[str]) -> str:
    """ゲーム風対話UI用のアイコン引数を表示可能な画像データに変換する。
    
    Args:
        icon: data URL、ファイルパス、base64、または短いテキスト/絵文字
    
    Returns:
        data URL 形式の画像データ
    """
    if not icon:
        return _default_dialogue_icon_data_url()

    value = icon.strip()
    if not value:
        return _default_dialogue_icon_data_url()
    if value.startswith("data:") or _is_existing_file(value) or _is_valid_base64(value):
        return _coerce_image_reference(value)
    return _text_dialogue_icon_data_url(value)


def _is_existing_file(value: str) -> bool:
    try:
        return Path(value).expanduser().is_file()
    except OSError:
        return False


def _is_valid_base64(value: str) -> bool:
    try:
        base64.b64decode(value, validate=True)
    except Exception:
        return False
    return True


def _normalize_layout_element(element: dict[str, Any]) -> dict[str, Any]:
    """レイアウト要素を正規化する。image フィールドを image_base64 に変換。"""
    normalized = dict(element)
    if normalized.get("type") != "image":
        return normalized
    image_value = normalized.get("image_base64", normalized.get("image"))
    if image_value is not None:
        normalized["image_base64"] = _coerce_image_reference(str(image_value))
    normalized.pop("image", None)
    return normalized


def _normalize_gestures(gestures: Optional[list[str]]) -> Optional[set[str]]:
    """ジェスチャーリストを正規化し、無効な値を検証する。"""
    if not gestures:
        return None
    normalized = {str(gesture).strip() for gesture in gestures if str(gesture).strip()}
    invalid = normalized - VALID_GESTURES
    if invalid:
        raise ValueError(f"invalid gestures: {', '.join(sorted(invalid))}")
    return normalized


def _build_choice_map(choices: Optional[list[str]]) -> dict[str, str]:
    """選択肢リストをジェスチャーへのマッピング辞書に変換する。最大4個まで。"""
    if not choices:
        return {}
    cleaned = [str(choice).strip() for choice in choices if str(choice).strip()]
    if not cleaned:
        return {}
    if len(cleaned) > len(CHOICE_GESTURES):
        raise ValueError("choices supports at most four options")
    return dict(zip(CHOICE_GESTURES, cleaned))


def _format_question(question: str, choice_map: dict[str, str]) -> str:
    """質問文と選択肢マッピングから表示用テキストを整形する。"""
    if not choice_map:
        return question
    lines = [question, ""]
    lines.extend(f"{CHOICE_LABELS[gesture]}: {choice}" for gesture, choice in choice_map.items())
    return "\n".join(lines)


async def _clear_interaction_display(control: GatewayControl, result: dict[str, Any]) -> dict[str, Any]:
    """Clear a display-backed interaction before returning its result."""

    result["clear"] = await control.display({"clear": True})
    return result


def _display_failed(display_result: dict[str, Any]) -> bool:
    return display_result.get("accepted") is False


def _client_user_response_type(
    response_kind: Literal["text", "integer", "number", "boolean", "approval"],
    choices: Optional[list[str]],
) -> Any:
    cleaned_choices = [str(choice).strip() for choice in choices or [] if str(choice).strip()]
    if cleaned_choices:
        return cleaned_choices
    if response_kind == "integer":
        return int
    if response_kind == "number":
        return float
    if response_kind == "boolean" or response_kind == "approval":
        return bool
    return str


def _format_elicit_result(result: Any) -> dict[str, Any]:
    action = _coerce_elicit_action(getattr(result, "action", ""))
    payload: dict[str, Any] = {"action": action}
    if action == "accept":
        payload["data"] = getattr(result, "data", None)
    return payload


def _coerce_elicit_action(action: Any) -> str:
    if isinstance(action, str):
        return action
    value = getattr(action, "value", None)
    if isinstance(value, str):
        return value
    return str(action)


def _clean_choices(choices: list[str]) -> list[str]:
    """選択肢リストから空文字列を削除する。少なくとも1個必要。"""
    cleaned = [str(choice).strip() for choice in choices if str(choice).strip()]
    if not cleaned:
        raise ValueError("choices must contain at least one non-empty item")
    return cleaned


def _format_menu_text(
    prompt: str,
    choices: list[str],
    cursor: int,
    title: Optional[str] = None,
) -> str:
    """メニュー表示用のテキストを整形する。カーソル位置に '>' を追加。"""
    lines: list[str] = []
    if title:
        lines.extend([title, ""])
    lines.extend([prompt, ""])
    lines.extend(("> " if index == cursor else "  ") + choice for index, choice in enumerate(choices))
    lines.extend(["", "swipe: move / tap: select / double tap: cancel"])
    return "\n".join(lines)


def _build_dialogue_layout(
    character_name: str,
    dialogue: str,
    choices: list[str],
    cursor: int,
    icon_data: str,
) -> dict[str, Any]:
    """ゲーム風対話 UI のレイアウトを構築する。
    
    レイアウト:
    - 左上: 話者アイコン (100x100)
    - 右上: 話者名と発言内容
    - 下部: 選択肢リスト（タッチイベント受付）
    """
    return {
        "elements": [
            {
                "type": "image",
                "image_base64": icon_data,
                "x": 0,
                "y": 0,
                "width": DIALOGUE_ICON_SIZE,
                "height": DIALOGUE_ICON_SIZE,
            },
            {
                "type": "text",
                "text": f"{character_name}: {dialogue}",
                "x": DIALOGUE_ICON_SIZE + 8,
                "y": 0,
                "width": 576 - DIALOGUE_ICON_SIZE - 8,
                "height": 150,
                "border_width": 1,
                "border_color": 15,
                "padding": 4,
                "container_name": "dialogue",
                "capture_events": True,
            },
            {
                "type": "text",
                "text": _format_dialogue_choices(choices, cursor),
                "x": 0,
                "y": 200 + 8,
                "width": 576,
                "height": 288 - 150 - 8,
                "border_width": 1,
                "border_color": 15,
                "padding": 6,
                "container_name": "choices",
                "capture_events": False,
            },
        ]
    }


def _format_dialogue_choices(choices: list[str], cursor: int) -> str:
    """ゲーム風対話 UI の選択肢リストを整形する。"""
    lines = [("> " if index == cursor else "  ") + choice for index, choice in enumerate(choices)]
    return "\n".join(lines)


def _default_dialogue_icon_data_url() -> str:
    """デフォルトのゲーム風対話アイコンを生成する。シンプルな顔アイコン。"""
    from PIL import Image, ImageDraw

    image = Image.new("L", (DIALOGUE_ICON_SIZE, DIALOGUE_ICON_SIZE), color=0)
    draw = ImageDraw.Draw(image)
    # 顔の輪郭
    draw.ellipse([8, 6, 92, 94], outline=220, width=3)
    # 左目
    draw.ellipse([26, 30, 42, 46], fill=220)
    # 右目
    draw.ellipse([58, 30, 74, 46], fill=220)
    # 口（笑顔）
    draw.arc([30, 54, 70, 80], start=15, end=165, fill=220, width=3)
    # 髪（小さな楕円）
    draw.ellipse([42, 12, 58, 22], fill=180)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# スタンドアロンモード用のグローバル MCP インスタンス（環境変数から接続先を取得）
def _text_dialogue_icon_data_url(text: str) -> str:
    """短いテキストや記号をゲーム風対話アイコン画像としてレンダリングする。
    
    Args:
        text: アイコンに表示するテキスト（絵文字や短い文字列）
    
    Returns:
        data URL 形式の画像データ
    """
    from PIL import Image, ImageDraw

    image = Image.new("L", (DIALOGUE_ICON_SIZE, DIALOGUE_ICON_SIZE), color=0)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([4, 4, 96, 96], radius=16, outline=220, width=3)

    display_text = text.strip()[:4] or "?"
    font = _load_icon_font(24)
    bbox = (0, 0, 0, 0)
    text_width = 0
    text_height = 0
    for font_size in (56, 48, 40, 32, 24):
        font = _load_icon_font(font_size)
        bbox = draw.textbbox((0, 0), display_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= 80 and text_height <= 72:
            break

    x = (DIALOGUE_ICON_SIZE - text_width) / 2 - bbox[0]
    y = (DIALOGUE_ICON_SIZE - text_height) / 2 - bbox[1]
    draw.text((x, y), display_text, fill=230, font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _load_icon_font(size: int):
    from PIL import ImageFont

    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = [
        windows_dir / "Fonts" / "seguisym.ttf",
        windows_dir / "Fonts" / "seguiemj.ttf",
        windows_dir / "Fonts" / "arial.ttf",
        "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


mcp = create_gateway_mcp()


if __name__ == "__main__":
    # コマンドラインから直接実行された場合は stdio モードで起動
    mcp.run()
