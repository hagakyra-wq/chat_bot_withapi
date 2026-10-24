"""Queued banner rendering and durable Telegram-channel storage."""

import asyncio
import itertools
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw
from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

import database
from config import BACKUP_CHANNEL_ID, BANNER_SALT
from yubari_banner import clean_text, content_hash, make_banner
from yubari_banner.bn_common import font

_ACTIVE_REFRESH_SECONDS = 10 * 60


@dataclass
class _RenderJob:
    arguments: dict[str, Any]
    future: asyncio.Future[io.BytesIO]


class _BannerRenderQueue:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[
            tuple[int, int, _RenderJob | None]
        ] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._worker: asyncio.Task[None] | None = None

    async def render(self, priority: int, **arguments: Any) -> io.BytesIO:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="banner-render-queue")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[io.BytesIO] = loop.create_future()
        await self._queue.put(
            (priority, next(self._sequence), _RenderJob(arguments, future))
        )
        return await future

    async def _run(self) -> None:
        while True:
            _priority, _sequence, job = await self._queue.get()
            try:
                if job is None:
                    return
                if job.future.cancelled():
                    continue
                try:
                    buffer = await asyncio.to_thread(make_banner, **job.arguments)
                except Exception as exc:
                    if not job.future.done():
                        job.future.set_exception(exc)
                else:
                    if not job.future.done():
                        job.future.set_result(buffer)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = None
            return
        await self._queue.put((10_000, next(self._sequence), None))
        await self._worker
        self._worker = None


_render_queue = _BannerRenderQueue()


async def _avatar_for_user(bot: Any, user_id: int) -> tuple[bytes | None, str | None]:
    photos = await bot.get_user_profile_photos(user_id, limit=1)
    if not photos.photos or not photos.photos[0]:
        return None, None
    photo = photos.photos[0][-1]
    telegram_file = await bot.get_file(photo.file_id)
    image = await telegram_file.download_as_bytearray()
    return bytes(image), str(photo.file_unique_id)


async def _render_and_backup(
    bot: Any,
    item: dict[str, Any],
    *,
    priority: int,
    force: bool = False,
) -> dict[str, Any]:
    user_id = int(item["user_id"])
    profile = await database.get_global_profile(user_id)
    if profile is None:
        raise RuntimeError(f"Не найден профиль владельца баннера user_id={user_id}.")
    name = clean_text(
        str(profile.get("callsign") or profile.get("display_name") or profile.get("username") or "Без имени")
    ) or "Без имени"
    tag = None
    avatar: bytes | None = None
    avatar_id: str | None = None
    if int(item["level"]) >= 5:
        try:
            avatar, avatar_id = await _avatar_for_user(bot, user_id)
        except Exception:
            logging.warning(
                "Не удалось получить аватар для баннера item_id=%s; будет использован инициал.",
                item["item_id"],
                exc_info=True,
            )
    digest = content_hash(name, tag, avatar_id)
    current_hash = str(item.get("content_hash") or "")
    file_id = str(item.get("file_id") or "")
    if file_id and current_hash == digest:
        return {**item, "content_hash": digest}
    rendered_at = float(item.get("rendered_at") or 0)
    if (
        not force
        and file_id
        and bool(item.get("is_active"))
        and time.time() - rendered_at < _ACTIVE_REFRESH_SECONDS
    ):
        return item

    buffer = await _render_queue.render(
        priority,
        user_id=user_id,
        name=name,
        tag=tag,
        level=int(item["level"]),
        item_seed=str(item["seed"]),
        preset=item.get("preset"),
        avatar=avatar,
        salt=BANNER_SALT,
    )
    caption = (
        f"🖼 Баннер item_id={item['item_id']}\n"
        f"Владелец: {user_id}\nУровень: {int(item['level'])}"
    )
    if int(item["level"]) == 6:
        sent = await bot.send_animation(
            chat_id=BACKUP_CHANNEL_ID,
            animation=buffer,
            caption=caption,
            disable_notification=True,
        )
        if sent.animation is None:
            raise RuntimeError("Telegram принял GIF без данных animation.")
        new_file_id = sent.animation.file_id
    else:
        sent = await bot.send_photo(
            chat_id=BACKUP_CHANNEL_ID,
            photo=buffer,
            caption=caption,
            disable_notification=True,
        )
        if not sent.photo:
            raise RuntimeError("Telegram принял изображение без данных photo.")
        new_file_id = sent.photo[-1].file_id

    old_backup_id = item.get("backup_msg_id")
    await database.set_banner_item_rendering(
        int(item["item_id"]),
        file_id=new_file_id,
        backup_msg_id=sent.message_id,
        content_hash=digest,
    )
    if old_backup_id:
        try:
            await bot.delete_message(BACKUP_CHANNEL_ID, int(old_backup_id))
        except (TelegramError, ValueError):
            logging.exception(
                "Не удалось удалить предыдущую копию баннера item_id=%s.",
                item["item_id"],
            )
    updated = await database.get_banner_item(int(item["item_id"]))
    if updated is None:
        raise RuntimeError(f"Баннер item_id={item['item_id']} исчез после генерации.")
    return updated


