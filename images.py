"""Независимая асинхронная генерация изображений для личных и групповых чатов."""

import asyncio
import base64
import io
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from config import AI_HORDE_API_KEY, BACKUP_CHANNEL_ID

POLLINATIONS_URL = "https://image.pollinations.ai/prompt"
AI_HORDE_URL = "https://aihorde.net/api/v2"
CLIENT_AGENT = "YubaraTelegramBot:1.0.0:github.com/hagakyra-wq/chat_bot_withapi"
POLLINATIONS_COOLDOWN_SECONDS = 120
MAX_IMAGE_BYTES = 9 * 1024 * 1024
PROMPT_MAX_LENGTH = 1500
STATUS_REFRESH_SECONDS = 5
HORDE_REQUEUE_MIN_AGE_SECONDS = 5 * 60
ANIME_PROMPT_PREFIX = "anime style, 2D anime illustration, anime art, "
DEFAULT_NEGATIVE_PROMPT = "3d, photorealistic, realistic, 3d render, bad anatomy, bad hands"
MODEL_PREFERENCES = {
    "auto": "Автовыбор (Animagine → anime → anything)",
    "animagine": "Animagine",
    "anime": "Любая anime-модель",
    "anything": "Anything",
}
POLLINATIONS_MODELS = {
    "flux": "Flux",
    "turbo": "Turbo",
}
IMAGE_SIZES = {
    "1024x576": (1024, 576),
    "576x1024": (576, 1024),
    "1024x1024": (1024, 1024),
    "768x768": (768, 768),
}

_IMAGE_COMMAND_RE = re.compile(r"^/(?:image|img)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_NATURAL_REQUEST_RE = re.compile(
    r"\b(?:нарисуй|изобрази|сгенерируй\s+(?:мне\s+)?(?:картинку|изображение|арт)?|"
    r"создай\s+(?:мне\s+)?(?:картинку|изображение|арт)|"
    r"сделай\s+(?:мне\s+)?(?:картинку|изображение|арт)|"
    r"draw|generate\s+(?:an?\s+)?image)\b",
    re.IGNORECASE,
)


@dataclass
class PendingImage:
    prompt: str
    owner_id: int
    chat_id: int


@dataclass
class PendingSettingInput:
    request_id: str
    setting: str


@dataclass
class ImageSettings:
    model_preference: str = "auto"
    pollinations_model: str = "flux"
    width: int = 768
    height: int = 1024
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    seed: str | None = None

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "ImageSettings":
        return cls(
            model_preference=str(record["model_preference"]),
            pollinations_model=str(record["pollinations_model"]),
            width=int(record["width"]),
            height=int(record["height"]),
            negative_prompt=str(record["negative_prompt"]),
            seed=str(record["seed"]) if record.get("seed") is not None else None,
        )


@dataclass
class ImageJob:
    job_id: str
    mode: str
    prompt: str
    owner_id: int
    chat_id: int
    status_message_id: int
    bot: object
    settings: ImageSettings = field(default_factory=ImageSettings)
    seed: str = field(default_factory=lambda: str(random.randint(1, 99_999_999)))
    started_at: float = field(default_factory=time.monotonic)
    remote_id: str | None = None
    task: asyncio.Task[object] | None = None
    status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HordeRequestExpired(Exception):
    """Задание AI Horde удалено до завершения генерации."""


def extract_image_request(text: str) -> tuple[bool, str]:
    """Возвращает (это запрос на рисунок, очищенное описание)."""
    command = _IMAGE_COMMAND_RE.match(text.strip())
    if command:
        return True, (command.group(1) or "").strip()
    match = _NATURAL_REQUEST_RE.search(text)
    if match is None:
        return False, ""
    prompt = text[match.end() :].strip(" \t\r\n,.:;!?—-")
    prompt = re.sub(r"^(?:мне|пожалуйста)\s+", "", prompt, flags=re.IGNORECASE)
    return True, prompt


def is_image_command(text: str) -> bool:
    return _IMAGE_COMMAND_RE.match(text.strip()) is not None


def _mode_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙️ Настройки изображения", callback_data=f"img:settings:{request_id}")],
            [
                InlineKeyboardButton(
                    "⚡ Быстро и некачественно",
                    callback_data=f"img:fast:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎨 Долго и качественно",
                    callback_data=f"img:horde:{request_id}",
                )
            ],
        ]
    )


def _settings_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎨 Модель AI Horde", callback_data=f"img:models:{request_id}")],
            [InlineKeyboardButton("⚡ Модель Pollinations", callback_data=f"img:pollinations:{request_id}")],
            [InlineKeyboardButton("📐 Размер", callback_data=f"img:sizes:{request_id}")],
            [InlineKeyboardButton("🚫 Негативный промпт", callback_data=f"img:input:{request_id}:negative_prompt")],
            [InlineKeyboardButton("🎲 Seed", callback_data=f"img:input:{request_id}:seed")],
            [InlineKeyboardButton("↩️ К выбору генератора", callback_data=f"img:back:{request_id}")],
        ]
    )


