"""Queued banner rendering and durable Telegram-channel storage."""

import asyncio
import itertools
import io
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw
from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

import database
from config import BACKUP_CHANNEL_ID, BANNER_SALT
from yubari_banner import clean_text, content_hash, make_banner, make_digest, pick_preset
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


async def render_banner_preview(
    *,
    priority: int = 0,
    **arguments: Any,
) -> io.BytesIO:
    return await _render_queue.render(priority, **arguments)


async def _recover_banner_from_backup(
    bot: Any,
    backup_msg_id: int,
    *,
    animated: bool,
    label: str,
) -> tuple[str, int] | None:
    if BACKUP_CHANNEL_ID is None:
        return None
    try:
        backup_copy = await bot.forward_message(
            chat_id=BACKUP_CHANNEL_ID,
            from_chat_id=BACKUP_CHANNEL_ID,
            message_id=backup_msg_id,
            disable_notification=True,
        )
    except TelegramError:
        logging.warning(
            "Не удалось восстановить %s из сообщения бэкапа message_id=%s; "
            "будет выполнена повторная генерация.",
            label,
            backup_msg_id,
            exc_info=True,
        )
        return None

    if animated:
        media = backup_copy.animation
        file_id = media.file_id if media is not None else None
    else:
        file_id = backup_copy.photo[-1].file_id if backup_copy.photo else None
    if not file_id:
        try:
            await bot.delete_message(BACKUP_CHANNEL_ID, backup_copy.message_id)
        except TelegramError:
            logging.warning(
                "Не удалось удалить некорректную копию бэкапа message_id=%s.",
                backup_copy.message_id,
                exc_info=True,
            )
        logging.warning(
            "В сообщении бэкапа message_id=%s нет ожидаемого изображения для %s; "
            "будет выполнена повторная генерация.",
            backup_msg_id,
            label,
        )
        return None
    return str(file_id), int(backup_copy.message_id)


