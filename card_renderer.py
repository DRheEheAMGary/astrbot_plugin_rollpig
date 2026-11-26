"""Pillow card image renderer for astrbot_plugin_rollpig (ported from nonebot-plugin-rollpig)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime environment without Pillow
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    ImageSequence = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

logger = logging.getLogger("astrbot_plugin_rollpig.card_renderer")

CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240

GIF_TARGET_FRAMES = 60
GIF_MAX_DECODE_WORK_PIXELS = 16_000_000
GIF_ABSOLUTE_MAX_SOURCE_FRAMES = 600
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100
GIF_PALETTE_SAMPLE_SIZE = 96
GIF_RENDER_CONCURRENCY = 2

CARD_DISK_CACHE_MAX_BYTES = 64 * 1024 * 1024
CARD_CACHE_VERSION = 1
CARD_DISK_CACHE_MAGIC = b"ROLLPIG-CARD-CACHE-V1\n"
CARD_DISK_CACHE_HEADER_MAX_BYTES = 4096

NAME_FONT_SIZE = 48
DESC_FONT_SIZE = 30
ANALYSIS_FONT_MAX_SIZE = 28
ANALYSIS_FONT_MIN_SIZE = 24

NAME_MARGIN_TOP = 20
DESC_MARGIN_TOP = 20
ANALYSIS_MARGIN_TOP = 30

NAME_LINE_HEIGHT = 58
DESC_LINE_HEIGHT = 38
ANALYSIS_LINE_HEIGHT_FACTOR = 1.5

BACKGROUND_COLOR = (255, 255, 255, 255)
NAME_COLOR = (32, 32, 32, 255)
DESC_COLOR = (85, 85, 85, 255)
ANALYSIS_COLOR = (51, 51, 51, 255)
PLACEHOLDER_BG = (255, 226, 239, 255)
PLACEHOLDER_FG = (154, 92, 135, 255)

PACKAGE_FONT_DIR = Path(__file__).resolve().parent / "resource" / "fonts"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#@%-]+|[^\S\n]+|\n|.", re.S)


@dataclass(frozen=True)
class PigCardRenderResult:
    """今日小猪卡片渲染结果；image_format 用于区分 PNG 与动态 GIF。"""

    data: bytes
    image_format: str
    renderer: str
    analysis_font_size: int
    analysis_lines: int


@dataclass(frozen=True)
class _TextLayout:
    name_line: str
    desc_line: str
    analysis_lines: list[str]
    analysis_font: Any
    analysis_font_size: int
    analysis_line_height: int
    total_height: int


@dataclass(frozen=True)
class _PreparedCard:
    """不含头像的卡片底层；GIF 渲染时逐帧复用，避免重复排版和绘制文字。"""

    canvas: Any
    layout: _TextLayout
    avatar_y: int


_card_cache_lock = threading.RLock()
_card_render_tasks: dict[tuple[object, ...], asyncio.Task[PigCardRenderResult]] = {}
_card_render_tasks_lock = asyncio.Lock()
_gif_render_semaphore: asyncio.Semaphore | None = None
_gif_render_semaphore_loop: asyncio.AbstractEventLoop | None = None
_gif_render_semaphore_guard = threading.Lock()


def _require_pillow() -> None:
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow is not installed")


# ================================ 字体加载 ================================ #


def _resolve_font_path(value: str | None) -> Path | None:
    """解析用户配置的字体路径；相对路径按工作目录处理。"""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    raw_path = Path(text).expanduser()
    return raw_path if raw_path.is_absolute() else Path.cwd() / raw_path


def _font_candidates(*, bold: bool, custom_font_path: str | None = None) -> list[Path]:
    """按优先级列出字体候选。"""
    configured = _resolve_font_path(custom_font_path)
    configured_list = [configured] if configured is not None else []
    packaged_fonts = [
        PACKAGE_FONT_DIR / "SourceHanSansSC-Medium.otf",
        PACKAGE_FONT_DIR / ("msyhbd.ttc" if bold else "msyh.ttc"),
        PACKAGE_FONT_DIR / "msyh.ttc",
        PACKAGE_FONT_DIR / "msyhbd.ttc",
    ]
    macos_fonts = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    ]
    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    linux_fonts = [
        Path(
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf"
            if bold
            else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"
        ),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
        Path(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
            if bold
            else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        ),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    return [*configured_list, *packaged_fonts, *macos_fonts, *windows_fonts, *linux_fonts]


def _file_content_digest(file_path: Path) -> str:
    """流式计算内容摘要，避免同路径资源被替换后继续命中旧卡片。"""
    hasher = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _first_font_file_signature(
    *,
    bold: bool,
    custom_font_path: str | None = None,
) -> tuple[object, ...]:
    """记录实际使用字体指纹；字体变化后不得继续读取旧卡片缓存。"""
    for font_path in _font_candidates(bold=bold, custom_font_path=custom_font_path):
        try:
            stat = font_path.stat()
            if font_path.is_file():
                return str(font_path.resolve()), stat.st_size, _file_content_digest(font_path)
        except OSError:
            continue
    return ("pillow-default",)


@lru_cache(maxsize=8)
def _card_render_asset_signature(custom_font_path: str | None = None) -> tuple[object, ...]:
    """汇总字体指纹，作为磁盘缓存键的一部分。"""
    return (
        _first_font_file_signature(bold=False, custom_font_path=custom_font_path),
        _first_font_file_signature(bold=True, custom_font_path=custom_font_path),
    )


@lru_cache(maxsize=64)
def _load_font(
    size: int,
    *,
    bold: bool = False,
    custom_font_path: str | None = None,
) -> Any:
    """加载指定字号字体；找不到中文字体时降级但不中断渲染。"""
    _require_pillow()
    for font_path in _font_candidates(bold=bold, custom_font_path=custom_font_path):
        if not font_path.exists():
            continue
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception as error:
            logger.debug("RollPig Pillow 字体加载失败: path=%s, error=%s", font_path, error)

    logger.warning("RollPig Pillow 未找到可用中文字体，已退回 Pillow 默认字体。")
    return ImageFont.load_default()


# ================================ 文本测量与换行 ================================ #


def _measure_text(draw: Any, text: str, font: Any) -> int:
    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0])


def _font_line_top(font: Any, y: int, line_height: int) -> int:
    try:
        ascent, descent = font.getmetrics()
        font_height = ascent + descent
    except Exception:
        mask = font.getmask("国Hg")
        bbox = mask.getbbox() or (0, 0, 1, line_height)
        font_height = max(1, bbox[3] - bbox[1])
    return int(y + max(0, line_height - font_height) / 2)


def _drop_last_text_unit(text: str) -> str:
    if not text:
        return ""
    result = text[:-1]
    while result and result[-1] in ("\ufe0f", "\u200d"):
        result = result[:-1]
    return result


def _truncate_to_width(draw: Any, text: str, font: Any, max_width: int) -> str:
    if _measure_text(draw, text, font) <= max_width:
        return text

    ellipsis = "…"
    result = text
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _append_ellipsis_to_width(draw: Any, text: str, font: Any, max_width: int) -> str:
    ellipsis = "…"
    result = text.rstrip()
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _wrap_text_by_width(
    draw: Any,
    text: str,
    font: Any,
    max_width: int,
    *,
    max_lines: int | None = None,
) -> list[str]:
    if not text:
        return []

    lines: list[str] = []
    current = ""

    def push_line(line: str) -> bool:
        lines.append(line.rstrip())
        return max_lines is not None and len(lines) >= max_lines

    for token in _TOKEN_RE.findall(text.replace("\r\n", "\n").replace("\r", "\n")):
        if token == "\n":
            if push_line(current):
                break
            current = ""
            continue

        candidate = f"{current}{token}"
        if current and _measure_text(draw, candidate.rstrip(), font) > max_width:
            if push_line(current):
                break
            current = token.lstrip()
            continue
        current = candidate
    else:
        if current or not lines:
            push_line(current)

    if max_lines is not None and len(lines) >= max_lines:
        consumed = "".join(lines)
        source_compact = text.replace("\n", "")
        if len(consumed) < len(source_compact):
            lines[-1] = _append_ellipsis_to_width(draw, lines[-1].rstrip(), font, max_width)

    return lines


# ================================ 图片与布局生成 ================================ #


def _make_canvas() -> Any:
    return Image.new("RGBA", CANVAS_SIZE, BACKGROUND_COLOR)


def _fit_avatar_frame(frame: Any) -> Any:
    transposed = ImageOps.exif_transpose(frame)
    rgba_frame = transposed.convert("RGBA")
    try:
        return ImageOps.fit(
            rgba_frame,
            (AVATAR_SIZE, AVATAR_SIZE),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    finally:
        if rgba_frame is not transposed:
            rgba_frame.close()
        if transposed is not frame:
            transposed.close()


def _load_avatar(image_file: Path | None) -> Any | None:
    if image_file is None:
        return None
    try:
        with Image.open(image_file) as opened:
            source_frame = opened.copy()
    except Exception as error:
        logger.warning("RollPig 小猪图片读取失败，使用占位图: file=%s, error=%s", image_file, error)
        return None

    try:
        return _fit_avatar_frame(source_frame)
    finally:
        source_frame.close()


def _normalize_gif_duration(raw_duration: object) -> int:
    try:
        duration = int(raw_duration)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


# ================================ 固定卡片磁盘缓存 ================================ #


def _card_text_values(pig_data: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(pig_data.get("name") or "未知小猪"),
        str(pig_data.get("description") or ""),
        str(pig_data.get("analysis") or "你今天是只神秘小猪。"),
    )


def _card_image_signature(image_file: Path | None) -> tuple[object, ...] | None:
    if image_file is None:
        return ("image-missing",)
    try:
        stat = image_file.stat()
        if not image_file.is_file():
            return None
        return str(image_file.resolve()), stat.st_size, _file_content_digest(image_file)
    except OSError:
        return None


def _fixed_card_cache_key(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    custom_font_path: str | None = None,
) -> tuple[object, ...] | None:
    image_signature = _card_image_signature(image_file)
    if image_signature is None:
        return None
    return (
        CARD_CACHE_VERSION,
        *image_signature,
        _card_render_asset_signature(custom_font_path),
        *_card_text_values(pig_data),
    )


def _fixed_card_cache_path(key: tuple[object, ...], cache_dir: Path) -> Path:
    serialized = json.dumps(key, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return cache_dir / f"v{CARD_CACHE_VERSION}-{digest}.cache"


def _serialize_card_disk_cache(result: PigCardRenderResult) -> bytes:
    header = json.dumps(
        {
            "image_format": result.image_format,
            "renderer": result.renderer,
            "analysis_font_size": result.analysis_font_size,
            "analysis_lines": result.analysis_lines,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return CARD_DISK_CACHE_MAGIC + len(header).to_bytes(4, "big") + header + result.data


def _deserialize_card_disk_cache(payload: bytes) -> PigCardRenderResult:
    prefix_size = len(CARD_DISK_CACHE_MAGIC)
    if not payload.startswith(CARD_DISK_CACHE_MAGIC) or len(payload) < prefix_size + 4:
        raise ValueError("缓存魔数不匹配")

    header_size = int.from_bytes(payload[prefix_size : prefix_size + 4], "big")
    if not 0 < header_size <= CARD_DISK_CACHE_HEADER_MAX_BYTES:
        raise ValueError(f"缓存头长度非法: {header_size}")

    header_start = prefix_size + 4
    data_start = header_start + header_size
    if data_start >= len(payload):
        raise ValueError("缓存缺少图片数据")

    metadata = json.loads(payload[header_start:data_start].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("缓存头不是 object")
    image_data = payload[data_start:]
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        detected_format = "gif"
    elif image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_format = "png"
    else:
        raise ValueError("缓存正文不是 PNG 或 GIF")

    metadata_format = str(metadata.get("image_format") or detected_format).lower()
    if metadata_format != detected_format:
        raise ValueError(f"缓存格式不一致: metadata={metadata_format}, data={detected_format}")
    return PigCardRenderResult(
        data=image_data,
        image_format=detected_format,
        renderer=str(metadata.get("renderer") or f"pillow-{detected_format}"),
        analysis_font_size=int(metadata.get("analysis_font_size") or 0),
        analysis_lines=int(metadata.get("analysis_lines") or 0),
    )


def _remove_invalid_card_disk_cache(cache_file: Path, error: Exception) -> None:
    logger.warning("RollPig 卡片磁盘缓存损坏，已忽略: file=%s, error=%s", cache_file, error)
    try:
        cache_file.unlink(missing_ok=True)
    except OSError:
        pass


def _get_fixed_card(key: tuple[object, ...], cache_dir: Path) -> PigCardRenderResult | None:
    cache_file = _fixed_card_cache_path(key, cache_dir)
    with _card_cache_lock:
        try:
            file_size = cache_file.stat().st_size
            if file_size > CARD_DISK_CACHE_MAX_BYTES:
                raise ValueError(f"缓存文件超过总容量: {file_size}")
            result = _deserialize_card_disk_cache(cache_file.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            _remove_invalid_card_disk_cache(cache_file, error)
            return None

        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        return result


def _card_disk_cache_files(cache_dir: Path) -> list[tuple[Path, int, int]]:
    entries: list[tuple[Path, int, int]] = []
    try:
        for path in cache_dir.glob("*.cache"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return []
    return entries


def _trim_card_disk_cache(cache_dir: Path) -> None:
    entries = _card_disk_cache_files(cache_dir)
    total_bytes = sum(size for _, size, _ in entries)
    for path, size, _ in sorted(entries, key=lambda item: item[2]):
        if total_bytes <= CARD_DISK_CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size


def _store_fixed_card(
    key: tuple[object, ...],
    result: PigCardRenderResult,
    cache_dir: Path,
) -> bool:
    is_valid_gif = result.image_format == "gif" and result.data.startswith((b"GIF87a", b"GIF89a"))
    is_valid_png = result.image_format == "png" and result.data.startswith(b"\x89PNG\r\n\x1a\n")
    if not (is_valid_gif or is_valid_png):
        return False

    payload = _serialize_card_disk_cache(result)
    if len(payload) > CARD_DISK_CACHE_MAX_BYTES:
        logger.warning(
            "RollPig 卡片成品超过磁盘缓存总上限，本次不缓存: bytes=%s/%s",
            len(payload),
            CARD_DISK_CACHE_MAX_BYTES,
        )
        return False

    cache_file = _fixed_card_cache_path(key, cache_dir)
    temporary_file = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with _card_cache_lock:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_file.write_bytes(payload)
            os.replace(temporary_file, cache_file)
            _trim_card_disk_cache(cache_dir)
    except OSError as error:
        logger.warning("RollPig 卡片磁盘缓存写入失败，本次继续发送即时结果: file=%s, error=%s", cache_file, error)
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _get_gif_render_semaphore() -> asyncio.Semaphore:
    global _gif_render_semaphore, _gif_render_semaphore_loop

    loop = asyncio.get_running_loop()
    with _gif_render_semaphore_guard:
        if _gif_render_semaphore is None or _gif_render_semaphore_loop is not loop:
            _gif_render_semaphore = asyncio.Semaphore(GIF_RENDER_CONCURRENCY)
            _gif_render_semaphore_loop = loop
        return _gif_render_semaphore


def _gif_frame_groups(frame_count: int) -> tuple[tuple[int, int, int], ...]:
    output_count = min(frame_count, GIF_TARGET_FRAMES)
    groups: list[tuple[int, int, int]] = []
    for output_index in range(output_count):
        start = output_index * frame_count // output_count
        end = (output_index + 1) * frame_count // output_count
        groups.append((start, end, (start + end) // 2))
    return tuple(groups)


def _load_animated_avatar_frames(image_file: Path | None) -> tuple[tuple[Any, int], ...]:
    if image_file is None or image_file.suffix.lower() != ".gif":
        return ()
    decoded: list[Any] = []
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            if not getattr(opened, "is_animated", False) or frame_count <= 1:
                return ()
            width, height = opened.size
            decode_work_pixels = width * height * frame_count
            if frame_count > GIF_ABSOLUTE_MAX_SOURCE_FRAMES:
                logger.warning(
                    "RollPig GIF 超过绝对源帧上限，已降级为静态首帧: file=%s, frames=%s/%s",
                    image_file,
                    frame_count,
                    GIF_ABSOLUTE_MAX_SOURCE_FRAMES,
                )
                return ()
            if decode_work_pixels > GIF_MAX_DECODE_WORK_PIXELS:
                logger.warning(
                    "RollPig GIF 超过解码工作量预算，已降级为静态首帧: file=%s, size=%sx%s, frames=%s",
                    image_file,
                    width,
                    height,
                    frame_count,
                )
                return ()

            groups = _gif_frame_groups(frame_count)
            durations = [0] * len(groups)
            group_index = 0
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                while group_index + 1 < len(groups) and index >= groups[group_index][1]:
                    group_index += 1
                duration = _normalize_gif_duration(frame.info.get("duration", opened.info.get("duration")))
                durations[group_index] += duration
                if index != groups[group_index][2]:
                    continue

                source_frame = frame.copy()
                try:
                    decoded.append(_fit_avatar_frame(source_frame))
                finally:
                    source_frame.close()

            if len(decoded) != len(groups):
                raise ValueError(f"GIF 抽样帧数量异常: decoded={len(decoded)}, expected={len(groups)}")
            return tuple(zip(decoded, durations))
    except Exception as error:
        for frame in decoded:
            frame.close()
        logger.warning("RollPig GIF 图片读取失败，回退静态渲染: file=%s, error=%s", image_file, error)
        return ()


def _draw_avatar(
    canvas: Any,
    avatar: Any | None,
    y: int,
    *,
    custom_font_path: str | None = None,
) -> None:
    x = (CANVAS_SIZE[0] - AVATAR_SIZE) // 2
    if avatar is not None:
        canvas.alpha_composite(avatar, (x, y))
        return

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((x, y, x + AVATAR_SIZE, y + AVATAR_SIZE), fill=PLACEHOLDER_BG)
    placeholder_font = _load_font(86, bold=True, custom_font_path=custom_font_path)
    _draw_text_line(
        canvas,
        "猪",
        placeholder_font,
        y + (AVATAR_SIZE - 100) // 2,
        100,
        PLACEHOLDER_FG,
        max_width=AVATAR_SIZE,
    )


def _build_text_layout(
    draw: Any,
    *,
    name: str,
    desc: str,
    analysis: str,
    custom_font_path: str | None = None,
) -> _TextLayout:
    name_font = _load_font(NAME_FONT_SIZE, bold=True, custom_font_path=custom_font_path)
    desc_font = _load_font(DESC_FONT_SIZE, custom_font_path=custom_font_path)

    name_line = _truncate_to_width(draw, name, name_font, CONTENT_WIDTH)
    desc_line = _truncate_to_width(draw, desc, desc_font, CONTENT_WIDTH) if desc else ""

    static_height = AVATAR_SIZE + NAME_MARGIN_TOP + NAME_LINE_HEIGHT
    if desc_line:
        static_height += DESC_MARGIN_TOP + DESC_LINE_HEIGHT
    if analysis:
        static_height += ANALYSIS_MARGIN_TOP

    for analysis_font_size in range(ANALYSIS_FONT_MAX_SIZE, ANALYSIS_FONT_MIN_SIZE - 1, -1):
        analysis_font = _load_font(analysis_font_size, custom_font_path=custom_font_path)
        analysis_line_height = round(analysis_font_size * ANALYSIS_LINE_HEIGHT_FACTOR)
        analysis_lines = _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH) if analysis else []
        total_height = static_height + len(analysis_lines) * analysis_line_height
        if total_height <= CONTENT_SAFE_HEIGHT:
            return _TextLayout(
                name_line=name_line,
                desc_line=desc_line,
                analysis_lines=analysis_lines,
                analysis_font=analysis_font,
                analysis_font_size=analysis_font_size,
                analysis_line_height=analysis_line_height,
                total_height=total_height,
            )

    analysis_font = _load_font(ANALYSIS_FONT_MIN_SIZE, custom_font_path=custom_font_path)
    analysis_line_height = round(ANALYSIS_FONT_MIN_SIZE * ANALYSIS_LINE_HEIGHT_FACTOR)
    available_height = max(analysis_line_height, CONTENT_SAFE_HEIGHT - static_height)
    max_lines = max(1, available_height // analysis_line_height)
    analysis_lines = (
        _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH, max_lines=max_lines)
        if analysis
        else []
    )
    total_height = static_height + len(analysis_lines) * analysis_line_height
    return _TextLayout(
        name_line=name_line,
        desc_line=desc_line,
        analysis_lines=analysis_lines,
        analysis_font=analysis_font,
        analysis_font_size=ANALYSIS_FONT_MIN_SIZE,
        analysis_line_height=analysis_line_height,
        total_height=total_height,
    )


# ================================ 绘制与编码 ================================ #


def _draw_text_line(
    canvas: Any,
    text: str,
    font: Any,
    y: int,
    line_height: int,
    fill: tuple[int, int, int, int],
    *,
    max_width: int,
    align_by_baseline: bool = False,
) -> None:
    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = max(1, bbox[3] - bbox[1])
    if align_by_baseline:
        text_y = _font_line_top(font, y, line_height)
    else:
        text_y = int(y + (line_height - text_h) / 2 - bbox[1])
    draw.text((x, text_y), text, fill=fill, font=font)


def _prepare_card_without_avatar(
    pig_data: Mapping[str, Any],
    *,
    custom_font_path: str | None = None,
) -> _PreparedCard:
    name, desc, analysis = _card_text_values(pig_data)

    canvas = _make_canvas()
    draw = ImageDraw.Draw(canvas)
    layout = _build_text_layout(
        draw,
        name=name,
        desc=desc,
        analysis=analysis,
        custom_font_path=custom_font_path,
    )

    start_y = max(20, (CANVAS_SIZE[1] - layout.total_height) // 2)
    y = start_y + AVATAR_SIZE + NAME_MARGIN_TOP

    name_font = _load_font(NAME_FONT_SIZE, bold=True, custom_font_path=custom_font_path)
    _draw_text_line(canvas, layout.name_line, name_font, y, NAME_LINE_HEIGHT, NAME_COLOR, max_width=CONTENT_WIDTH)
    y += NAME_LINE_HEIGHT

    if layout.desc_line:
        y += DESC_MARGIN_TOP
        desc_font = _load_font(DESC_FONT_SIZE, custom_font_path=custom_font_path)
        _draw_text_line(canvas, layout.desc_line, desc_font, y, DESC_LINE_HEIGHT, DESC_COLOR, max_width=CONTENT_WIDTH)
        y += DESC_LINE_HEIGHT

    if layout.analysis_lines:
        y += ANALYSIS_MARGIN_TOP
        for line in layout.analysis_lines:
            _draw_text_line(
                canvas,
                line,
                layout.analysis_font,
                y,
                layout.analysis_line_height,
                ANALYSIS_COLOR,
                max_width=CONTENT_WIDTH,
                align_by_baseline=True,
            )
            y += layout.analysis_line_height

    return _PreparedCard(canvas=canvas, layout=layout, avatar_y=start_y)


def _encode_png_card(
    prepared: _PreparedCard,
    image_file: Path | None,
    *,
    custom_font_path: str | None = None,
) -> PigCardRenderResult:
    canvas = prepared.canvas.copy()
    avatar = _load_avatar(image_file)
    try:
        _draw_avatar(canvas, avatar, prepared.avatar_y, custom_font_path=custom_font_path)
        rgb_canvas = canvas.convert("RGB")
        try:
            output = BytesIO()
            rgb_canvas.save(output, format="PNG", optimize=True)
        finally:
            rgb_canvas.close()
    finally:
        if avatar is not None:
            avatar.close()
        canvas.close()
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


def _build_gif_palette(
    prepared: _PreparedCard,
    avatar_frames: tuple[tuple[Any, int], ...],
) -> Any:
    sample_size = GIF_PALETTE_SAMPLE_SIZE
    palette_source = Image.new(
        "RGB",
        (sample_size, sample_size * (len(avatar_frames) + 1)),
        BACKGROUND_COLOR[:3],
    )

    static_rgb = prepared.canvas.convert("RGB")
    try:
        full_card_sample = static_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
        try:
            palette_source.paste(full_card_sample, (0, 0))
        finally:
            full_card_sample.close()
    finally:
        static_rgb.close()

    avatar_box = (
        (CANVAS_SIZE[0] - AVATAR_SIZE) // 2,
        prepared.avatar_y,
        (CANVAS_SIZE[0] + AVATAR_SIZE) // 2,
        prepared.avatar_y + AVATAR_SIZE,
    )
    avatar_background = prepared.canvas.crop(avatar_box)
    try:
        for index, (avatar, _) in enumerate(avatar_frames, 1):
            sample = avatar_background.copy()
            try:
                sample.alpha_composite(avatar, (0, 0))
                sample_rgb = sample.convert("RGB")
            finally:
                sample.close()
            try:
                resized = sample_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
            finally:
                sample_rgb.close()
            try:
                palette_source.paste(resized, (0, sample_size * index))
            finally:
                resized.close()
    finally:
        avatar_background.close()

    try:
        return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    finally:
        palette_source.close()


def _encode_gif_card(
    prepared: _PreparedCard,
    avatar_frames: tuple[tuple[Any, int], ...],
    *,
    custom_font_path: str | None = None,
) -> PigCardRenderResult:
    palette = _build_gif_palette(prepared, avatar_frames)
    output_frames: list[Any] = []
    durations: list[int] = []
    try:
        for avatar, duration in avatar_frames:
            frame = prepared.canvas.copy()
            try:
                _draw_avatar(frame, avatar, prepared.avatar_y, custom_font_path=custom_font_path)
                rgb_frame = frame.convert("RGB")
                try:
                    output_frames.append(rgb_frame.quantize(palette=palette, dither=Image.Dither.NONE))
                finally:
                    rgb_frame.close()
            finally:
                frame.close()
            durations.append(duration)

        output = BytesIO()
        output_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=output_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        result_data = output.getvalue()
    finally:
        palette.close()
        for frame in output_frames:
            frame.close()
    return PigCardRenderResult(
        data=result_data,
        image_format="gif",
        renderer="pillow-gif",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


# ================================ 对外入口 ================================ #


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_dir: Path,
    font_path: str | None = None,
    _final_cache_key: tuple[object, ...] | None = None,
) -> PigCardRenderResult:
    _require_pillow()
    final_cache_key = _final_cache_key or _fixed_card_cache_key(
        pig_data, image_file, custom_font_path=font_path
    )
    if final_cache_key is not None:
        cached = _get_fixed_card(final_cache_key, cache_dir)
        if cached is not None:
            return cached

    prepared = _prepare_card_without_avatar(pig_data, custom_font_path=font_path)
    avatar_frames: tuple[tuple[Any, int], ...] = ()
    try:
        avatar_frames = _load_animated_avatar_frames(image_file)
        result = (
            _encode_gif_card(prepared, avatar_frames, custom_font_path=font_path)
            if avatar_frames
            else _encode_png_card(prepared, image_file, custom_font_path=font_path)
        )
    finally:
        prepared.canvas.close()
        for frame, _ in avatar_frames:
            frame.close()

    if final_cache_key is not None:
        current_cache_key = _fixed_card_cache_key(
            pig_data, image_file, custom_font_path=font_path
        )
        if current_cache_key == final_cache_key:
            _store_fixed_card(final_cache_key, result, cache_dir)
    return result


def _read_fixed_card_cache(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_dir: Path,
    font_path: str | None = None,
) -> tuple[tuple[object, ...] | None, PigCardRenderResult | None]:
    cache_key = _fixed_card_cache_key(pig_data, image_file, custom_font_path=font_path)
    cached = _get_fixed_card(cache_key, cache_dir) if cache_key is not None else None
    return cache_key, cached


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_dir: Path,
    font_path: str | None = None,
) -> PigCardRenderResult:
    """异步入口：渲染 800×800 小猪卡片（支持磁盘缓存与并发合流）。"""
    _require_pillow()
    final_cache_key, cached = await asyncio.to_thread(
        _read_fixed_card_cache,
        pig_data,
        image_file,
        cache_dir=cache_dir,
        font_path=font_path,
    )
    if cached is not None:
        return cached

    if final_cache_key is not None:
        task_key = ("fixed-card", str(cache_dir), *final_cache_key)
        async with _card_render_tasks_lock:
            render_task = _card_render_tasks.get(task_key)
            if render_task is None:
                render_task = asyncio.create_task(
                    _render_card_once(
                        task_key,
                        pig_data,
                        image_file,
                        cache_dir=cache_dir,
                        font_path=font_path,
                        final_cache_key=final_cache_key,
                    )
                )
                _card_render_tasks[task_key] = render_task
        return await asyncio.shield(render_task)

    if image_file is not None and image_file.suffix.lower() == ".gif":
        async with _get_gif_render_semaphore():
            return await asyncio.to_thread(
                _render_pig_card_image_sync,
                pig_data,
                image_file,
                cache_dir=cache_dir,
                font_path=font_path,
            )
    return await asyncio.to_thread(
        _render_pig_card_image_sync,
        pig_data,
        image_file,
        cache_dir=cache_dir,
        font_path=font_path,
    )


async def _render_card_once(
    task_key: tuple[object, ...],
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    cache_dir: Path,
    font_path: str | None,
    final_cache_key: tuple[object, ...],
) -> PigCardRenderResult:
    try:
        if image_file is not None and image_file.suffix.lower() == ".gif":
            async with _get_gif_render_semaphore():
                return await asyncio.to_thread(
                    _render_pig_card_image_sync,
                    pig_data,
                    image_file,
                    cache_dir=cache_dir,
                    font_path=font_path,
                    _final_cache_key=final_cache_key,
                )
        return await asyncio.to_thread(
            _render_pig_card_image_sync,
            pig_data,
            image_file,
            cache_dir=cache_dir,
            font_path=font_path,
            _final_cache_key=final_cache_key,
        )
    finally:
        current_task = asyncio.current_task()
        async with _card_render_tasks_lock:
            if _card_render_tasks.get(task_key) is current_task:
                _card_render_tasks.pop(task_key, None)


def clear_card_renderer_caches() -> None:
    global _gif_render_semaphore, _gif_render_semaphore_loop

    _load_font.cache_clear()
    _card_render_asset_signature.cache_clear()
    with _gif_render_semaphore_guard:
        _gif_render_semaphore = None
        _gif_render_semaphore_loop = None


async def shutdown_card_renderer() -> None:
    async with _card_render_tasks_lock:
        render_tasks = list(_card_render_tasks.values())
    if render_tasks:
        await asyncio.gather(*render_tasks, return_exceptions=True)
    clear_card_renderer_caches()
