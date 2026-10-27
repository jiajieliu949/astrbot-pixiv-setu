import os
import re
import time
import random
import asyncio
import logging
from typing import List, Optional

import aiohttp
import aiofiles
from pathlib import Path

from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger(__name__)

# Lolicon API v2：一个公开的 Pixiv 图片索引服务，返回的 pid 即 Pixiv 作品 ID，
# urls.original 为 Pixiv 图床原始地址。原生支持 tag 筛选。
API_URL = "https://api.lolicon.app/setu/v2"

# 解析标签时要顺手去掉的"无意义字"，避免 "白丝涩图" 被拆成 ["白丝涩图"] 之类
STRIP_CHARS = "色涩瑟图福利塞"

# Lolicon API 支持的图片尺寸
SIZE_OPTIONS = ("original", "regular", "small", "thumb", "mini")


# ==================== 标签解析 ====================

def parse_alias_map(alias_str: str) -> dict:
    """解析中文标签到 API 标签的别名映射，格式：白丝=white_pantyhose,猫娘=cat_girl。"""
    result: dict = {}
    if not alias_str:
        return result
    for pair in re.split(r"[,\n，]+", alias_str):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            result[key] = value
    return result


def extract_tags(text: str, trigger_word: str) -> List[str]:
    """从消息文本里提取标签：去掉触发词本身，再按空白/逗号/顿号拆分。"""
    idx = text.find(trigger_word)
    if idx >= 0:
        text = text[idx + len(trigger_word):]
    for ch in STRIP_CHARS:
        text = text.replace(ch, " ")
    parts = re.split(r"[\s,，、;；]+", text)
    return [p.strip() for p in parts if p.strip()]


def resolve_tags(tags: List[str], alias_map: dict) -> List[str]:
    """把中文标签映射成 API 标签，未配置别名的原样保留。"""
    return [alias_map.get(t, t) for t in tags if t]


# ==================== API ====================

async def fetch_setu(
    tags: Optional[List[str]] = None,
    r18: int = 0,
    num: int = 1,
    size: str = "original",
    exclude_ai: bool = True,
    proxy: str = "",
    tag_match: str = "or",
) -> List[dict]:
    """调用 Lolicon API 拉取 Pixiv 图，返回 data 数组（空列表表示无结果）。"""
    params = {
        "r18": max(0, min(2, r18)),
        "num": max(1, min(20, num)),
        "size": [size if size in SIZE_OPTIONS else "original"],
        "excludeAI": bool(exclude_ai),
    }
    # Lolicon v2：tag 传扁平数组 ["a","b"] 表示"全部满足"(AND)；传 [["a","b"]] 表示"命中任一"(OR)。
    # tag_match 决定用哪种：and=扁平数组（全部命中），or=嵌套数组（命中任一）。
    if tags:
        params["tag"] = tags if tag_match == "and" else [tags]
    if proxy:
        params["proxy"] = proxy

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.post(API_URL, json=params) as resp:
                data = await resp.json()
                if data.get("error"):
                    logger.warning("Lolicon API 报错: %s", data["error"])
                    return []
                return data.get("data", []) or []
    except Exception as exc:
        logger.error("fetch_setu 请求失败: %s", exc)
        return []


def clamp_r18(value) -> int:
    """把配置里的 R18 等级收敛到 0-2；WebUI 填了越界值或非数字时退回 0（非 R18）。"""
    try:
        return max(0, min(2, int(value)))
    except (TypeError, ValueError):
        return 0


def pick_url(item: dict, size: str) -> Optional[str]:
    """从返回的 urls 里挑一个可用地址：优先用户选择尺寸，逐级降级。"""
    urls = item.get("urls") or {}
    if size in urls and urls[size]:
        return urls[size]
    for s in SIZE_OPTIONS:
        if urls.get(s):
            return urls[s]
    return None


def is_send_timeout(exc: BaseException) -> bool:
    """判断发送图片抛出的异常是否为超时（如 NapCat 等平台的 retcode=1200 / Timeout）。"""
    retcode = getattr(exc, "retcode", None)
    if retcode is not None and str(retcode) == "1200":
        return True
    if "timeout" in str(exc).lower():
        return True
    for attr in ("message", "wording"):
        val = getattr(exc, attr, None)
        if val is not None and "timeout" in str(val).lower():
            return True
    return False