async def _delete_replaced_backup(bot: Any, backup_msg_id: int | None, label: str) -> None:
    if not backup_msg_id or BACKUP_CHANNEL_ID is None:
        return
    try:
        await bot.delete_message(BACKUP_CHANNEL_ID, int(backup_msg_id))
    except (TelegramError, ValueError):
        logging.exception("Не удалось удалить предыдущий бэкап %s.", label)


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
    replace_design: bool = False,
) -> dict[str, Any]:
    user_id = int(item["user_id"])
    design_user_id = int(item.get("design_user_id") or user_id)
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
            avatar, avatar_id = await _avatar_for_user(bot, design_user_id)
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
    old_backup_id = item.get("backup_msg_id")
    if (
        not file_id
        and old_backup_id
        and current_hash == digest
    ):
        recovered = await _recover_banner_from_backup(
            bot,
            int(old_backup_id),
            animated=int(item["level"]) == 6,
            label=f"баннер item_id={item['item_id']}",
        )
        if recovered is not None:
            recovered_file_id, recovered_backup_id = recovered
            await database.set_banner_item_rendering(
                int(item["item_id"]),
                file_id=recovered_file_id,
                backup_msg_id=recovered_backup_id,
                content_hash=digest,
                design_user_id=design_user_id,
            )
            await _delete_replaced_backup(
                bot,
                int(old_backup_id),
                f"баннера item_id={item['item_id']}",
            )
            updated = await database.get_banner_item(int(item["item_id"]))
            if updated is None:
                raise RuntimeError(
                    f"Баннер item_id={item['item_id']} исчез после восстановления из бэкапа."
                )
            return updated
    rendered_at = float(item.get("rendered_at") or 0)
    if (
        not force
        and file_id
        and bool(item.get("is_active"))
        and time.time() - rendered_at < _ACTIVE_REFRESH_SECONDS
    ):
        return item
    if BACKUP_CHANNEL_ID is None:
        raise RuntimeError(
            "Для создания или обновления баннера настройте BACKUP_CHANNEL_ID."
        )

    buffer = await _render_queue.render(
        priority,
        user_id=design_user_id,
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

    try:
        await database.set_banner_item_rendering(
            int(item["item_id"]),
            file_id=new_file_id,
            backup_msg_id=sent.message_id,
            content_hash=digest,
            design_user_id=design_user_id,
            replace_design=replace_design,
            owner_user_id=user_id if replace_design else None,
            seed=str(item["seed"]) if replace_design else None,
            preset=item.get("preset") if replace_design else None,
        )
    except Exception:
        await _delete_replaced_backup(
            bot,
            sent.message_id,
            f"неприменённого рендера баннера item_id={item['item_id']}",
        )
        raise
    await _delete_replaced_backup(
        bot,
        int(old_backup_id) if old_backup_id else None,
        f"баннера item_id={item['item_id']}",
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


async def reroll_active_banner(
    bot: Any,
    user_id: int,
) -> tuple[str, dict[str, Any] | None]:
    item = await database.get_active_banner_item(user_id)
    if item is None:
        return "no_active", None
    status = await database.get_banner_reroll_status(user_id, int(item["item_id"]))
    if status != "ok":
        return status, None
    if BACKUP_CHANNEL_ID is None:
        raise RuntimeError("Для переделки баннера настройте BACKUP_CHANNEL_ID.")

    seed = secrets.token_hex(16)
    preset = (
        pick_preset(make_digest(int(item.get("design_user_id") or user_id), item_seed=seed))
        if int(item["level"]) == 6
        else None
    )
    rerolled_item = {
        **item,
        "seed": seed,
        "preset": preset,
        "file_id": None,
        "content_hash": None,
    }
    updated = await _render_and_backup(
        bot,
        rerolled_item,
        priority=0,
        force=True,
        replace_design=True,
    )
    return "rerolled", updated


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
    old_backup_id = item.get("market_backup_msg_id")
    if old_backup_id:
        recovered = await _recover_banner_from_backup(
            bot,
            int(old_backup_id),
            animated=int(item["level"]) == 6,
            label=f"превью лота item_id={item_id}",
        )
        if recovered is not None:
            recovered_file_id, recovered_backup_id = recovered
            await database.set_market_banner_rendering(
                item_id,
                file_id=recovered_file_id,
                backup_msg_id=recovered_backup_id,
            )
            await _delete_replaced_backup(
                bot,
                int(old_backup_id),
                f"превью лота item_id={item_id}",
            )
            updated = await database.get_banner_item(item_id)
            if updated is None:
                raise RuntimeError(
                    f"Баннер item_id={item_id} исчез после восстановления превью."
                )
            return updated
    if BACKUP_CHANNEL_ID is None:
        raise RuntimeError(
            "Для создания превью баннера настройте BACKUP_CHANNEL_ID."
        )
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
    await _delete_replaced_backup(
        bot,
        int(old_backup_id) if old_backup_id else None,
        f"превью лота item_id={item_id}",
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


async def get_group_owner_banner(
    bot: Any,
    group_id: int,
    owner_user_id: int,
) -> tuple[str, bool] | None:
    item = await database.get_group_owner_banner(group_id, owner_user_id)
    if item is None:
        return None
    profile = await database.get_global_profile(owner_user_id)
    if profile is None:
        raise RuntimeError(
            f"Не найден профиль владельца баннера user_id={owner_user_id}."
        )
    name = clean_text(
        str(
            profile.get("callsign")
            or profile.get("display_name")
            or profile.get("username")
            or "Без имени"
        )
    ) or "Без имени"
    avatar: bytes | None = None
    avatar_id: str | None = None
    try:
        avatar, avatar_id = await _avatar_for_user(bot, owner_user_id)
    except Exception:
        logging.warning(
            "Не удалось получить аватар для группового баннера group_id=%s user_id=%s.",
            group_id,
            owner_user_id,
            exc_info=True,
        )
    digest = content_hash(name, None, avatar_id)
    file_id = str(item.get("file_id") or "")
    if file_id and item.get("content_hash") == digest:
        return file_id, True
    old_backup_id = item.get("backup_msg_id")
    if not file_id and old_backup_id and item.get("content_hash") == digest:
        recovered = await _recover_banner_from_backup(
            bot,
            int(old_backup_id),
            animated=True,
            label=f"групповой баннер group_id={group_id}",
        )
        if recovered is not None:
            recovered_file_id, recovered_backup_id = recovered
            saved = await database.set_group_owner_banner_rendering(
                group_id,
                owner_user_id,
                file_id=recovered_file_id,
                content_hash=digest,
                backup_msg_id=recovered_backup_id,
            )
            if not saved:
                raise RuntimeError(
                    f"Баннер владельца group_id={group_id} изменился во время восстановления."
                )
            await _delete_replaced_backup(
                bot,
                int(old_backup_id),
                f"группового баннера group_id={group_id}",
            )
            return recovered_file_id, True

    if BACKUP_CHANNEL_ID is None:
        raise RuntimeError(
            "Для создания группового баннера настройте BACKUP_CHANNEL_ID."
        )
    buffer = await _render_queue.render(
        1,
        user_id=owner_user_id,
        name=name,
        tag=None,
        level=6,
        item_seed=str(item["seed"]),
        preset=item.get("preset"),
        avatar=avatar,
        salt=BANNER_SALT,
    )
    sent = await bot.send_animation(
        chat_id=BACKUP_CHANNEL_ID,
        animation=buffer,
        caption=f"🖼 Групповой баннер владельца\nГруппа: {group_id}\nВладелец: {owner_user_id}",
        disable_notification=True,
    )
    if sent.animation is None:
        raise RuntimeError("Telegram принял баннер владельца без данных animation.")
    saved = await database.set_group_owner_banner_rendering(
        group_id,
        owner_user_id,
        file_id=sent.animation.file_id,
        content_hash=digest,
        backup_msg_id=sent.message_id,
    )
    if not saved:
        raise RuntimeError(
            f"Баннер владельца group_id={group_id} изменился во время рендера."
        )
    await _delete_replaced_backup(
        bot,
        int(old_backup_id) if old_backup_id else None,
        f"группового баннера group_id={group_id}",
    )
    return sent.animation.file_id, True


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
