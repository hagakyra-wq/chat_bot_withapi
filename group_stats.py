"""Group-scoped activity and coin leaderboards with profile-photo banners."""

import asyncio
import io
import logging
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import database
from yubari_banner.bn_common import clean_text, put_text, text_length

_PERIOD_LABELS = {
    "d": ("ДЕНЬ", "за день"),
    "w": ("НЕДЕЛЯ", "за неделю"),
    "m": ("МЕСЯЦ", "за месяц"),
    "a": ("ВСЁ ВРЕМЯ", "за всё время"),
}
_CROWN_COLORS = (
    (255, 199, 53),
    (240, 75, 86),
    (255, 206, 226),
)
_FALLBACK_COLORS = (
    (126, 108, 219),
    (52, 166, 177),
    (212, 132, 77),
    (193, 93, 143),
    (96, 147, 204),
    (158, 166, 83),
    (194, 116, 190),
    (88, 170, 126),
    (212, 164, 82),
)
_PERIOD_CALLBACK_RE = re.compile(r"^gstats:([ac]):(-?\d+):([dwma])$")
_MODE_CALLBACK_RE = re.compile(r"^gstats:([ac]):(-?\d+)$")


def leaderboard_keyboard(
    chat_id: int,
    *,
    mode: str,
    period: str = "d",
) -> InlineKeyboardMarkup:
    if mode == "a":
        period_buttons = [
            InlineKeyboardButton(
                f"{'● ' if key == period else ''}{label}",
                callback_data=f"gstats:a:{chat_id}:{key}",
            )
            for key, label in (
                ("d", "День"),
                ("w", "Неделя"),
                ("m", "Месяц"),
                ("a", "Всё время"),
            )
        ]
        return InlineKeyboardMarkup(
            [
                period_buttons,
                [
                    InlineKeyboardButton(
                        "💰 Топ монет",
                        callback_data=f"gstats:c:{chat_id}",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "📊 Топ активности",
                callback_data=f"gstats:a:{chat_id}:d",
            )
        ]]
    )


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    bundled_font = Path(__file__).resolve().parent / "yubari_banner" / "fonts"
    paths = (
        (
            str(bundled_font / "DejaVuSans-Bold.ttf"),
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        )
        if bold
        else (
            str(bundled_font / "DejaVuSans.ttf"),
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_fitted_text(
    image: Image.Image,
    text: str,
    *,
    center_x: int,
    y: int,
    max_width: float,
    size: int,
    fill: tuple[int, int, int, int],
    bold: bool = False,
) -> None:
    text = clean_text(text, 256)
    if not text:
        return
    current_size = size
    selected_font = _font(current_size, bold=bold)
    while text_length(text, selected_font) > max_width and current_size > 11:
        current_size -= 1
        selected_font = _font(current_size, bold=bold)
    if text_length(text, selected_font) > max_width:
        low, high = 1, len(text)
        best = "…"
        while low <= high:
            limit = (low + high) // 2
            candidate = clean_text(text, limit)
            if text_length(candidate, selected_font) <= max_width:
                best = candidate
                low = limit + 1
            else:
                high = limit - 1
        text = best
    width = text_length(text, selected_font)
    put_text(image, (center_x - width / 2, y), text, selected_font, fill)


def _avatar_color(avatar: Image.Image | None, index: int) -> tuple[int, int, int]:
    if avatar is None:
        return _FALLBACK_COLORS[index % len(_FALLBACK_COLORS)]
    average = ImageStat.Stat(avatar.resize((1, 1))).mean[:3]
    return (
        int(max(70, min(235, average[0]))),
        int(max(70, min(235, average[1]))),
        int(max(70, min(235, average[2]))),
    )


def _lighten(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(int(channel + (255 - channel) * amount) for channel in color)


def _format_coin_value(value: int) -> str:
    sign = "-" if value < 0 else ""
    amount = abs(value)
    return f"{sign}{amount // 100}.{amount % 100:02d} монет"


def _draw_crown(draw: ImageDraw.ImageDraw, center_x: int, top: int, color: tuple[int, int, int]) -> None:
    points = [
        (center_x - 25, top + 18),
        (center_x - 31, top),
        (center_x - 13, top + 10),
        (center_x, top - 3),
        (center_x + 13, top + 10),
        (center_x + 31, top),
        (center_x + 25, top + 18),
    ]
    draw.polygon(points, fill=color, outline=(255, 247, 218))
    draw.rounded_rectangle(
        (center_x - 25, top + 17, center_x + 25, top + 25),
        radius=3,
        fill=color,
        outline=(255, 247, 218),
        width=2,
    )


def _prepare_avatar(avatar: Image.Image | None, size: int) -> Image.Image:
    if avatar is None:
        placeholder = Image.new("RGB", (size, size), (83, 75, 115))
        draw = ImageDraw.Draw(placeholder)
        draw.ellipse((size * 0.25, size * 0.14, size * 0.75, size * 0.64), fill=(226, 213, 236))
        draw.rounded_rectangle(
            (size * 0.13, size * 0.60, size * 0.87, size * 1.2),
            radius=size * 0.26,
            fill=(226, 213, 236),
        )
        avatar = placeholder
    return ImageOps.fit(avatar.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS)


def _make_banner(
    entries: list[dict[str, Any]],
    avatars: list[Image.Image | None],
    *,
    mode: str,
    period: str,
    group_title: str,
) -> io.BytesIO:
    width, height = 1200, 720
    coin_mode = mode == "c"
    base_top = (22, 18, 43) if coin_mode else (18, 27, 48)
    base_bottom = (57, 34, 73) if coin_mode else (28, 66, 77)
    accent = (239, 190, 89) if coin_mode else (107, 222, 190)
    gradient = Image.new("RGB", (1, height))
    gradient.putdata(
        [
            tuple(
                int(top * (1 - y / (height - 1)) + bottom * y / (height - 1))
                for top, bottom in zip(base_top, base_bottom)
            )
            for y in range(height)
        ]
    )
    image = gradient.resize((width, height)).convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    for index in range(65):
        x = (index * 197 + 61) % width
        y = (index * 113 + 31) % height
        radius = 2 + index % 5
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(*accent, 45 + index % 85),
        )
    if coin_mode:
        for index in range(8):
            x = 70 + index * 157
            draw.ellipse(
                (x, 651, x + 42, 663),
                fill=(226, 173, 63, 170),
                outline=(255, 220, 126, 210),
                width=2,
            )
    else:
        for x in (48, 1152):
            draw.arc((x - 80, 90, x + 80, 250), 190, 350, fill=(*accent, 100), width=4)

    title_font = _font(44, bold=True)
    subtitle_font = _font(23)
    mode_title = "КОРОЛЕВСКАЯ СОКРОВИЩНИЦА" if coin_mode else "АКТИВНОСТЬ ПРИДВОРНЫХ"
    draw.text((width // 2, 34), mode_title, anchor="mt", font=title_font, fill=(255, 239, 202, 255) if coin_mode else (235, 255, 246, 255))
    period_text = "БАЛАНСЫ · МОНЕТЫ" if coin_mode else _PERIOD_LABELS.get(period, _PERIOD_LABELS["d"])[0]
    title = re.sub(r"\s+", " ", group_title).strip()[:48] or "ГРУППА"
    _draw_fitted_text(
        image,
        f"{title} · {period_text}",
        center_x=width // 2,
        y=91,
        max_width=width - 80,
        size=23,
        fill=(221, 213, 237, 255),
    )
    if not entries:
        draw.text(
            (width // 2, 360),
            "Пока нет данных за этот период",
            anchor="mm",
            font=subtitle_font,
            fill=(235, 228, 245, 255),
        )
    else:
        count = len(entries)
        max_offset = math.ceil((count - 1) / 2)
        spacing = (width // 2 - 85) / max(max_offset, 1)
        raw_values = [int(entry.get("value", 0)) for entry in entries]
        values = [max(0, value) for value in raw_values]
        maximum = max(values, default=0)
        baseline = 578
        bar_width = max(54, min(92, int(spacing * 0.62)))
        avatar_size = max(64, min(104, int(spacing * 0.70)))

        for rank, entry in enumerate(entries):
            offset = 0 if rank == 0 else (rank + 1) // 2 * (-1 if rank % 2 else 1)
            x = int(width // 2 + offset * spacing)
            value = values[rank]
            if maximum:
                relative = math.sqrt(value / maximum)
            else:
                relative = 0.15
            height_scale = max(0.16, 1.0 - rank / max(count, 1) * 0.38)
            bar_height = int((120 + 205 * relative) * height_scale)
            top = baseline - bar_height
            avatar = avatars[rank] if rank < len(avatars) else None
            color = _avatar_color(avatar, rank)
            x0 = x - bar_width // 2
            draw.rounded_rectangle(
                (x0, top, x0 + bar_width, baseline),
                radius=12,
                fill=(*color, 205),
                outline=(*_lighten(color, 0.35), 255),
                width=3,
            )
            for stripe in range(1, 4):
                y = top + stripe * bar_height // 4
                draw.line((x0 + 8, y, x0 + bar_width - 8, y), fill=(255, 239, 208, 60), width=2)

            avatar_image = _prepare_avatar(avatar, avatar_size)
            avatar_x = x - avatar_size // 2
            avatar_y = max(150, top - avatar_size - 10)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
            image.paste(avatar_image, (avatar_x, avatar_y), mask)
            draw.ellipse(
                (avatar_x - 4, avatar_y - 4, avatar_x + avatar_size + 3, avatar_y + avatar_size + 3),
                outline=(*_lighten(color, 0.55), 255),
                width=5,
            )
            if rank < 3:
                _draw_crown(draw, x, avatar_y - 18, _CROWN_COLORS[rank])
            medal_color = _CROWN_COLORS[rank] if rank < 3 else (240, 239, 248)
            draw.text(
                (x, top + 18),
                f"#{rank + 1}",
                anchor="mt",
                font=_font(19, bold=True),
                fill=(*medal_color, 255),
            )
            name = re.sub(
                r"\s+",
                " ",
                str(entry.get("display_name") or "Участник"),
            ).strip()
            _draw_fitted_text(
                image,
                name or "Участник",
                center_x=x,
                y=baseline + 12,
                max_width=spacing * 0.88,
                size=18,
                fill=(248, 243, 255, 255),
                bold=True,
            )
            value_text = (
                _format_coin_value(raw_values[rank]).removesuffix(" монет")
                if coin_mode
                else f"{value:,}".replace(",", " ")
            )
            _draw_fitted_text(
                image,
                value_text,
                center_x=x,
                y=baseline + 37,
                max_width=spacing * 0.88,
                size=19,
                fill=(*accent, 255),
                bold=True,
            )

    footer = "Юбара хранит счёт каждого двора отдельно" if coin_mode else "Каждое сообщение добавляет высоты башне"
    draw.text(
        (width // 2, 693 if coin_mode else 682),
        footer,
        anchor="mm",
        font=subtitle_font,
        fill=(231, 220, 242, 240),
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.name = "group_treasury.png" if coin_mode else "group_activity.png"
    output.seek(0)
    return output


async def render_leaderboard_banner(
    bot: Any,
    entries: list[dict[str, Any]],
    *,
    mode: str,
    period: str = "d",
    group_title: str = "",
) -> io.BytesIO:
    semaphore = asyncio.Semaphore(4)

    async def load_avatar(entry: dict[str, Any]) -> Image.Image | None:
        user_id = int(entry["user_id"])
        async with semaphore:
            try:
                photos = await bot.get_user_profile_photos(user_id, limit=1)
                if not photos.photos or not photos.photos[0]:
                    return None
                photo = photos.photos[0][-1]
                telegram_file = await bot.get_file(photo.file_id)
                image_bytes = await telegram_file.download_as_bytearray()
                with Image.open(io.BytesIO(image_bytes)) as source:
                    return source.convert("RGB").copy()
            except (TelegramError, OSError, ValueError):
                logging.warning(
                    "Не удалось загрузить аватар участника рейтинга user_id=%s.",
                    user_id,
                    exc_info=True,
                )
                return None

    avatars = await asyncio.gather(*(load_avatar(entry) for entry in entries[:9]))
    return await asyncio.to_thread(
        _make_banner,
        entries[:9],
        avatars,
        mode=mode,
        period=period,
        group_title=group_title,
    )


async def _get_group_entries(
    group_id: int,
    *,
    mode: str,
    period: str,
) -> list[dict[str, Any]]:
    if mode == "a":
        records = await database.get_group_message_leaderboard(group_id, period, limit=9)
        return [
            {**record, "value": int(record["message_count"])}
            for record in records
        ]
    records = await database.get_group_coin_leaderboard(group_id, limit=9)
    return [
        {**record, "value": int(record["balance_cents"])}
        for record in records
    ]


def leaderboard_caption(
    entries: list[dict[str, Any]],
    *,
    mode: str,
    period: str,
) -> str:
    if mode == "a":
        title = f"📊 Активность группы · {_PERIOD_LABELS[period][1]}"
        metric = lambda value: f"{value:,}".replace(",", " ") + " сообщений"
    else:
        title = "💰 Топ монет этой группы"
        metric = _format_coin_value
    lines = [title]
    if not entries:
        lines.append("За выбранный период пока нет участников в рейтинге.")
    else:
        medals = ("🥇", "🥈", "🥉")
        for rank, entry in enumerate(entries[:9], start=1):
            name = re.sub(
                r"\s+",
                " ",
                str(entry.get("display_name") or "Участник"),
            ).strip()[:70]
            lines.append(
                f"{medals[rank - 1] if rank <= 3 else f'{rank}.'} "
                f"{name or 'Участник'} — "
                f"{metric(int(entry['value']))}"
            )
    return "\n".join(lines)[:1000]


async def on_group_stats_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    match = _PERIOD_CALLBACK_RE.fullmatch(data) or _MODE_CALLBACK_RE.fullmatch(data)
    if match is None:
        await query.answer("Эта кнопка устарела.", show_alert=True)
        return
    mode, raw_group_id = match.group(1), match.group(2)
    period = match.group(3) if len(match.groups()) == 3 else "d"
    group_id = int(raw_group_id)
    message = query.message
    if (
        message is None
        or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
        or message.chat_id != group_id
        or not await database.is_group_allowed(group_id)
    ):
        await query.answer("Эта статистика больше недоступна.", show_alert=True)
        return
    await query.answer()
    try:
        entries = await _get_group_entries(group_id, mode=mode, period=period)
        banner = await render_leaderboard_banner(
            context.bot,
            entries,
            mode=mode,
            period=period,
            group_title=message.chat.title or "",
        )
        media = InputMediaPhoto(
            media=banner,
            caption=leaderboard_caption(entries, mode=mode, period=period),
        )
        await message.edit_media(
            media=media,
            reply_markup=leaderboard_keyboard(group_id, mode=mode, period=period),
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).casefold():
            return
        logging.exception(
            "Не удалось обновить баннер статистики group_id=%s mode=%s period=%s.",
            group_id,
            mode,
            period,
        )
    except Exception:
        logging.exception(
            "Не удалось построить статистику group_id=%s mode=%s period=%s.",
            group_id,
            mode,
            period,
        )
        await query.message.reply_text("Не удалось обновить статистику. Попробуй позже.")
