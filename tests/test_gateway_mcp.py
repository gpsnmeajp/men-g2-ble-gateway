from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


class FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: dict[str, object] = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[kwargs.get("name") or func.__name__] = func
            return func

        return decorator

    def run(self) -> None:
        return None


class FakeImageObject:
    def save(self, buffer, format: str) -> None:
        buffer.write(b"fake png")


class FakeImageModule(types.ModuleType):
    def new(self, mode: str, size: tuple[int, int], color: int = 0) -> FakeImageObject:
        return FakeImageObject()


class FakeDraw:
    def ellipse(self, *args, **kwargs) -> None:
        return None

    def arc(self, *args, **kwargs) -> None:
        return None

    def rounded_rectangle(self, *args, **kwargs) -> None:
        return None

    def textbbox(self, position, text: str, font=None) -> tuple[int, int, int, int]:
        return (0, 0, max(1, len(text)) * 10, 20)

    def text(self, *args, **kwargs) -> None:
        return None


class FakeImageDrawModule(types.ModuleType):
    def Draw(self, image: FakeImageObject) -> FakeDraw:
        return FakeDraw()


class FakeImageFontModule(types.ModuleType):
    def truetype(self, font: str, size: int):
        return object()

    def load_default(self):
        return object()


class FakeGateway:
    def __init__(self, gestures: list[str] | None = None, ready_states: list[bool] | None = None) -> None:
        self.display_payloads: list[dict] = []
        self.gestures = list(gestures or [])
        self.ready_states = list(ready_states or [True])
        self.last_ready = self.ready_states[-1]
        self.status_calls = 0

    async def display_payload(self, payload: dict) -> dict:
        self.display_payloads.append(payload)
        return {"accepted": True, "index": len(self.display_payloads) - 1}

    def status_payload(self) -> dict:
        self.status_calls += 1
        if self.ready_states:
            self.last_ready = self.ready_states.pop(0)
        ready = self.last_ready
        return {"server": {"mcp": {"enabled": True}}, "glasses": {"ready": ready, "phase": "ready" if ready else "connecting"}}

    async def wait_for_touch(self, timeout_sec: float, allowed_gestures: set[str] | None = None) -> dict:
        if not self.gestures:
            raise asyncio.TimeoutError()
        gesture = self.gestures.pop(0)
        self.last_allowed_gestures = allowed_gestures
        return {"kind": "glasses.touch", "data": {"gesture": gesture}}


class FakeElicitResult:
    def __init__(self, action: object, data: object = None) -> None:
        self.action = action
        self.data = data


