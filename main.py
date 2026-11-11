"""Точка входа для минимального бота: меню, удаление, транскрипция, суд и саммари."""

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import PORT, TELEGRAM_TOKEN
from court import court_callback
from court_advanced import advanced_court_callback
from handlers_pm import admin_command, handle_plain_message, menu_callback, start_command

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/health"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, _format: str, *args: object) -> None:
        del _format, args


def start_healthcheck_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Healthcheck server started on port %s", PORT)
    return server


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("adm", admin_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(court_callback, pattern="^court:"))
    app.add_handler(CallbackQueryHandler(advanced_court_callback, pattern="^court_adv:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VOICE | filters.STICKER | filters.AUDIO | filters.ANIMATION | filters.Document.ALL,
            handle_plain_message,
        )
    )
    return app


if __name__ == "__main__":
    health_server = start_healthcheck_server()
    app = build_app()
    try:
        app.run_polling()
    finally:
        health_server.shutdown()
        health_server.server_close()
