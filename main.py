"""AstrBot 插件：今天是什么小猪（移植自 huannai_plugin_rollpig）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from . import core
except ImportError:  # pragma: no cover - 便于单元测试直接 import main
    import core  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_rollpig"
AUTO_REFRESH_TASK_NAME = "rollpig_auto_refresh"

HELP_TEXT = """今日小猪 - 抽取今天属于你的小猪
随机小猪 - 从PigHub随机获取一张猪猪图
找猪 + 关键词 - 从PigHub中搜索关键词相关的猪猪图片并发送（默认最多8张）
刷新小猪 - 手动从PigHub刷新图库缓存""".strip()


class Main(star.Star):
    """今天是什么小猪 AstrBot 插件主类。"""

    def __init__(self, context: Any, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.context = context
        self.config: dict[str, Any] = config if config is not None else {}
        self._data_dir: Path = self._resolve_data_dir()
        self._pig_list: list[dict[str, Any]] = core.load_pig_list()
        self._today_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件加载时检查资源并按配置启动每日 0 点自动刷新任务。"""
        self._data_dir = self._resolve_data_dir()
        self._pig_list = core.load_pig_list()
        if not self._pig_list:
            logger.error("[rollpig] 猪圈空荡荡，请检查资源文件 resource/pig.json！")

        if self._cfg_bool("auto_refresh_enabled", True):
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._auto_refresh_loop(),
                    name=AUTO_REFRESH_TASK_NAME,
                )
        logger.info(
            "[rollpig] 插件已加载 | pigs=%s auto_refresh=%s data_dir=%s",
            len(self._pig_list),
            self._cfg_bool("auto_refresh_enabled", True),
            self._data_dir,
        )

    async def terminate(self) -> None:
        """插件卸载/重载时取消后台刷新任务。"""
        task = self._refresh_task
        self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[rollpig] 停止自动刷新任务时出错: %s", exc)
        logger.info("[rollpig] 插件已卸载")

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        try:
            base = Path(get_astrbot_plugin_data_path())
            return base / PLUGIN_NAME
        except Exception:
            return core.PLUGIN_DIR / "data"

    def _cfg_str(self, key: str, default: str) -> str:
        val = self.config.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key)
        if isinstance(val, bool):
            return val
        return default

    def _cfg_int(self, key: str, default: int, min_val: int, max_val: int) -> int:
        val = self.config.get(key)
        try:
            parsed = int(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(min_val, min(max_val, parsed))

    def _cfg_timezone(self) -> str:
        return self._cfg_str("timezone", core.DEFAULT_TIMEZONE)

    def _cfg_max_search_results(self) -> int:
        return self._cfg_int("max_search_results", 8, 1, 20)

    def _cfg_request_timeout(self) -> float:
        return float(self._cfg_int("request_timeout", 30, 5, 120))

    def _cfg_pighub_api_url(self) -> str:
        return self._cfg_str("pighub_api_url", core.DEFAULT_PIGHUB_API_URL)

    def _cfg_pighub_image_base_url(self) -> str:
        return self._cfg_str("pighub_image_base_url", core.DEFAULT_PIGHUB_IMAGE_BASE_URL)

    # ------------------------------------------------------------------
    # 后台定时刷新
    # ------------------------------------------------------------------
    async def _auto_refresh_loop(self) -> None:
        while True:
            sleep_seconds = core.seconds_until_next_midnight(self._cfg_timezone())
            try:
                await asyncio.sleep(sleep_seconds)
                count = await core.refresh_pighub_cache(
                    data_dir=self._data_dir,
                    api_url=self._cfg_pighub_api_url(),
                    timeout=self._cfg_request_timeout(),
                )
                if count > 0:
                    logger.info("[rollpig] 成功从 PigHub 刷新 %s 头猪猪", count)
                else:
                    logger.warning("[rollpig] PigHub 中找不到猪猪，未能刷新数据。")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[rollpig] 从 PigHub 获取猪猪失败: %s", exc)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("今日小猪帮助")
    async def send_help(self, event: AstrMessageEvent):
        """查看今天是什么小猪插件帮助。"""
        yield event.plain_result(HELP_TEXT)

    @filter.command("今日小猪")
    async def send_today_pig(self, event: AstrMessageEvent):
        """抽取今天属于你的小猪类型。"""
        if not self._pig_list:
            self._pig_list = core.load_pig_list()
        if not self._pig_list:
            yield event.plain_result("猪圈空荡荡，请检查资源文件！")
            return

        today_str = core.local_now(self._cfg_timezone()).date().isoformat()
        user_id = str(event.get_sender_id())
        today_path = self._data_dir / "today.json"

        async with self._today_lock:
            pig = core.draw_today_pig(
                user_id=user_id,
                pig_list=self._pig_list,
                today_path=today_path,
                today_str=today_str,
            )

        chain: list[Any] = [Plain(f"今日你是：{pig.get('name', '未知小猪')}\n")]
        img_path = core.find_image_file(str(pig.get("id", "")))
        if img_path is not None:
            chain.append(Image.fromFileSystem(str(img_path)))
        chain.append(
            Plain(
                f"\n{pig.get('description', '')}\n分析：{pig.get('analysis', '')}"
            )
        )
        yield event.chain_result(chain)

    @filter.command("随机小猪")
    async def send_random_pig(self, event: AstrMessageEvent):
        """从 PigHub 随机获取一张猪猪图。"""
        images = core.load_pighub_images(self._data_dir)
        pig = core.pick_random_pighub_image(images)
        if pig is None:
            yield event.plain_result("猪圈空荡荡...")
            return

        image_url = core.build_pighub_image_url(
            pig, base_url=self._cfg_pighub_image_base_url()
        )
        yield event.chain_result([Image.fromURL(image_url)])

    @filter.command("找猪")
    async def find_pig(self, event: AstrMessageEvent):
        """从 PigHub 中按关键词搜索猪猪图片。"""
        keyword = self._extract_find_keyword(event.message_str or "")
        if not keyword:
            yield event.plain_result("请输入关键词，例如：找猪 喜报")
            return

        images = core.load_pighub_images(self._data_dir)
        if not images:
            yield event.plain_result("猪圈空荡荡...")
            return

        found_pigs = core.search_pighub_images(
            images,
            keyword=keyword,
            limit=self._cfg_max_search_results(),
        )
        if not found_pigs:
            yield event.plain_result("你要找的猪仔离家出走了~")
            return

        base_url = self._cfg_pighub_image_base_url()
        chain: list[Any] = []
        for idx, pig in enumerate(found_pigs):
            title = str(pig.get("title") or pig.get("filename") or "小猪")
            image_url = core.build_pighub_image_url(pig, base_url=base_url)
            if idx > 0:
                chain.append(Plain("\n"))
            chain.append(Plain(f"{title}\n"))
            chain.append(Image.fromURL(image_url))

        yield event.chain_result(chain)

    @filter.command("刷新小猪")
    async def refresh_pig_data(self, event: AstrMessageEvent):
        """手动从 PigHub 刷新猪猪图库缓存。"""
        if self._cfg_bool("refresh_admin_only", False) and not event.is_admin():
            yield event.plain_result("仅管理员可以执行刷新小猪。")
            return

        try:
            count = await core.refresh_pighub_cache(
                data_dir=self._data_dir,
                api_url=self._cfg_pighub_api_url(),
                timeout=self._cfg_request_timeout(),
            )
            if count > 0:
                msg = f"成功从 PigHub 刷新 {count} 头猪猪"
            else:
                msg = "刷新失败，PigHub 中找不到猪猪。"
            yield event.plain_result(msg)
        except Exception as exc:
            yield event.plain_result(f"刷新失败，无法从PigHub获取数据。{exc}")

    @staticmethod
    def _extract_find_keyword(message_str: str) -> str:
        text = message_str.strip()
        if text.startswith("/"):
            text = text[1:].lstrip()
        if text.startswith("找猪"):
            text = text[len("找猪") :].strip()
        return text
