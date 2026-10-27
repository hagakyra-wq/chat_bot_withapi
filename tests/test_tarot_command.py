import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from chat_tarot import tarot_command


class TarotCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_question_uses_general_reading(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=None)
        cards = [
            {
                "position": position,
                "symbol": "✦",
                "name": f"Карта {index}",
                "reversed": False,
            }
            for index, position in enumerate(
                ("Прошлое", "Настоящее", "Возможное направление"),
                start=1,
            )
        ]

        with (
            patch("chat_tarot.draw_spread", return_value=cards),
            patch(
                "chat_tarot.llm.generate_tarot_interpretation",
                new=AsyncMock(return_value="Время подумать."),
            ) as interpret,
        ):
            await tarot_command(update, context)

        self.assertEqual(interpret.await_args.args[0], "Общий расклад")

    async def test_each_call_generates_a_new_unlimited_reading(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=["Что", "меня", "ждёт?"])
        cards = [
            {
                "position": position,
                "symbol": "✦",
                "name": f"Карта {index}",
                "reversed": False,
            }
            for index, position in enumerate(
                ("Прошлое", "Настоящее", "Возможное направление"),
                start=1,
            )
        ]

        with (
            patch("chat_tarot.draw_spread", side_effect=[cards, cards]) as draw,
            patch(
                "chat_tarot.llm.generate_tarot_interpretation",
                new=AsyncMock(return_value="Время подумать."),
            ) as interpret,
        ):
            await tarot_command(update, context)
            await tarot_command(update, context)

        self.assertEqual(draw.call_count, 2)
        self.assertEqual(interpret.await_count, 2)
        self.assertEqual(message.reply_text.await_count, 2)
        self.assertIn("Вопрос: Что меня ждёт?", message.reply_text.call_args.args[0])

    async def test_rejects_long_question(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=["x" * 501])

        with patch("chat_tarot.draw_spread") as draw:
            await tarot_command(update, context)

        draw.assert_not_called()
        message.reply_text.assert_awaited_once_with("Сократи вопрос до 500 символов.")


if __name__ == "__main__":
    unittest.main()