async def ensure_banner_file(
    bot: Any,
    item: dict[str, Any],
    *,
    priority: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    try:
        return await _render_and_backup(bot, item, priority=priority, force=force)
    except Exception:
        logging.exception("Не удалось подготовить баннер item_id=%s.", item.get("item_id"))
        raise


async def ensure_market_banner_file(
    bot: Any,
    item: dict[str, Any],
    *,
    priority: int = 1,
) -> dict[str, Any]:
    file_id = item.get("market_file_id")
    if isinstance(file_id, str) and file_id:
        return item
    item_id = int(item["item_id"])
    buffer = await _render_queue.render(
        priority,
        user_id=int(item["user_id"]),
        name="",
        tag=None,
        level=int(item["level"]),
        item_seed=str(item["seed"]),
        preset=item.get("preset"),
        avatar=None,
        salt=BANNER_SALT,
        show_name=False,
    )
    caption = (
        f"🛍 Баннер для магазина item_id={item_id}\n"
        f"Уровень: {int(item['level'])}"
    )
    if int(item["level"]) == 6:
        sent = await bot.send_animation(
            chat_id=BACKUP_CHANNEL_ID,
            animation=buffer,
            caption=caption,
            disable_notification=True,
        )
        if sent.animation is None:
            raise RuntimeError("Telegram принял GIF магазина без данных animation.")
        new_file_id = sent.animation.file_id
    else:
        sent = await bot.send_photo(
            chat_id=BACKUP_CHANNEL_ID,
            photo=buffer,
            caption=caption,
            disable_notification=True,
        )
        if not sent.photo:
            raise RuntimeError("Telegram принял изображение магазина без данных photo.")
        new_file_id = sent.photo[-1].file_id
    await database.set_market_banner_rendering(
        item_id,
        file_id=new_file_id,
        backup_msg_id=sent.message_id,
    )
    updated = await database.get_banner_item(item_id)
    if updated is None:
        raise RuntimeError(f"Баннер item_id={item_id} исчез после генерации превью.")
    return updated


def make_shame_banner(name: str) -> io.BytesIO:
    image = Image.new("RGB", (800, 420), (118, 118, 118))
    draw = ImageDraw.Draw(image)
    title_font = font("b", 33)
    name_font = font("b", 48)
    title = "ПОМАТРОСИЛ И БРОСИЛ"
    clean_name = clean_text(name, 30) or "Без имени"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    name_box = draw.textbbox((0, 0), clean_name, font=name_font)
    draw.text(((800 - title_box[2]) / 2, 150), title, fill=(235, 235, 235), font=title_font)
    draw.text(((800 - name_box[2]) / 2, 220), clean_name, fill=(245, 245, 245), font=name_font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    buffer.name = "shame.png"
    return buffer


async def get_profile_banner(
    bot: Any,
    user_id: int,
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[str | io.BytesIO, bool] | None:
    profile = profile or await database.get_global_profile(user_id)
    if profile is None:
        return None
    balance = await database.get_duel_balance(
        user_id,
        str(profile.get("username")) if profile.get("username") else None,
    )
    if balance < 0:
        name = str(profile.get("callsign") or profile.get("display_name") or "Без имени")
        return make_shame_banner(name), False
    item = await database.get_active_banner_item(user_id)
    if item is None:
        return None
    item = await ensure_banner_file(bot, item, priority=0)
    file_id = item.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError(f"У активного баннера item_id={item['item_id']} нет Telegram file_id.")
    return file_id, int(item["level"]) == 6


async def send_banner(
    bot: Any,
    chat_id: int,
    media: str | io.BytesIO,
    *,
    animated: bool,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Any:
    if animated:
        return await bot.send_animation(
            chat_id=chat_id,
            animation=media,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    return await bot.send_photo(
        chat_id=chat_id,
        photo=media,
        caption=caption,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


async def stop_banner_render_queue() -> None:
    await _render_queue.stop()
