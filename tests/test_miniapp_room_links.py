import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

import main


class MiniAppRoomLinkTests(unittest.IsolatedAsyncioTestCase):
    def _signed_init_data(self):
        fields = {
            "auth_date": str(int(time.time())),
            "query_id": "query",
            "user": json.dumps({"id": 123, "first_name": "Игрок"}),
        }
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(
            b"WebAppData",
            main.TELEGRAM_TOKEN.encode(),
            hashlib.sha256,
        ).digest()
        fields["hash"] = hmac.new(
            secret,
            check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        return fields

    def test_room_url_adds_query_code_and_requires_https(self):
        with patch.object(main, "MINI_APP_URL", "https://games.example/mini?theme=dark#old"):
            result = main._room_app_url("ABC234")
        parsed = urlsplit(result)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.path, "/mini")
        self.assertEqual(parse_qs(parsed.query), {"theme": ["dark"], "room": ["ABC234"]})
        self.assertEqual(parsed.fragment, "")

        with patch.object(main, "MINI_APP_URL", "http://games.example"):
            self.assertIsNone(main._room_app_url("ABC234"))

    async def test_start_payload_receives_web_app_button_with_room_code(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=["room_ABC234"],
            bot=SimpleNamespace(username="YubaraBot"),
        )
        room = {
            "status": "lobby",
            "max_players": 2,
            "state": {"players": [{"user_id": 12}]},
        }
        with (
            patch.object(main, "MINI_APP_URL", "https://games.example/"),
            patch("main.database.get_minigame_room", new=AsyncMock(return_value=room)),
        ):
            await main.room_start_command(update, context)

        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].web_app.url,
            "https://games.example/?room=ABC234",
        )

    def test_init_data_signature_remains_required(self):
        fields = self._signed_init_data()
        valid = urlencode(fields)
        self.assertEqual(main._validate_web_app_init_data(valid)[0], 123)

        fields["user"] = json.dumps({"id": 456, "first_name": "Other"})
        with self.assertRaisesRegex(ValueError, "signature"):
            main._validate_web_app_init_data(urlencode(fields))

    def test_room_query_loads_miniapp_and_auth_errors_are_json(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), main.HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base_url}/?room=ABC234", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"room-join-form", response.read())
            with urlopen(f"{base_url}/assets/sounds.js", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"playGameSound", response.read())

            request = Request(
                f"{base_url}/api/session",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(
                raised.exception.headers.get_content_type(),
                "application/json",
            )
            self.assertIn(b"error", raised.exception.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
