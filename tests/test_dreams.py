import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from death_fate import (
    format_time_left,
    is_death_reason_request,
    is_death_time_request,
    random_death_deadline,
)


class DeathFateTests(unittest.TestCase):
    def test_time_requests_match_expected_phrases(self):
        self.assertTrue(is_death_time_request("сколько мне осталось?"))
        self.assertFalse(is_death_time_request("/timeleft"))
        self.assertFalse(is_death_time_request("сколько мне осталось сделать?"))

    def test_reason_requests_match_expected_phrases(self):
        self.assertTrue(is_death_reason_request("причина смерти"))
        self.assertFalse(is_death_reason_request("/death_reason"))
        self.assertFalse(is_death_reason_request("причина для разговора"))

    def test_random_deadline_is_in_the_future_and_formatted(self):
        deadline = random_death_deadline()
        self.assertGreater(deadline, __import__("datetime").datetime.now())
        self.assertIn("дн.", format_time_left(deadline))


if __name__ == "__main__":
    unittest.main()
