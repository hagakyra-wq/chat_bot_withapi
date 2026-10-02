"""Независимая асинхронная генерация изображений для личных и групповых чатов."""

import asyncio
import base64
import io
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import BACKUP_CHANNEL_ID

POLLINATIONS_URL = "https://image.pollinations.ai/prompt"
AI_HORDE_URL = "https://aihorde.net/api/v2"
AI_HORDE_ANONYMOUS_KEY = "0000000000"
CLIENT_AGENT = "YubaraTelegramBot:1.0.0:github.com/hagakyra-wq/chat_bot_withapi"
POLLINATIONS_COOLDOWN_SECONDS = 120
MAX_IMAGE_BYTES = 9 * 1024 * 1024
PROMPT_MAX_LENGTH = 1500
STATUS_REFRESH_SECONDS = 5

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
class ImageJob:
    job_id: str
    mode: str
    prompt: str
    owner_id: int
    chat_id: int
    status_message_id: int
    bot: object
    started_at: float = field(default_factory=time.monotonic)
    remote_id: str | None = None
    task: asyncio.Task[object] | None = None
    status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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


def _cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Отменить генерацию", callback_data=f"img:cancel:{job_id}")]]
    )


class ImageGenerationManager:
    def __init__(self) -> None:
        self.pending: dict[str, PendingImage] = {}
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
        )
        self.jobs[job_key] = job
        job.task = context.application.create_task(
            self._run_job(job, context),
            update=update,
            name=f"image-generation-{job_id}",
        )

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
        try:
            if self._generation_lock.locked():
                await self._edit_status(
                    job,
                    "Ожидаю, пока Юбара закончит генерацию для другого пользователя.",
                )
            async with self._generation_lock:
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
        except Exception:
            logging.exception(
                "Ошибка генерации изображения через %s (chat_id=%s)",
                job.mode,
                job.chat_id,
            )
            await self._edit_status(
                job,
                "Не получилось создать изображение. Попробуй другой режим или измени описание.",
                active=False,
            )
        finally:
            if self.jobs.get((job.chat_id, job.owner_id)) is job:
                self.jobs.pop((job.chat_id, job.owner_id), None)

    async def _generate_pollinations(self, job: ImageJob) -> tuple[bytes, str, str]:
        url = f"{POLLINATIONS_URL}/{quote(job.prompt, safe='')}"
        params = {
            "width": 768,
            "height": 1024,
            "nologo": "true",
            "enhance": "true",
            "private": "true",
        }
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
            "apikey": AI_HORDE_ANONYMOUS_KEY,
            "Client-Agent": CLIENT_AGENT,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            models = await self._available_anime_models(client, headers, job)
            payload = {
                "prompt": job.prompt,
                "params": {
                    "width": 768,
                    "height": 1024,
                    "steps": 25,
                    "cfg_scale": 7.0,
                    "n": 1,
                },
                "models": [models[0]],
                "nsfw": False,
                "censor_nsfw": True,
                "trusted_workers": False,
                "shared": False,
            }
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
            await self._edit_status(
                job,
                self._status_text("AI Horde", job.started_at, "запрос в очереди"),
            )

            while True:
                check_response = await client.get(
                    f"{AI_HORDE_URL}/generate/check/{remote_id}",
                    headers=headers,
                )
                check_response.raise_for_status()
                check = check_response.json()
                queue_position = int(check.get("queue_position") or 0)
                eta = int(check.get("wait_time") or 0)
                elapsed = max(0, int(time.monotonic() - job.started_at))
                await self._edit_status(
                    job,
                    f"Юбара уже {elapsed // 60} мин {elapsed % 60} сек ждёт AI Horde.\n"
                    f"Позиция в очереди: {queue_position}.\n"
                    f"Ожидание по оценке сервиса: около {max(0, eta)} сек.",
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
            status_response.raise_for_status()
            generations = status_response.json().get("generations", [])
            if not generations:
                raise RuntimeError("AI Horde завершил задание без изображения.")
            generated = generations[0]
            image_source = generated.get("img")
            model_name = str(generated.get("model") or models[0])
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

    async def _available_anime_models(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        job: ImageJob,
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
            raise RuntimeError("Сейчас в AI Horde нет доступных аниме-моделей с воркерами.")
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
            "apikey": AI_HORDE_ANONYMOUS_KEY,
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
