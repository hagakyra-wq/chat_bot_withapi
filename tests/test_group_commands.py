import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from handlers_pm import _admin_help_text, _admin_panel_markup
from main import build_app


class GroupCommandFilterTests(unittest.TestCase):
    def test_admin_panel_contains_reset_action_and_aliases(self):
        panel_buttons = [
            button.text
            for row in _admin_panel_markup().inline_keyboard
            for button in row
        ]
        self.assertIn("🧹 Сбросить историю чата", panel_buttons)

        help_text = _admin_help_text()
        for command in ("/adm", "/admin", "/adminhelp", "/helpadmin"):
            self.assertIn(command, help_text)

    def test_admin_callback_pattern_is_registered(self):
        app = build_app()
        handlers = app.handlers.get(0, [])
        patterns = [
            getattr(handler, "pattern", None)
            for handler in handlers
            if getattr(handler, "callback", None) is not None
        ]
        self.assertTrue(any(p and p.pattern == r"^(menu|admin):" for p in patterns))


if __name__ == "__main__":
    unittest.main()