def timeout_fallback_text(item: dict, url: str) -> str:
    """图片发送超时时的兜底回复：作者、PID 与图片链接。"""
    lines = ["图被大手吃掉了"]
    author = item.get("author")
    uid = item.get("uid")
    pid = item.get("pid")
    if author:
        lines.append(f"作者：{author}（UID: {uid}）" if uid else f"作者：{author}")
    if pid:
        lines.append(f"PID: {pid}  https://www.pixiv.net/artworks/{pid}")
    if url:
        lines.append(f"图片：{url}")
    return "\n".join(lines)


# ==================== 速率限制 ====================

class RateLimiter:
    """按用户加锁，防止同一用户并发连发刷爆 API；带 TTL 防锁表泄漏。"""

    MAX_LOCKS = 1000
    LOCK_TTL = 60

    def __init__(self):
        self._locks: dict = {}
        self._lock_times: dict = {}

    async def acquire(self, user_id: str) -> bool:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            # 锁卡死（异常退出）时按 TTL 兜底释放
            if time.monotonic() - self._lock_times.get(user_id, 0) > self.LOCK_TTL:
                try:
                    lock.release()
                except RuntimeError:
                    pass
        if lock.locked():
            return False
        await lock.acquire()
        self._lock_times[user_id] = time.monotonic()
        return True

    async def release(self, user_id: str):
        lock = self._locks.get(user_id)
        if lock and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass
        self._lock_times.pop(user_id, None)
        if len(self._locks) > self.MAX_LOCKS:
            stale = [k for k, v in self._locks.items() if not v.locked()]
            for k in stale[: len(stale) // 2]:
                self._locks.pop(k, None)
                self._lock_times.pop(k, None)


_rate_limiter = RateLimiter()


# ==================== 插件主体 ====================

@register(
    "astrbot_plugin_pixiv_setu",
    "AstrBot User",
    "Pixiv 涩图",
    "1.0.0",
    "https://github.com/yourname/astrbot_plugin_pixiv_setu",
)
class PixivSetuPlugin(Star):
    """检测到"来份涩图"时，从 Pixiv（Lolicon API）随机发一张涩图，支持 tag 匹配。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_path = Path(get_astrbot_data_path())
        self.imgs_dir = str(
            data_path / "plugin_data" / "astrbot_plugin_pixiv_setu" / "imgs"
        )
        os.makedirs(self.imgs_dir, exist_ok=True)

    # 监听所有普通消息
    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            text = str(event.message_str or "")
            trigger = self.config.get("trigger_word", "来份涩图")
            if not trigger or trigger not in text:
                return
            tags = extract_tags(text, trigger)
            # 别名映射每次请求时重新解析，保证 WebUI 改配置即时生效
            alias_map = parse_alias_map(self.config.get("tag_alias", ""))
            tags = resolve_tags(tags, alias_map)
            await self._handle_request(event, tags)
        except Exception as exc:
            logger.error("pixiv_setu 处理消息异常: %s", exc)
            try:
                await event.send(event.plain_result(f"插件异常：{exc}"))
                event.stop_event()
            except Exception:
                pass

    async def _handle_request(self, event: AstrMessageEvent, tags: List[str]):
        user_id = "default"
        try:
            user_id = event.get_sender_id()
        except Exception:
            pass

        if not await _rate_limiter.acquire(user_id):
            await event.send(event.plain_result("你太快啦，等一下再试"))
            event.stop_event()
            return

        r18 = self._resolve_r18(event)
        try:
            item = await self._random_item(tags, r18)
            fallback_note = ""

            # 带标签没命中时，兜底随机一张，并告知用户
            if item is None and tags:
                item = await self._random_item([], r18)
                if item is not None:
                    fallback_note = f"（没找到符合「{'、'.join(tags)}」的图，随机来一张）"

            if item is None:
                await event.send(event.plain_result("涩图源暂时抽风了，请稍后再试"))
                event.stop_event()
                return

            size = self.config.get("size", "original")
            url = pick_url(item, size)
            if not url:
                await event.send(event.plain_result("图片地址解析失败，请稍后再试"))
                event.stop_event()
                return

            filename = f"{item.get('pid')}_p{item.get('p')}.{item.get('ext', 'jpg')}"
            path = await self._download_image(url, filename)
            if not path:
                await event.send(event.plain_result("图片下载失败，请稍后再试"))
                event.stop_event()
                return

            try:
                try:
                    await event.send(event.make_result().file_image(path))
                except Exception as exc:
                    # 发送超时（如 retcode=1200）时，别把原始异常怼给用户，改用兜底提示
                    if not is_send_timeout(exc):
                        raise
                    logger.warning("图片发送超时，返回兜底提示 pid=%s", item.get("pid"))
                    await event.send(
                        event.plain_result(timeout_fallback_text(item, url))
                    )
                else:
                    caption = self.config.get("reply_text", "给你涩图~")
                    if fallback_note:
                        caption = f"{caption}\n{fallback_note}"
                    if self.config.get("show_pid", True):
                        pid = item.get("pid")
                        if pid:
                            caption = (
                                f"{caption}\nPID: {pid}  "
                                f"https://www.pixiv.net/artworks/{pid}"
                            )
                    await event.send(event.plain_result(caption))
            finally:
                await self._cleanup(path)

            event.stop_event()
        finally:
            await _rate_limiter.release(user_id)

    def _resolve_r18(self, event: AstrMessageEvent) -> int:
        """决定本次请求的 R18 等级：私聊走独立开关，群聊走全局 r18。

        私聊总开关关闭时强制 0（非 R18），而不是回落到群聊的 r18，
        这样「关掉私聊 R18」在群聊开了 r18=2 的情况下依然能保证私聊安全。
        """
        if self._is_private_chat(event):
            if not self.config.get("private_r18_enable", False):
                return 0
            return clamp_r18(self.config.get("private_r18", 0))
        return clamp_r18(self.config.get("r18", 0))

    @staticmethod
    def _is_private_chat(event: AstrMessageEvent) -> bool:
        """判断是否私聊；老版本 AstrBot 没有 is_private_chat 时按群聊处理（更保守）。"""
        try:
            return bool(event.is_private_chat())
        except Exception as exc:
            logger.debug("is_private_chat 不可用，按群聊处理: %s", exc)
            return False

    async def _random_item(self, tags: List[str], r18: int) -> Optional[dict]:
        """拉取一小批（random_pool）候选，随机挑一张，避免总是拿到最新那张。"""
        pool = max(1, min(20, int(self.config.get("random_pool", 5))))
        results = await fetch_setu(
            tags=tags or None,
            r18=r18,
            num=pool,
            size=self.config.get("size", "original"),
            exclude_ai=bool(self.config.get("exclude_ai", True)),
            proxy=self.config.get("proxy", ""),
            tag_match=self.config.get("tag_match", "and"),
        )
        if not results:
            return None
        return random.choice(results)

    async def _download_image(self, url: str, filename: str) -> Optional[str]:
        """下载图片到本地临时目录，返回文件路径；失败返回 None。"""
        path = os.path.join(self.imgs_dir, filename)
        if os.path.exists(path):
            return path
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45)
            ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    content = await resp.read()
                    if not content:
                        return None
                    async with aiofiles.open(path, "wb") as f:
                        await f.write(content)
                    return path
        except Exception as exc:
            logger.error("下载图片失败: %s", exc)
            return None

    async def _cleanup(self, path: str):
        """发送后删除临时文件，避免堆积。"""
        try:
            if os.path.exists(path):
                await asyncio.to_thread(os.remove, path)
        except Exception as exc:
            logger.warning("清理临时图片失败: %s", exc)

    async def terminate(self):
        """插件停用时清理残留临时图片。"""
        try:
            if os.path.isdir(self.imgs_dir):
                for name in os.listdir(self.imgs_dir):
                    p = os.path.join(self.imgs_dir, name)
                    if os.path.isfile(p):
                        await asyncio.to_thread(os.remove, p)
        except Exception as exc:
            logger.warning("pixiv_setu 清理目录失败: %s", exc)
