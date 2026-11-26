"""Pure business logic for astrbot_plugin_rollpig (no AstrBot imports)."""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLUGIN_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = PLUGIN_DIR / "resource"
PIG_INFO_PATH = RESOURCE_DIR / "pig.json"
BUNDLED_PIG_HUB_PATH = RESOURCE_DIR / "pig_hub.json"
IMAGE_DIR = RESOURCE_DIR / "image"
IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "gif")

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PIGHUB_API_URL = "https://pighub.top/api/all-images"
DEFAULT_PIGHUB_IMAGE_BASE_URL = "https://pighub.top/data/"


def local_now(tz_name: str = DEFAULT_TIMEZONE) -> datetime:
    """Return current timezone-aware datetime, falling back to DEFAULT_TIMEZONE if invalid."""
    try:
        tz = ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz)


def seconds_until_next_midnight(
    tz_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> float:
    """Compute seconds until the next 00:00:00 (+1s buffer) in the given timezone."""
    current = now if now is not None else local_now(tz_name)
    next_midnight = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delta = (next_midnight - current).total_seconds() + 1.0
    return max(1.0, delta)


def load_json(path: Path, default: Any) -> Any:
    """Safely load JSON from path, returning default if missing or malformed."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def save_json_atomic(path: Path, data: Any) -> None:
    """Atomically write JSON data to path using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def load_pig_list(path: Path = PIG_INFO_PATH) -> list[dict[str, Any]]:
    """Load the static pig definitions list."""
    data = load_json(path, [])
    return data if isinstance(data, list) else []


def find_image_file(pig_id: str, image_dir: Path = IMAGE_DIR) -> Path | None:
    """Find the local image file for a pig ID across supported extensions."""
    if not pig_id:
        return None
    for ext in IMAGE_EXTS:
        candidate = image_dir / f"{pig_id}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_pighub_images(
    data_dir: Path,
    bundled_path: Path = BUNDLED_PIG_HUB_PATH,
) -> list[dict[str, Any]]:
    """Load PigHub image metadata from data_dir cache, falling back to bundled JSON."""
    cached_path = data_dir / "pig_hub.json"
    for path in (cached_path, bundled_path):
        data = load_json(path, None)
        if isinstance(data, dict):
            images = data.get("images")
            if isinstance(images, list) and images:
                return images
    return []


def build_pighub_image_url(
    pig: dict[str, Any],
    base_url: str = DEFAULT_PIGHUB_IMAGE_BASE_URL,
) -> str:
    """Build a properly percent-encoded PigHub image URL from a pig metadata dict."""
    raw = str(pig.get("thumbnail") or pig.get("filename") or "")
    filename = raw.split("/")[-1]
    base = (base_url or DEFAULT_PIGHUB_IMAGE_BASE_URL).rstrip("/") + "/"
    return base + quote(filename, safe="")


def draw_today_pig(
    user_id: str,
    pig_list: list[dict[str, Any]],
    today_path: Path,
    today_str: str,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Draw or retrieve today's pig for user_id, persisting daily state atomically."""
    if not pig_list:
        raise ValueError("pig_list must not be empty")

    cache = load_json(today_path, {"date": "", "records": {}})
    if (
        not isinstance(cache, dict)
        or cache.get("date") != today_str
        or not isinstance(cache.get("records"), dict)
    ):
        cache = {"date": today_str, "records": {}}

    records: dict[str, Any] = cache["records"]
    uid = str(user_id)
    if uid in records and isinstance(records[uid], dict):
        return records[uid]

    chooser = rng if rng is not None else random
    pig = chooser.choice(pig_list)
    records[uid] = pig
    save_json_atomic(today_path, cache)
    return pig


def pick_random_pighub_image(
    images: list[dict[str, Any]],
    rng: random.Random | None = None,
) -> dict[str, Any] | None:
    """Pick a random entry from the PigHub image list."""
    if not images:
        return None
    chooser = rng if rng is not None else random
    return chooser.choice(images)


def search_pighub_images(
    images: list[dict[str, Any]],
    keyword: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search in PigHub image titles (fallback filename)."""
    kw = keyword.strip().lower()
    if not kw or not images:
        return []
    cap = max(1, limit)
    matches: list[dict[str, Any]] = []
    for pig in images:
        if not isinstance(pig, dict):
            continue
        target = str(pig.get("title") or pig.get("filename") or "").lower()
        if kw in target:
            matches.append(pig)
            if len(matches) >= cap:
                break
    return matches


async def async_fetch_pighub_data(
    api_url: str = DEFAULT_PIGHUB_API_URL,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch PigHub image catalog JSON asynchronously."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(api_url)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}


async def refresh_pighub_cache(
    data_dir: Path,
    api_url: str = DEFAULT_PIGHUB_API_URL,
    timeout: float = 30.0,
) -> int:
    """Fetch PigHub data and persist to data_dir/pig_hub.json if non-empty."""
    data = await async_fetch_pighub_data(api_url=api_url, timeout=timeout)
    images = data.get("images") if isinstance(data, dict) else None
    if isinstance(images, list) and images:
        save_json_atomic(data_dir / "pig_hub.json", data)
        return len(images)
    return 0
