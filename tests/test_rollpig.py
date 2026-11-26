"""Unit tests for astrbot_plugin_rollpig."""

from __future__ import annotations

import json
import random
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import card_renderer  # noqa: E402
import core  # noqa: E402


def _install_astrbot_stubs() -> None:
    """Install lightweight astrbot stubs in sys.modules for testing main.py."""
    if "astrbot" in sys.modules:
        return

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    core_mod = types.ModuleType("astrbot.core")
    utils_mod = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

    class _DummyLogger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class _Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config or {}

    class _Filter:
        @staticmethod
        def command(name):
            def decorator(fn):
                return fn

            return decorator

    class _Plain:
        def __init__(self, text: str):
            self.text = text

    class _Image:
        def __init__(self, kind: str, target: str):
            self.kind = kind
            self.target = target

        @classmethod
        def fromFileSystem(cls, path: str):
            return cls("file", path)

        @classmethod
        def fromURL(cls, url: str):
            return cls("url", url)

        @classmethod
        def fromBytes(cls, data: bytes):
            return cls("bytes", str(len(data)))

    class _AstrMessageEvent:
        pass

    api_mod.logger = _DummyLogger()
    api_mod.star = types.SimpleNamespace(Star=_Star)
    event_mod.AstrMessageEvent = _AstrMessageEvent
    event_mod.filter = _Filter
    mc_mod.Image = _Image
    mc_mod.Plain = _Plain
    path_mod.get_astrbot_plugin_data_path = lambda: str(PLUGIN_ROOT / "data")

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.utils"] = utils_mod
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod


class _FakeEvent:
    def __init__(self, sender_id: str = "u1", message_str: str = "", admin: bool = False):
        self._sender_id = sender_id
        self.message_str = message_str
        self._admin = admin

    def get_sender_id(self) -> str:
        return self._sender_id

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain: list):
        return ("chain", chain)


class TestResources(unittest.TestCase):
    def test_pig_json_and_images_complete(self):
        pigs = core.load_pig_list()
        self.assertEqual(len(pigs), 62)
        for pig in pigs:
            for field in ("id", "name", "description", "analysis"):
                self.assertTrue(pig.get(field), f"Missing {field} in {pig}")
            img = core.find_image_file(pig["id"])
            self.assertIsNotNone(img, f"Missing image for pig id={pig['id']}")

    def test_bundled_pighub_json_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = core.load_pighub_images(Path(tmp))
            self.assertGreater(len(images), 0)

    def test_bundled_font_exists(self):
        font_file = PLUGIN_ROOT / "resource" / "fonts" / "SourceHanSansSC-Medium.otf"
        self.assertTrue(font_file.is_file())
        self.assertGreater(font_file.stat().st_size, 100_000)