class FakeElicitContext:
    def __init__(self, result: FakeElicitResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def elicit(self, message: str, response_type: object) -> FakeElicitResult:
        self.calls.append({"message": message, "response_type": response_type})
        return self.result


def load_gateway_mcp():
    fastmcp_module = types.ModuleType("fastmcp")
    fastmcp_module.FastMCP = FakeFastMCP

    dependencies_module = types.ModuleType("fastmcp.dependencies")
    dependencies_module.CurrentContext = lambda: None

    context_module = types.ModuleType("fastmcp.server.context")
    context_module.Context = object

    server_module = types.ModuleType("fastmcp.server")
    server_module.context = context_module

    aiohttp_module = types.ModuleType("aiohttp")
    aiohttp_module.WSMsgType = types.SimpleNamespace(TEXT="TEXT")
    aiohttp_module.ClientSession = object

    pil_module = types.ModuleType("PIL")
    image_module = FakeImageModule("PIL.Image")
    image_draw_module = FakeImageDrawModule("PIL.ImageDraw")
    image_font_module = FakeImageFontModule("PIL.ImageFont")

    sys.modules["fastmcp"] = fastmcp_module
    sys.modules["fastmcp.dependencies"] = dependencies_module
    sys.modules["fastmcp.server"] = server_module
    sys.modules["fastmcp.server.context"] = context_module
    sys.modules["aiohttp"] = aiohttp_module
    sys.modules["PIL"] = pil_module
    sys.modules["PIL.Image"] = image_module
    sys.modules["PIL.ImageDraw"] = image_draw_module
    sys.modules["PIL.ImageFont"] = image_font_module
    sys.modules.pop("gateway_mcp", None)
    return importlib.import_module("gateway_mcp")


gateway_mcp = load_gateway_mcp()


class GatewayMcpHelperTests(unittest.TestCase):
    def test_build_choice_map_limits_and_order(self) -> None:
        self.assertEqual(
            gateway_mcp._build_choice_map(["A", "B", "C", "D"]),
            {
                "single_tap": "A",
                "double_tap": "B",
                "swipe_up": "C",
                "swipe_down": "D",
            },
        )
        with self.assertRaises(ValueError):
            gateway_mcp._build_choice_map(["A", "B", "C", "D", "E"])

    def test_normalize_gestures_rejects_invalid_values(self) -> None:
        self.assertEqual(gateway_mcp._normalize_gestures(["single_tap", "swipe_down"]), {"single_tap", "swipe_down"})
        with self.assertRaises(ValueError):
            gateway_mcp._normalize_gestures(["single_tap", "bad"])

    def test_normalize_layout_element_accepts_image_alias_and_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "sample.png"
            image_path.write_bytes(b"not a real png but good enough for base64")
            element = gateway_mcp._normalize_layout_element({"type": "image", "image": str(image_path)})

        self.assertNotIn("image", element)
        self.assertTrue(element["image_base64"].startswith("data:image/png;base64,"))
        encoded = element["image_base64"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), b"not a real png but good enough for base64")

    def test_character_layout_shape(self) -> None:
        layout = gateway_mcp._build_dialogue_layout("Alto", "Hello", ["Talk", "Leave"], 1, "data:image/png;base64,AAAA")
        self.assertEqual(len(layout["elements"]), 3)
        self.assertEqual(layout["elements"][0]["type"], "image")
        self.assertIn("Alto: Hello", layout["elements"][1]["text"])
        self.assertIn("> Leave", layout["elements"][2]["text"])
        self.assertTrue(any(element.get("capture_events") for element in layout["elements"]))

    def test_character_icon_accepts_non_ascii_symbol(self) -> None:
        icon = gateway_mcp._coerce_dialogue_icon("♡")

        self.assertTrue(icon.startswith("data:image/png;base64,"))
        base64.b64decode(icon.split(",", 1)[1], validate=True)


class GatewayMcpToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_waits_until_gateway_is_ready(self) -> None:
        gateway = FakeGateway(ready_states=[False, False, True])
        old_timeout = gateway_mcp.READY_WAIT_TIMEOUT_SEC
        old_interval = gateway_mcp.READY_WAIT_INTERVAL_SEC
        gateway_mcp.READY_WAIT_TIMEOUT_SEC = 1.0
        gateway_mcp.READY_WAIT_INTERVAL_SEC = 0.0
        try:
            mcp = gateway_mcp.create_gateway_mcp(gateway)
            result = await mcp.tools["display_text"]("hello")
        finally:
            gateway_mcp.READY_WAIT_TIMEOUT_SEC = old_timeout
            gateway_mcp.READY_WAIT_INTERVAL_SEC = old_interval

        self.assertTrue(result["accepted"])
        self.assertGreaterEqual(gateway.status_calls, 3)
        self.assertEqual(gateway.display_payloads, [{"text": "hello"}])

    async def test_display_returns_error_when_gateway_never_becomes_ready(self) -> None:
        gateway = FakeGateway(ready_states=[False])
        old_timeout = gateway_mcp.READY_WAIT_TIMEOUT_SEC
        old_interval = gateway_mcp.READY_WAIT_INTERVAL_SEC
        gateway_mcp.READY_WAIT_TIMEOUT_SEC = 0.0
        gateway_mcp.READY_WAIT_INTERVAL_SEC = 0.0
        try:
            mcp = gateway_mcp.create_gateway_mcp(gateway)
            result = await mcp.tools["display_text"]("hello")
        finally:
            gateway_mcp.READY_WAIT_TIMEOUT_SEC = old_timeout
            gateway_mcp.READY_WAIT_INTERVAL_SEC = old_interval

        self.assertFalse(result["accepted"])
        self.assertIn("glasses did not become ready", result["error"])
        self.assertEqual(gateway.display_payloads, [])

    async def test_ask_user_on_glasses_clears_after_choice(self) -> None:
        gateway = FakeGateway(["double_tap"])
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["ask_user_on_glasses"]("Proceed?", ["Yes", "No"])

        self.assertTrue(result["accepted"])
        self.assertEqual(result["choice"], "No")
        self.assertEqual(gateway.display_payloads[-1], {"clear": True})

    async def test_notify_user_displays_then_clears(self) -> None:
        gateway = FakeGateway()
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["notify_user_on_glasses"]("Done", title="Notice", duration_sec=0)

        self.assertTrue(result["accepted"])
        self.assertEqual(gateway.display_payloads, [{"text": "Notice\n\nDone"}, {"clear": True}])

    async def test_menu_moves_cursor_and_selects(self) -> None:
        gateway = FakeGateway(["swipe_down", "single_tap"])
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["ask_menu_on_glasses"]("Choose", ["A", "B", "C"], title="Menu")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["choice"], "B")
        self.assertEqual(result["index"], 1)
        self.assertEqual(len(gateway.display_payloads), 3)
        self.assertIn("> B", gateway.display_payloads[-2]["text"])
        self.assertEqual(gateway.display_payloads[-1], {"clear": True})

    async def test_menu_double_tap_cancels(self) -> None:
        gateway = FakeGateway(["double_tap"])
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["ask_menu_on_glasses"]("Choose", ["A", "B"])

        self.assertFalse(result["accepted"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(gateway.display_payloads[-1], {"clear": True})

    async def test_character_dialogue_moves_cursor_and_selects(self) -> None:
        gateway = FakeGateway(["swipe_down", "single_tap"])
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["ask_character_on_glasses"](
            "Alto",
            "What now?",
            ["Talk", "Leave"],
            icon="data:image/png;base64,AAAA",
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["choice"], "Leave")
        self.assertEqual(result["index"], 1)
        self.assertEqual(len(gateway.display_payloads), 3)
        self.assertIn("> Leave", gateway.display_payloads[-2]["elements"][2]["text"])
        self.assertEqual(gateway.display_payloads[-1], {"clear": True})

    async def test_character_dialogue_accepts_text_icon(self) -> None:
        gateway = FakeGateway(["single_tap"])
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        result = await mcp.tools["ask_character_on_glasses"]("Alto", "Pick?", ["OK"], icon="♡")

        self.assertTrue(result["accepted"])
        icon_payload = gateway.display_payloads[0]["elements"][0]["image_base64"]
        self.assertTrue(icon_payload.startswith("data:image/png;base64,"))
        self.assertNotIn("♡", icon_payload)

    async def test_display_image_converts_file_path(self) -> None:
        gateway = FakeGateway()
        mcp = gateway_mcp.create_gateway_mcp(gateway)

        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "sample.png"
            image_path.write_bytes(b"image bytes")
            await mcp.tools["display_image"](str(image_path), width=10, height=20, dither=True)

        payload = gateway.display_payloads[0]
        self.assertTrue(payload["dither"])
        self.assertEqual(payload["elements"][0]["width"], 10)
        self.assertEqual(payload["elements"][0]["height"], 20)
        self.assertTrue(payload["elements"][0]["image_base64"].startswith("data:image/png;base64,"))

    async def test_ask_client_user_approval_uses_boolean_schema(self) -> None:
        mcp = gateway_mcp.create_gateway_mcp(FakeGateway())
        ctx = FakeElicitContext(FakeElicitResult("accept", True))

        result = await mcp.tools["ask_client_user"]("Proceed?", response_kind="approval", ctx=ctx)

        self.assertEqual(result, {"action": "accept", "data": True})
        self.assertEqual(ctx.calls[0]["response_type"], bool)

    async def test_ask_client_user_choices_are_single_select_schema(self) -> None:
        mcp = gateway_mcp.create_gateway_mcp(FakeGateway())
        ctx = FakeElicitContext(FakeElicitResult("accept", "B"))

        result = await mcp.tools["ask_client_user"]("Choose", choices=[" A ", "", "B"], ctx=ctx)

        self.assertEqual(result, {"action": "accept", "data": "B"})
        self.assertEqual(ctx.calls[0]["response_type"], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
