import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from telegram.constants import ChatType

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from reactions import maybe_react


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_uses_recent_messages_and_older_reply(self):
        history = [
            (message_id, f"User {message_id}", message_id, f"Text {message_id}")
            for message_id in range(10, 15)
        ]
        replied = SimpleNamespace(
            message_id=1,
            from_user=SimpleNamespace(is_bot=False, first_name="Earlier"),
            text="Old replied message",
            caption=None,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type=ChatType.SUPERGROUP),
            chat_id=-1001,
            message_id=20,
            from_user=SimpleNamespace(first_name="Current", username=None),
            text="Current message",
            caption=None,
            reply_to_message=replied,
            set_reaction=AsyncMock(),
        )

        with patch(
            "reactions.choose_message_reaction",
            new_callable=AsyncMock,
            return_value="🔥",
        ) as choose:
            await maybe_react(message, history)

        prompt = choose.await_args.args[0]
        self.assertIn("[11] User 11: Text 11", prompt)
        self.assertIn("[14] User 14: Text 14", prompt)
        self.assertNotIn("[10] User 10: Text 10", prompt)
        self.assertIn("[1] Earlier: Old replied message", prompt)
        self.assertIn("[20] Current: Current message", prompt)
        message.set_reaction.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()