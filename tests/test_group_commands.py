import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from handlers_group import should_process_group_command


class GroupCommandFilterTests(unittest.TestCase):
    def test_unknown_slash_commands_are_ignored(self):
        self.assertFalse(should_process_group_command("/anotherbotcommand", "yubara_bot"))

    def test_commands_addressed_to_other_bots_are_ignored(self):
        self.assertFalse(should_process_group_command("/start@another_bot", "yubara_bot"))

    def test_removed_room_command_is_ignored(self):
        self.assertFalse(should_process_group_command("/room@Yubara_Bot ABC234", "yubara_bot"))

    def test_regular_messages_are_not_filtered(self):
        self.assertTrue(should_process_group_command("Привет, Юбара!", "yubara_bot"))


if __name__ == "__main__":
    unittest.main()