def _model_keyboard(request_id: str, current: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if key == current else ''}{label}",
                callback_data=f"img:set:{request_id}:model:{key}",
            )
        ]
        for key, label in MODEL_PREFERENCES.items()
    ]
    rows.append([InlineKeyboardButton("↩️ К параметрам", callback_data=f"img:settings:{request_id}")])
    return InlineKeyboardMarkup(rows)


def _pollinations_keyboard(request_id: str, current: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if key == current else ''}{label}",
                callback_data=f"img:set:{request_id}:pollinations_model:{key}",
            )
        ]
        for key, label in POLLINATIONS_MODELS.items()
    ]
    rows.append([InlineKeyboardButton("↩️ К параметрам", callback_data=f"img:settings:{request_id}")])
    return InlineKeyboardMarkup(rows)


def _size_keyboard(request_id: str, current: tuple[int, int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if size == current else ''}{label}",
                callback_data=f"img:set:{request_id}:size:{label}",
            )
        ]
        for label, size in IMAGE_SIZES.items()
    ]
    rows.append([InlineKeyboardButton("↩️ К параметрам", callback_data=f"img:settings:{request_id}")])
    return InlineKeyboardMarkup(rows)


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Отменить генерацию", callback_data=f"img:cancel:{job_id}")]]
    )