class TestCoreLogic(unittest.TestCase):
    def test_draw_today_pig_idempotent_and_resets_next_day(self):
        pigs = [
            {"id": "a", "name": "A", "description": "dA", "analysis": "aA"},
            {"id": "b", "name": "B", "description": "dB", "analysis": "aB"},
            {"id": "c", "name": "C", "description": "dC", "analysis": "aC"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            today_path = Path(tmp) / "today.json"
            rng = random.Random(42)

            first = core.draw_today_pig("user1", pigs, today_path, "2026-11-26", rng=rng)
            second = core.draw_today_pig("user1", pigs, today_path, "2026-11-26", rng=rng)
            self.assertEqual(first, second)

            other_user = core.draw_today_pig("user2", pigs, today_path, "2026-11-26", rng=rng)
            self.assertIn(other_user, pigs)

            # Corrupt file recovery
            today_path.write_text("{bad json", encoding="utf-8")
            recovered = core.draw_today_pig("user1", pigs, today_path, "2026-11-27", rng=rng)
            self.assertIn(recovered, pigs)
            saved = json.loads(today_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["date"], "2026-11-27")
            self.assertEqual(list(saved["records"].keys()), ["user1"])

    def test_build_pighub_image_url_encoding(self):
        pig = {"thumbnail": "/data/猪名言（丘吉尔）.png"}
        url = core.build_pighub_image_url(pig, "https://pighub.top/data")
        self.assertTrue(url.startswith("https://pighub.top/data/"))
        self.assertNotIn("猪", url)
        self.assertIn("%E7%8C%AA", url)

    def test_search_pighub_images(self):
        images = [
            {"title": "喜报", "thumbnail": "/data/1.png"},
            {"title": "Hello Pig", "thumbnail": "/data/2.png"},
            {"title": "hello world", "thumbnail": "/data/3.png"},
            {"filename": "fallback_pig.png"},
        ]
        self.assertEqual(len(core.search_pighub_images(images, "HELLO", limit=1)), 1)
        self.assertEqual(len(core.search_pighub_images(images, "hello", limit=8)), 2)
        self.assertEqual(len(core.search_pighub_images(images, "fallback", limit=8)), 1)
        self.assertEqual(core.search_pighub_images(images, "不存在", limit=8), [])

    def test_seconds_until_next_midnight(self):
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 26, 23, 59, 0, tzinfo=tz)
        secs = core.seconds_until_next_midnight("Asia/Shanghai", now=now)
        self.assertAlmostEqual(secs, 61.0, places=3)

    def test_card_disk_cache_roundtrip_and_invalid(self):
        png_result = card_renderer.PigCardRenderResult(
            data=b"\x89PNG\r\n\x1a\nfake-png-payload",
            image_format="png",
            renderer="pillow",
            analysis_font_size=28,
            analysis_lines=2,
        )
        raw = card_renderer._serialize_card_disk_cache(png_result)
        parsed = card_renderer._deserialize_card_disk_cache(raw)
        self.assertEqual(parsed, png_result)

        gif_result = card_renderer.PigCardRenderResult(
            data=b"GIF89afake-gif-payload",
            image_format="gif",
            renderer="pillow-gif",
            analysis_font_size=26,
            analysis_lines=3,
        )
        parsed_gif = card_renderer._deserialize_card_disk_cache(
            card_renderer._serialize_card_disk_cache(gif_result)
        )
        self.assertEqual(parsed_gif, gif_result)

        with self.assertRaises(ValueError):
            card_renderer._deserialize_card_disk_cache(b"BAD-MAGIC")

    def test_gif_helpers(self):
        self.assertEqual(card_renderer._normalize_gif_duration(0), 100)
        self.assertEqual(card_renderer._normalize_gif_duration(5), 20)
        self.assertEqual(card_renderer._normalize_gif_duration(5000), 2000)
        groups = card_renderer._gif_frame_groups(120)
        self.assertEqual(len(groups), card_renderer.GIF_TARGET_FRAMES)
        self.assertEqual(groups[0][0], 0)
        self.assertEqual(groups[-1][1], 120)


class TestAsyncAndPluginCommands(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_pighub_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch(
                "core.async_fetch_pighub_data",
                new=AsyncMock(return_value={"images": [{"id": "1", "title": "x"}]}),
            ):
                count = await core.refresh_pighub_cache(data_dir)
                self.assertEqual(count, 1)
                self.assertTrue((data_dir / "pig_hub.json").exists())

            with patch(
                "core.async_fetch_pighub_data",
                new=AsyncMock(return_value={"images": []}),
            ):
                count_empty = await core.refresh_pighub_cache(data_dir)
                self.assertEqual(count_empty, 0)

    async def test_main_commands(self):
        _install_astrbot_stubs()
        import main as plugin_main  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            plugin = plugin_main.Main(
                context=None,
                config={
                    "auto_refresh_enabled": False,
                    "refresh_admin_only": True,
                    "render_card": False,
                },
            )
            plugin._data_dir = Path(tmp)
            await plugin.initialize()
            try:
                # 1. 今日小猪帮助
                res_help = [r async for r in plugin.send_help(_FakeEvent())]
                self.assertEqual(res_help[0][0], "plain")
                self.assertIn("今日小猪", res_help[0][1])

                # 2. 今日小猪
                res_today = [r async for r in plugin.send_today_pig(_FakeEvent(sender_id="42"))]
                self.assertEqual(res_today[0][0], "chain")
                chain = res_today[0][1]
                self.assertTrue(any(getattr(c, "kind", None) == "file" for c in chain))

                # 3. 随机小猪
                res_rand = [r async for r in plugin.send_random_pig(_FakeEvent())]
                self.assertEqual(res_rand[0][0], "chain")
                self.assertEqual(res_rand[0][1][0].kind, "url")

                # 4. 找猪 (empty keyword, match, no match)
                res_empty = [r async for r in plugin.find_pig(_FakeEvent(message_str="找猪"))]
                self.assertIn("请输入关键词", res_empty[0][1])

                res_found = [r async for r in plugin.find_pig(_FakeEvent(message_str="/找猪 喜报"))]
                self.assertEqual(res_found[0][0], "chain")

                res_none = [
                    r async for r in plugin.find_pig(_FakeEvent(message_str="找猪 绝对不存在的词xyz"))
                ]
                self.assertEqual(res_none[0][1], "你要找的猪仔离家出走了~")

                # 5. 刷新小猪 (non-admin blocked, admin allowed)
                res_denied = [
                    r async for r in plugin.refresh_pig_data(_FakeEvent(admin=False))
                ]
                self.assertIn("仅管理员", res_denied[0][1])

                with patch(
                    "core.refresh_pighub_cache",
                    new=AsyncMock(return_value=123),
                ):
                    res_ok = [
                        r async for r in plugin.refresh_pig_data(_FakeEvent(admin=True))
                    ]
                    self.assertEqual(res_ok[0][1], "成功从 PigHub 刷新 123 头猪猪")
            finally:
                await plugin.terminate()

    async def test_today_pig_card_render_and_fallback(self):
        _install_astrbot_stubs()
        import main as plugin_main  # noqa: E402

        fake_card = card_renderer.PigCardRenderResult(
            data=b"\x89PNG\r\n\x1a\ncard",
            image_format="png",
            renderer="pillow",
            analysis_font_size=28,
            analysis_lines=2,
        )
        with tempfile.TemporaryDirectory() as tmp:
            plugin = plugin_main.Main(
                context=None,
                config={"auto_refresh_enabled": False, "render_card": True},
            )
            plugin._data_dir = Path(tmp)
            await plugin.initialize()
            try:
                with patch(
                    "card_renderer.render_pig_card_image",
                    new=AsyncMock(return_value=fake_card),
                ):
                    res = [r async for r in plugin.send_today_pig(_FakeEvent(sender_id="77"))]
                    self.assertEqual(res[0][0], "chain")
                    self.assertEqual(len(res[0][1]), 1)
                    self.assertEqual(res[0][1][0].kind, "bytes")

                with patch(
                    "card_renderer.render_pig_card_image",
                    new=AsyncMock(side_effect=RuntimeError("Pillow unavailable")),
                ):
                    res_fb = [r async for r in plugin.send_today_pig(_FakeEvent(sender_id="77"))]
                    self.assertEqual(res_fb[0][0], "chain")
                    self.assertTrue(any(getattr(c, "kind", None) == "file" for c in res_fb[0][1]))
            finally:
                await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
