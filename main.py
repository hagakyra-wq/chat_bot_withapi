"""Точка входа для минимального бота: меню, удаление, транскрипция, суд и саммари."""

import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import TELEGRAM_TOKEN
from court import court_callback
from court_advanced import advanced_court_callback
from handlers_pm import handle_plain_message, menu_callback, start_command

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(court_callback, pattern="^court:"))
    app.add_handler(CallbackQueryHandler(advanced_court_callback, pattern="^court_adv:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_message))
    return app


if __name__ == "__main__":
    app = build_app()
    app.run_polling()
