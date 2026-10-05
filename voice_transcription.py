"""Распознавание аудиосообщений, на которые пользователь отвечает."""

import logging
import tempfile
from pathlib import Path

from telegram import Audio, Document, Message, Voice
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from llm import transcribe_audio

MAX_AUDIO_FILE_SIZE_BYTES = 20 * 1024 * 1024
MAX_TRANSCRIPT_CHUNK_SIZE = 3500
_TRANSCRIPTION_REQUESTS = (
    "в текст",
    "расшифруй",
    "расшифровать",
    "транскриб",
    "что там",
    "что говорится",
    "что говориться",
    "что сказано",
    "что говорят",
)
_AUDIO_SUFFIXES = {".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
_AUDIO_MIME_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-wav": ".wav",
}


def _audio_attachment(message: Message | None) -> tuple[Audio | Document | Voice, str] | None:
    if message is None:
        return None
    if message.voice is not None:
        return message.voice, "voice.ogg"

    audio: Audio | Document | None = message.audio
    if audio is None and message.document is not None:
        document = message.document
        if document.mime_type and document.mime_type.startswith("audio/"):
            audio = document
    if audio is None:
        return None

    file_name = audio.file_name or ""
    suffix = Path(file_name).suffix.casefold()
    if suffix not in _AUDIO_SUFFIXES:
        suffix = _AUDIO_MIME_SUFFIXES.get(audio.mime_type or "", ".ogg")
    return audio, f"audio{suffix}"


def is_transcription_request(text: str, message: Message) -> bool:
    replied = message.reply_to_message
    if _audio_attachment(replied) is None:
        return False
    normalized_text = " ".join(text.casefold().split())
    return any(phrase in normalized_text for phrase in _TRANSCRIPTION_REQUESTS)


async def _transcribe_reply(message: Message, context: ContextTypes.DEFAULT_TYPE) -> str:
    attachment = _audio_attachment(message.reply_to_message)
    if attachment is None:
        raise ValueError("Ответь на голосовое или аудиофайл, чтобы распознать его.")

    audio, filename = attachment
    if audio.file_size and audio.file_size > MAX_AUDIO_FILE_SIZE_BYTES:
        raise ValueError("Аудиофайл больше 20 МБ и не может быть обработан ботом.")

    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
        audio_path = Path(temporary_file.name)
    try:
        telegram_file = await context.bot.get_file(audio.file_id)
        await telegram_file.download_to_drive(custom_path=str(audio_path))
        with audio_path.open("rb") as audio_file:
            return await transcribe_audio(audio_file, filename)
    finally:
        audio_path.unlink(missing_ok=True)


async def handle_voice_transcription(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        try:
            await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        except TelegramError:
            logging.debug("Не удалось показать действие распознавания аудио.", exc_info=True)

        transcript = await _transcribe_reply(message, context)
        if not transcript:
            transcript = "Не удалось распознать речь в этом аудиосообщении."
        chunks = [
            transcript[index : index + MAX_TRANSCRIPT_CHUNK_SIZE]
            for index in range(0, len(transcript), MAX_TRANSCRIPT_CHUNK_SIZE)
        ]
        for index, chunk in enumerate(chunks):
            answer = ("Текст голосового:\n" if index == 0 else "") + chunk
            sent = await message.reply_text(answer)
            try:
                await database.save_message(
                    message.chat_id,
                    context.bot.id,
                    sent.message_id,
                    "Юбара",
                    answer,
                )
            except Exception:
                logging.exception("Не удалось сохранить распознанный текст в истории чата.")
    except ValueError as error:
        await message.reply_text(str(error))
    except Exception:
        logging.exception("Не удалось распознать аудиосообщение через Groq.")
        try:
            await message.reply_text("Не удалось распознать голосовое. Попробуй ещё раз позже.")
        except TelegramError:
            logging.exception("Не удалось отправить сообщение об ошибке распознавания.")