class ImageGenerationManager:
    def __init__(self) -> None:
        self.pending: dict[str, PendingImage] = {}
        self.pending_setting_inputs: dict[tuple[int, int], PendingSettingInput] = {}
        self.jobs: dict[tuple[int, int], ImageJob] = {}
        self._generation_lock = asyncio.Lock()
        self._fast_lock = asyncio.Lock()
        self._last_fast_start: float | None = None

    async def offer(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        prompt = prompt.strip()
        if not prompt:
            await message.reply_text("Опиши, что именно мне изобразить: /image описание.")
            return
        if len(prompt) > PROMPT_MAX_LENGTH:
            await message.reply_text(
                f"Описание слишком длинное. Сократи его до {PROMPT_MAX_LENGTH} символов."
            )
            return

        request_id = uuid.uuid4().hex
        self.pending[request_id] = PendingImage(
            prompt=prompt,
            owner_id=user.id,
            chat_id=message.chat_id,
        )
        try:
            await message.reply_text(
                "Выбирай, каким способом я милостиво создам твою картину:",
                reply_markup=_mode_keyboard(request_id),
            )
        except TelegramError:
            self.pending.pop(request_id, None)
            logging.exception("Не удалось показать кнопки выбора генерации изображения.")
            return
        await self.refresh_status(message.chat_id, context.bot, owner_id=user.id)

    async def handle_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        action = query.data or ""
        if action.startswith("img:settings:"):
            await self._show_settings(update, action.rsplit(":", 1)[-1])
            return
        if action.startswith("img:models:"):
            await self._show_models(update, action.rsplit(":", 1)[-1])
            return
        if action.startswith("img:pollinations:"):
            await self._show_pollinations_models(update, action.rsplit(":", 1)[-1])
            return
        if action.startswith("img:sizes:"):
            await self._show_sizes(update, action.rsplit(":", 1)[-1])
            return
        if action.startswith("img:input:"):
            await self._request_setting_input(update, action)
            return
        if action.startswith("img:set:"):
            await self._save_setting_from_callback(update, action)
            return
        if action.startswith("img:back:"):
            await self._return_to_generator_menu(update, action.rsplit(":", 1)[-1])
            return
        if action.startswith("img:cancel:"):
            await self._cancel_from_callback(update)
            return

        parts = action.split(":")
        if len(parts) != 3 or parts[1] not in ("fast", "horde"):
            await query.answer("Кнопка больше не действует.", show_alert=True)
            return
        request_id = parts[2]
        pending = self.pending.get(request_id)
        if pending is None:
            await query.answer("Этот запрос уже обработан или устарел.", show_alert=True)
            return
        if pending.owner_id != user.id:
            await query.answer("Эту генерацию может запустить только её автор.", show_alert=True)
            return

        mode = parts[1]
        if mode == "fast":
            remaining = await self._reserve_fast_start()
            if remaining > 0:
                await query.answer(
                    f"Общий перерыв Pollinations: подожди ещё {remaining} сек.",
                    show_alert=True,
                )
                return

        self.pending.pop(request_id, None)
        await query.answer("Запускаю генерацию отдельно от нашего разговора.")

        job_key = (pending.chat_id, pending.owner_id)
        old_job = self.jobs.get(job_key)
        if old_job and old_job.task and not old_job.task.done():
            old_job.task.cancel()
            try:
                await old_job.task
            except asyncio.CancelledError:
                pass

        query_message = query.message
        if query_message is None:
            return
        try:
            settings = ImageSettings.from_record(
                await database.get_image_settings(pending.chat_id)
            )
        except Exception:
            logging.exception("Не удалось прочитать настройки генерации чата %s.", pending.chat_id)
            await query.answer("Не удалось прочитать настройки изображения.", show_alert=True)
            return
        job_id = uuid.uuid4().hex
        status_text = self._status_text(
            "Pollinations" if mode == "fast" else "AI Horde",
            time.monotonic(),
            "готовлю запрос",
        )
        try:
            status_message = await query_message.edit_text(
                status_text,
                reply_markup=_cancel_keyboard(job_id),
            )
        except TelegramError:
            logging.exception("Не удалось показать статус генерации.")
            return

        job = ImageJob(
            job_id=job_id,
            mode=mode,
            prompt=pending.prompt,
            owner_id=pending.owner_id,
            chat_id=pending.chat_id,
            status_message_id=status_message.message_id,
            bot=context.bot,
            settings=settings,
            seed=settings.seed or str(random.randint(1, 99_999_999)),
        )
        self.jobs[job_key] = job
        job.task = context.application.create_task(
            self._run_job(job, context),
            update=update,
            name=f"image-generation-{job_id}",
        )
        logging.info(
            "Запущена генерация изображения job_id=%s provider=%s chat_id=%s user_id=%s settings=%sx%s model=%s.",
            job_id,
            mode,
            pending.chat_id,
            pending.owner_id,
            settings.width,
            settings.height,
            settings.model_preference,
        )

    def _pending_for_callback(
        self,
        update: Update,
        request_id: str,
    ) -> PendingImage | None:
        pending = self.pending.get(request_id)
        message = update.callback_query.message if update.callback_query else None
        user = update.effective_user
        chat = update.effective_chat
        if (
            pending is None
            or message is None
            or user is None
            or chat is None
            or message.chat_id != pending.chat_id
            or (chat.type == ChatType.PRIVATE and pending.owner_id != user.id)
        ):
            return None
        return pending

    async def _show_settings(self, update: Update, request_id: str) -> None:
        query = update.callback_query
        pending = self._pending_for_callback(update, request_id)
        if query is None:
            return
        if pending is None:
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        try:
            settings = ImageSettings.from_record(
                await database.get_image_settings(pending.chat_id)
            )
        except Exception:
            logging.exception("Не удалось прочитать настройки чата %s.", pending.chat_id)
            await query.answer("Не удалось загрузить настройки.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(
                self._settings_description(settings),
                reply_markup=_settings_keyboard(request_id),
            )
        except TelegramError:
            logging.exception("Не удалось показать меню настроек чата %s.", pending.chat_id)

    async def _show_models(self, update: Update, request_id: str) -> None:
        query = update.callback_query
        pending = self._pending_for_callback(update, request_id)
        if query is None:
            return
        if pending is None:
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        try:
            settings = await database.get_image_settings(pending.chat_id)
            preference = str(settings["model_preference"])
        except Exception:
            logging.exception("Не удалось загрузить предпочтение модели чата %s.", pending.chat_id)
            await query.answer("Не удалось загрузить модели.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(
                "Выбери предпочтение модели. Юбара ищет только аниме-модели; если "
                "подходящих сейчас нет, AI Horde выберет доступную сам.",
                reply_markup=_model_keyboard(request_id, preference),
            )
        except TelegramError:
            logging.exception("Не удалось показать модели для чата %s.", pending.chat_id)

    async def _show_sizes(self, update: Update, request_id: str) -> None:
        query = update.callback_query
        pending = self._pending_for_callback(update, request_id)
        if query is None:
            return
        if pending is None:
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        try:
            settings = await database.get_image_settings(pending.chat_id)
            current = (int(settings["width"]), int(settings["height"]))
        except Exception:
            logging.exception("Не удалось загрузить размер изображения чата %s.", pending.chat_id)
            await query.answer("Не удалось загрузить размеры.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(
                "Выбери размер изображения:",
                reply_markup=_size_keyboard(request_id, current),
            )
        except TelegramError:
            logging.exception("Не удалось показать размеры для чата %s.", pending.chat_id)

    async def _show_pollinations_models(self, update: Update, request_id: str) -> None:
        query = update.callback_query
        pending = self._pending_for_callback(update, request_id)
        if query is None:
            return
        if pending is None:
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        try:
            settings = await database.get_image_settings(pending.chat_id)
            current = str(settings["pollinations_model"])
        except Exception:
            logging.exception("Не удалось загрузить модель Pollinations чата %s.", pending.chat_id)
            await query.answer("Не удалось загрузить настройки.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text(
                "Выбери модель Pollinations:",
                reply_markup=_pollinations_keyboard(request_id, current),
            )
        except TelegramError:
            logging.exception("Не удалось показать модели Pollinations для чата %s.", pending.chat_id)

    async def _request_setting_input(self, update: Update, action: str) -> None:
        query = update.callback_query
        user = update.effective_user
        parts = action.split(":")
        if len(parts) != 4:
            if query is not None:
                await query.answer("Некорректный запрос настройки.", show_alert=True)
            return
        _, _, request_id, setting = parts
        pending = self._pending_for_callback(update, request_id)
        if query is None:
            return
        if (
            pending is None
            or user is None
            or setting not in ("negative_prompt", "seed")
        ):
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        self.pending_setting_inputs[(pending.chat_id, user.id)] = PendingSettingInput(
            request_id=request_id,
            setting=setting,
        )
        label = "негативный промпт (до 500 символов)" if setting == "negative_prompt" else "числовой seed или /random"
        await query.answer()
        await query.edit_message_text(
            f"Отправь {label}. Отправь /clear, чтобы очистить негативный промпт."
            if setting == "negative_prompt"
            else f"Отправь {label}.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data=f"img:settings:{request_id}")]]
            ),
        )

    async def _save_setting_from_callback(self, update: Update, action: str) -> None:
        query = update.callback_query
        if query is None:
            return
        parts = action.split(":")
        if len(parts) != 5:
            await query.answer("Настройка задана некорректно.", show_alert=True)
            return
        _, _, request_id, setting, value = parts
        pending = self._pending_for_callback(update, request_id)
        if pending is None:
            await query.answer("Запрос настроек устарел.", show_alert=True)
            return
        try:
            if setting == "model":
                await database.set_image_setting(pending.chat_id, "model_preference", value)
            elif setting == "pollinations_model":
                if value not in POLLINATIONS_MODELS:
                    raise ValueError("Недопустимая модель Pollinations.")
                await database.set_image_setting(pending.chat_id, "pollinations_model", value)
            elif setting == "size":
                if value not in IMAGE_SIZES:
                    raise ValueError("Недопустимый размер изображения.")
                width, height = (int(part) for part in value.split("x", 1))
                await database.set_image_setting(pending.chat_id, "size", (width, height))
            else:
                await query.answer("Неизвестная настройка.", show_alert=True)
                return
            settings = ImageSettings.from_record(
                await database.get_image_settings(pending.chat_id)
            )
        except Exception:
            logging.exception("Не удалось сохранить настройку изображения в чате %s.", pending.chat_id)
            await query.answer("Не удалось сохранить настройку.", show_alert=True)
            return
        await query.answer("Сохранено для этого чата.")
        try:
            await query.edit_message_text(
                self._settings_description(settings),
                reply_markup=_settings_keyboard(request_id),
            )
        except TelegramError:
            logging.exception("Не удалось обновить меню настроек чата %s.", pending.chat_id)

    async def _return_to_generator_menu(self, update: Update, request_id: str) -> None:
        query = update.callback_query
        if query is None:
            return
        pending = self._pending_for_callback(update, request_id)
        if pending is None:
            await query.answer("Запрос устарел.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            "Выбирай, каким способом я создам твою картину:",
            reply_markup=_mode_keyboard(request_id),
        )

    @staticmethod
    def _settings_description(settings: ImageSettings) -> str:
        model = MODEL_PREFERENCES.get(settings.model_preference, MODEL_PREFERENCES["auto"])
        negative = settings.negative_prompt or "не задан"
        if len(negative) > 140:
            negative = negative[:137] + "..."
        seed = settings.seed or "случайный"
        return (
            "Настройки изображений для этого чата:\n"
            f"AI Horde: {model}\n"
            f"Pollinations: {POLLINATIONS_MODELS.get(settings.pollinations_model, 'Flux')}\n"
            f"Размер: {settings.width}×{settings.height}\n"
            f"Негативный промпт: {negative}\n"
            f"Seed: {seed}"
        )

    async def consume_setting_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        del context
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or message.text is None:
            return False
        key = (message.chat_id, user.id)
        pending_input = self.pending_setting_inputs.pop(key, None)
        if pending_input is None:
            return False
        text = message.text.strip()
        setting = pending_input.setting
        if setting == "negative_prompt":
            if text.casefold() in ("/cancel", "/back"):
                await message.reply_text("Настройка отменена.")
                return True
            value = "" if text.casefold() == "/clear" else text
            if len(value) > 500:
                self.pending_setting_inputs[key] = pending_input
                await message.reply_text("Негативный промпт слишком длинный. Максимум 500 символов.")
                return True
        else:
            if text.casefold() in ("/cancel", "/back"):
                await message.reply_text("Настройка отменена.")
                return True
            if text.casefold() == "/random":
                value = None
            elif text.isdigit() and len(text) <= 10:
                value = text
            else:
                self.pending_setting_inputs[key] = pending_input
                await message.reply_text("Отправь целое число или /random.")
                return True
        try:
            await database.set_image_setting(message.chat_id, setting, value)
            settings = ImageSettings.from_record(
                await database.get_image_settings(message.chat_id)
            )
            await message.reply_text(
                "Настройка сохранена для этого чата.\n"
                + self._settings_description(settings)
            )
        except Exception:
            self.pending_setting_inputs[key] = pending_input
            logging.exception("Не удалось сохранить настройку изображения для чата %s.", message.chat_id)
            await message.reply_text("Не удалось сохранить настройку. Попробуй позже.")
        return True

    async def _reserve_fast_start(self) -> int:
        async with self._fast_lock:
            now = time.monotonic()
            remaining = 0
            if self._last_fast_start is not None:
                remaining = max(
                    0,
                    int(POLLINATIONS_COOLDOWN_SECONDS - (now - self._last_fast_start) + 0.999),
                )
            if remaining == 0:
                self._last_fast_start = now
            return remaining

    async def _cancel_from_callback(self, update: Update) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        job_id = (query.data or "").rsplit(":", 1)[-1]
        job = next(
            (active for active in self.jobs.values() if active.job_id == job_id),
            None,
        )
        if job is None:
            await query.answer("Генерация уже завершена.", show_alert=True)
            return
        if job.owner_id != user.id:
            await query.answer("Отменить эту генерацию может только её автор.", show_alert=True)
            return
        await query.answer("Отменяю генерацию.")
        if job.task and not job.task.done():
            job.task.cancel()

    async def _run_job(
        self,
        job: ImageJob,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        generation_slot_acquired = False
        try:
            while not generation_slot_acquired:
                try:
                    await asyncio.wait_for(
                        self._generation_lock.acquire(),
                        timeout=STATUS_REFRESH_SECONDS,
                    )
                    generation_slot_acquired = True
                except asyncio.TimeoutError:
                    elapsed = max(0, int(time.monotonic() - job.started_at))
                    await self._edit_status(
                        job,
                        f"Жду, пока Юбара закончит генерацию для другого пользователя.\n"
                        f"В очереди бота: {elapsed // 60} мин {elapsed % 60} сек.",
                    )
            await self._edit_status(
                job,
                self._status_text(
                    "Pollinations" if job.mode == "fast" else "AI Horde",
                    job.started_at,
                    "создаю изображение" if job.mode == "fast" else "обрабатываю запрос",
                ),
            )
            if job.mode == "fast":
                image, mime_type, model_name = await self._generate_pollinations(job)
            else:
                image, mime_type, model_name = await self._generate_horde(job)
            await self._publish_and_deliver(job, context, image, mime_type, model_name)
        except asyncio.CancelledError:
            if job.mode == "horde" and job.remote_id:
                await self._cancel_horde_request(job.remote_id)
            await self._edit_status(
                job,
                "Генерацию отменили. Заказ достоин лучшего момента.",
                active=False,
            )
            raise
        except Exception as exc:
            elapsed = max(0, int(time.monotonic() - job.started_at))
            reason = self._safe_error_summary(exc)
            logging.error(
                "Ошибка генерации job_id=%s provider=%s chat_id=%s elapsed_seconds=%s: %s",
                job.job_id,
                job.mode,
                job.chat_id,
                elapsed,
                reason,
                exc_info=True,
            )
            await self._edit_status(
                job,
                "Не получилось создать изображение. "
                f"Сервис: {job.mode}; причина: {reason}. Попробуй позже или другой режим.",
                active=False,
            )
        finally:
            if generation_slot_acquired:
                self._generation_lock.release()
            if self.jobs.get((job.chat_id, job.owner_id)) is job:
                self.jobs.pop((job.chat_id, job.owner_id), None)

    @staticmethod
    def _safe_error_summary(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            try:
                response_data = response.json()
            except ValueError:
                response_data = {}

            error_code = (
                response_data.get("rc")
                if isinstance(response_data, dict)
                else None
            )
            server_message = (
                response_data.get("message")
                if isinstance(response_data, dict)
                else None
            )
            endpoint = response.request.url.path if response.request else "unknown"

            if error_code == "RequestNotFound":
                return (
                    "AI Horde больше не находит задание: оно могло быть удалено "
                    "сервисом за бездействие (RequestNotFound, HTTP 404; "
                    f"endpoint {endpoint})."
                )
            if error_code == "RequestExpired":
                return (
                    "AI Horde просрочил задание (RequestExpired, "
                    f"HTTP {response.status_code}; endpoint {endpoint})."
                )

            details = ""
            if isinstance(error_code, str) and error_code:
                details += f"; код {error_code}"
            if isinstance(server_message, str) and server_message:
                safe_message = re.sub(r"https?://\S+", "[URL скрыт]", server_message)
                details += f"; ответ сервиса: {safe_message[:180]}"
            return (
                f"HTTP {response.status_code} {response.reason_phrase} "
                f"(endpoint {endpoint}{details})"
            )[:500]
        if isinstance(error, httpx.RequestError):
            return f"Сетевая ошибка ({type(error).__name__})."
        return f"{type(error).__name__}: {str(error)[:250]}"

    async def _generate_pollinations(self, job: ImageJob) -> tuple[bytes, str, str]:
        prompt = f"{ANIME_PROMPT_PREFIX}{job.prompt}"
        url = f"{POLLINATIONS_URL}/{quote(prompt, safe='')}"
        params = {
            "model": job.settings.pollinations_model,
            "width": job.settings.width,
            "height": job.settings.height,
            "nologo": "true",
            "enhance": "true",
            "private": "true",
            "seed": job.seed,
        }
        if job.settings.negative_prompt:
            params["negative"] = job.settings.negative_prompt
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
            request_task = asyncio.create_task(
                client.get(url, params=params, headers={"User-Agent": CLIENT_AGENT})
            )
            try:
                while not request_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(request_task),
                            timeout=STATUS_REFRESH_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        await self._edit_status(
                            job,
                            self._status_text(
                                "Pollinations",
                                job.started_at,
                                "создаю изображение",
                            ),
                        )
                response = await request_task
            except asyncio.CancelledError:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "").split(";", 1)[0]
        if not mime_type.startswith("image/"):
            raise RuntimeError(f"Pollinations вернул неожиданный Content-Type: {mime_type!r}")
        if len(response.content) > MAX_IMAGE_BYTES:
            raise RuntimeError("Сгенерированный файл превышает лимит отправки фотографии.")
        return response.content, mime_type, "Pollinations"

    async def _generate_horde(self, job: ImageJob) -> tuple[bytes, str, str]:
        headers = {
            "apikey": AI_HORDE_API_KEY,
            "Client-Agent": CLIENT_AGENT,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            models = await self._available_anime_models(
                client,
                headers,
                job,
                job.settings.model_preference,
            )
            prompt = f"{ANIME_PROMPT_PREFIX}{job.prompt}"
            if job.settings.negative_prompt:
                prompt += f" ### {job.settings.negative_prompt}"
            payload = {
                "prompt": prompt,
                "params": {
                    "width": job.settings.width,
                    "height": job.settings.height,
                    "steps": 25,
                    "cfg_scale": 7.0,
                    "seed": job.seed,
                    "n": 1,
                },
                "nsfw": False,
                "censor_nsfw": True,
                "trusted_workers": False,
                "r2": True,
            }
            if models:
                payload["models"] = [models[0]]
            retries = 0
            while True:
                try:
                    generated, model_name = await self._request_horde_generation(
                        client,
                        headers,
                        payload,
                        models,
                        job,
                    )
                    break
                except HordeRequestExpired:
                    retries += 1
                    logging.warning(
                        "AI Horde удалил задание из очереди; отправляю заново "
                        "(job_id=%s, повтор=%s).",
                        job.job_id,
                        retries,
                    )
                    elapsed = max(0, int(time.monotonic() - job.started_at))
                    await self._edit_status(
                        job,
                        f"Юбара уже {elapsed // 60} мин {elapsed % 60} сек ждёт AI Horde.\n"
                        "Сервис удалил долго ожидавшее задание, отправляю его повторно.\n"
                        "Ожидание продолжается без ограничения по времени.",
                    )
            image_source = generated.get("img")
            if not isinstance(image_source, str) or not image_source:
                raise RuntimeError("AI Horde вернул некорректную ссылку на изображение.")
            if image_source.startswith("data:image/"):
                header, encoded = image_source.split(",", 1)
                mime_type = header[5:].split(";", 1)[0]
                image = base64.b64decode(encoded, validate=True)
            elif image_source.startswith("https://"):
                image_response = await client.get(image_source)
                image_response.raise_for_status()
                mime_type = image_response.headers.get("content-type", "").split(";", 1)[0]
                image = image_response.content
            else:
                image = base64.b64decode(image_source, validate=True)
                if image.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime_type = "image/png"
                elif image.startswith(b"\xff\xd8\xff"):
                    mime_type = "image/jpeg"
                elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
                    mime_type = "image/webp"
                else:
                    raise RuntimeError("AI Horde вернул неизвестный формат изображения.")
            if not mime_type.startswith("image/") or len(image) > MAX_IMAGE_BYTES:
                raise RuntimeError("AI Horde вернул файл неподдерживаемого типа или размера.")
            return image, mime_type, model_name

    async def _request_horde_generation(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        payload: dict[str, object],
        models: list[str],
        job: ImageJob,
    ) -> tuple[dict[str, object], str]:
        response = await client.post(
            f"{AI_HORDE_URL}/generate/async",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        request_data = response.json()
        remote_id = request_data.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise RuntimeError("AI Horde не вернул ID задания.")
        job.remote_id = remote_id

        warnings = request_data.get("warnings", [])
        if isinstance(warnings, list):
            warning_text = " ".join(str(item) for item in warnings)
        elif isinstance(warnings, str):
            warning_text = warnings
        else:
            warning_text = ""
        no_workers = (
            "NoAvailableWorker" in warning_text
            or "no available workers" in str(request_data.get("message", "")).casefold()
        )
        if no_workers:
            logging.warning(
                "AI Horde принял запрос без подходящих воркеров; задание будет "
                "автоматически поставлено повторно при удалении из очереди "
                "(job_id=%s, remote_id=%s).",
                job.job_id,
                remote_id,
            )

        model_name = models[0] if models else "AI Horde"
        request_started_at = time.monotonic()
        await self._edit_status(
            job,
            self._status_text(
                "AI Horde",
                job.started_at,
                "нет подходящих воркеров; запрос будет обновлён автоматически"
                if no_workers
                else "запрос в очереди",
            ),
        )

        while True:
            check_response = await client.get(
                f"{AI_HORDE_URL}/generate/check/{remote_id}",
                headers=headers,
            )
            try:
                check_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if (
                    self._is_horde_request_not_found(exc)
                    and time.monotonic() - request_started_at
                    >= HORDE_REQUEUE_MIN_AGE_SECONDS
                ):
                    raise HordeRequestExpired from exc
                raise
            check = check_response.json()
            queue_position = int(check.get("queue_position") or 0)
            eta = int(check.get("wait_time") or 0)
            elapsed = max(0, int(time.monotonic() - job.started_at))
            await self._edit_status(
                job,
                f"Юбара уже {elapsed // 60} мин {elapsed % 60} сек ждёт AI Horde.\n"
                f"Позиция в очереди: {queue_position}.\n"
                f"Ожидание по оценке сервиса: около {max(0, eta)} сек."
                + (
                    "\nПодходящих воркеров нет; задание будет отправлено повторно "
                    "при удалении из очереди."
                    if no_workers
                    else ""
                ),
            )
            if check.get("faulted"):
                raise RuntimeError("AI Horde сообщил об ошибке генерации.")
            if check.get("done"):
                break
            await asyncio.sleep(5)

        status_response = await client.get(
            f"{AI_HORDE_URL}/generate/status/{remote_id}",
            headers=headers,
        )
        try:
            status_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if (
                self._is_horde_request_not_found(exc)
                and time.monotonic() - request_started_at
                >= HORDE_REQUEUE_MIN_AGE_SECONDS
            ):
                raise HordeRequestExpired from exc
            raise
        generations = status_response.json().get("generations", [])
        if not generations or not isinstance(generations[0], dict):
            raise RuntimeError("AI Horde завершил задание без изображения.")
        generated = generations[0]
        model_name = str(generated.get("model") or model_name)
        return generated, model_name

    @staticmethod
    def _is_horde_request_not_found(error: httpx.HTTPStatusError) -> bool:
        if error.response.status_code != 404:
            return False
        try:
            response_data = error.response.json()
        except ValueError:
            return False
        return (
            isinstance(response_data, dict)
            and response_data.get("rc") in ("RequestNotFound", "RequestExpired")
        )

    async def _available_anime_models(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        job: ImageJob,
        preference: str,
    ) -> list[str]:
        await self._edit_status(
            job,
            self._status_text("AI Horde", job.started_at, "ищу доступные аниме-модели"),
        )
        response = await client.get(
            f"{AI_HORDE_URL}/status/models",
            params={"type": "image"},
            headers=headers,
        )
        response.raise_for_status()
        models = response.json()
        if not isinstance(models, list):
            raise RuntimeError("AI Horde вернул некорректный список моделей.")
        anime_models = [
            item
            for item in models
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and int(item.get("count") or 0) > 0
            and any(
                marker in item["name"].casefold()
                for marker in ("anime", "animagine", "anylora", "anything", "orangemix")
            )
            and "hentai" not in item["name"].casefold()
        ]
        if not anime_models:
            logging.warning(
                "В AI Horde нет доступных аниме-моделей (job_id=%s); отдаю выбор модели сервису.",
                job.job_id,
            )
            return []
        preferred_markers = (
            ("animagine", "anime", "anything")
            if preference == "auto"
            else (preference,)
        )
        for marker in preferred_markers:
            preferred_models = [
                item for item in anime_models if marker in item["name"].casefold()
            ]
            if preferred_models:
                anime_models = preferred_models
                break
        anime_models.sort(
            key=lambda item: (
                float(item.get("performance") or 0),
                int(item.get("count") or 0),
            ),
            reverse=True,
        )
        return [str(item["name"]) for item in anime_models[:3]]

    async def _cancel_horde_request(
        self,
        remote_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        request_headers = headers or {
            "apikey": AI_HORDE_API_KEY,
            "Client-Agent": CLIENT_AGENT,
        }
        try:
            if client is not None:
                response = await client.delete(
                    f"{AI_HORDE_URL}/generate/status/{remote_id}",
                    headers=request_headers,
                )
                if response.status_code not in (200, 202, 204, 404):
                    response.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=10.0) as cancellation_client:
                    response = await cancellation_client.delete(
                        f"{AI_HORDE_URL}/generate/status/{remote_id}",
                        headers=request_headers,
                    )
                    if response.status_code not in (200, 202, 204, 404):
                        response.raise_for_status()
        except httpx.HTTPError:
            logging.exception("Не удалось отменить задание AI Horde %s.", remote_id)

    async def _publish_and_deliver(
        self,
        job: ImageJob,
        context: ContextTypes.DEFAULT_TYPE,
        image: bytes,
        mime_type: str,
        model_name: str,
    ) -> None:
        image_file = io.BytesIO(image)
        image_file.name = "yubara.png" if mime_type == "image/png" else "yubara.jpg"
        try:
            await context.bot.send_photo(
                chat_id=BACKUP_CHANNEL_ID,
                photo=image_file,
                caption=f"Yubara | {model_name}\n{job.prompt}"[:1024],
            )
        except TelegramError:
            logging.exception("Не удалось опубликовать генерацию в канале BACKUP_CHANNEL_ID.")

        image_file.seek(0)
        try:
            await context.bot.send_photo(
                chat_id=job.chat_id,
                photo=image_file,
                caption=f"Милостиво готово. Модель: {model_name}",
            )
        except TelegramError:
            logging.exception("Не удалось отправить готовое изображение в чат %s.", job.chat_id)
            await self._edit_status(
                job,
                "Картинка создана, но Telegram не смог отправить её в чат.",
                active=False,
            )
            return

        await self._edit_status(
            job,
            "Готово! Изображение отправлено сюда.",
            active=False,
        )

    def _status_text(self, provider: str, started_at: float, phase: str) -> str:
        elapsed = max(0, int(time.monotonic() - started_at))
        return (
            f"Юбара: {provider} — {phase}.\n"
            f"Прошло: {elapsed // 60} мин {elapsed % 60} сек."
        )

    async def _edit_status(self, job: ImageJob, text: str, *, active: bool = True) -> None:
        async with job.status_lock:
            try:
                await job.bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=text,
                    reply_markup=_cancel_keyboard(job.job_id) if active else None,
                )
            except TelegramError:
                logging.warning("Не удалось обновить статус задачи %s.", job.job_id, exc_info=True)

    async def refresh_status(
        self,
        chat_id: int,
        bot: object,
        *,
        owner_id: int | None = None,
    ) -> None:
        jobs = [
            job
            for (active_chat_id, active_owner_id), job in self.jobs.items()
            if active_chat_id == chat_id
            and (owner_id is None or active_owner_id == owner_id)
            and job.task is not None
            and not job.task.done()
        ]
        for job in jobs:
            async with job.status_lock:
                try:
                    await bot.delete_message(chat_id, job.status_message_id)
                except TelegramError:
                    logging.info("Предыдущее сообщение статуса %s удалить не удалось.", job.job_id)
                try:
                    status_message = await bot.send_message(
                        chat_id=chat_id,
                        text=self._status_text(
                            "Pollinations" if job.mode == "fast" else "AI Horde",
                            job.started_at,
                            "создаю изображение" if job.mode == "fast" else "обрабатываю запрос",
                        ),
                        reply_markup=_cancel_keyboard(job.job_id),
                    )
                    job.status_message_id = status_message.message_id
                except TelegramError:
                    logging.exception("Не удалось переместить статус задачи %s вниз чата.", job.job_id)

    async def cancel_all(self) -> None:
        active_jobs = list(self.jobs.values())
        for job in active_jobs:
            if job.task and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in active_jobs if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

image_manager = ImageGenerationManager()


async def on_image_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await image_manager.handle_callback(update, context)


async def stop_image_jobs() -> None:
    await image_manager.cancel_all()
