"""Проверки читающей стороны: что видно в сообщении и что остаётся на диске.

Запуск: PYTHONPATH=whatsapp-mcp-server python -m unittest discover -s whatsapp-mcp-server/tests
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import whatsapp


# Те же колонки, что заводит мост. Здесь они нужны, чтобы читающие запросы
# работали на своей базе, а не на той, в которую пишет живой мост.
SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
CREATE TABLE messages (
    id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
    is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT, direct_path TEXT,
    media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
    reply_to TEXT, reply_sender TEXT, mentions TEXT, edited_at TIMESTAMP,
    revoked_at TIMESTAMP,
    PRIMARY KEY (id, chat_jid)
);
CREATE TABLE reactions (
    message_id TEXT, chat_jid TEXT, sender TEXT, emoji TEXT, timestamp TIMESTAMP,
    PRIMARY KEY (message_id, chat_jid, sender)
);
"""


def message(**fields) -> whatsapp.Message:
    base = dict(
        timestamp=datetime(2026, 10, 23, 4, 30, 0),
        sender="123456789",
        content="привет",
        is_from_me=False,
        chat_jid="123456789@s.whatsapp.net",
        id="MSG1",
        chat_name="Вика",
    )
    base.update(fields)
    return whatsapp.Message(**base)


class FormatMessageTest(unittest.TestCase):
    """Пометки в строке сообщения: без них правленое и удалённое выглядят
    так же, как написанное только что."""

    def test_plain(self):
        line = whatsapp.format_message(message())
        self.assertIn("привет", line)
        self.assertNotIn("[edited]", line)
        self.assertNotIn("[deleted", line)

    def test_edited(self):
        line = whatsapp.format_message(message(edited=True, content="вторая версия"))
        self.assertIn("[edited] вторая версия", line)

    def test_revoked_wins_over_edited(self):
        # Поправили, а потом удалили: в чате сообщения больше нет, и это
        # важнее того, что его когда-то правили.
        line = whatsapp.format_message(message(edited=True, revoked=True))
        self.assertIn("[deleted by sender]", line)
        self.assertNotIn("[edited]", line)

    def test_reactions(self):
        line = whatsapp.format_message(
            message(reactions=[("123456789", "❤️"), ("987654321", "👍")])
        )
        self.assertIn("[Reactions: ❤️ 123456789, 👍 987654321]", line)

    def test_reply_and_mentions(self):
        line = whatsapp.format_message(
            message(
                reply_to="QUOTED1",
                reply_sender="40733873140@s.whatsapp.net",
                mentions=["111@s.whatsapp.net"],
            )
        )
        self.assertIn("[Reply to", line)
        self.assertIn("QUOTED1", line)
        self.assertIn("[Mentions:", line)


class MentionsFromRowTest(unittest.TestCase):
    """Упоминания мост пишет JSON-массивом, а старые строки пусты."""

    def test_list(self):
        self.assertEqual(
            whatsapp.mentions_from_row('["111@s.whatsapp.net"]'),
            ["111@s.whatsapp.net"],
        )

    def test_empty_and_broken(self):
        for raw in (None, "", "[]", "не json", "{}"):
            self.assertIsNone(whatsapp.mentions_from_row(raw), raw)


class AttachReactionsTest(unittest.TestCase):
    """Реакции приезжают одним запросом на весь список."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "messages.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_matched_by_chat_too(self):
        # Один и тот же номер сообщения встречается в разных чатах, поэтому
        # ключ - пара: иначе реакция уехала бы в чужой чат.
        self.conn.executemany(
            "INSERT INTO reactions VALUES (?, ?, ?, ?, ?)",
            [
                ("MSG1", "123456789@s.whatsapp.net", "123456789", "❤️", "2026-10-23"),
                ("MSG1", "999@g.us", "555", "👍", "2026-10-23"),
            ],
        )
        mine = message()
        other = message(chat_jid="999@g.us")
        whatsapp.attach_reactions(self.conn, [mine, other])
        self.assertEqual(mine.reactions, [("123456789", "❤️")])
        self.assertEqual(other.reactions, [("555", "👍")])

    def test_no_reactions_left_as_none(self):
        only = message()
        whatsapp.attach_reactions(self.conn, [only])
        self.assertIsNone(only.reactions)

    def test_missing_table_is_not_an_error(self):
        # База, в которую писал мост постарше: таблицы реакций нет вовсе,
        # и чтение сообщений от этого падать не должно.
        bare = sqlite3.connect(":memory:")
        try:
            only = message()
            whatsapp.attach_reactions(bare, [only])
            self.assertIsNone(only.reactions)
        finally:
            bare.close()


class SendAudioMessageTest(unittest.TestCase):
    """Перекодированный ogg - временный файл, и после отправки он не нужен."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.source = os.path.join(self.directory, "voice.m4a")
        with open(self.source, "wb") as handle:
            handle.write(b"not really audio")
        self.converted = os.path.join(self.directory, "converted.ogg")

    def _convert(self, path, *args, **kwargs):
        with open(self.converted, "wb") as handle:
            handle.write(b"ogg")
        return self.converted

    def test_converted_file_removed_after_sending(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "message": "sent"}
        with mock.patch.object(whatsapp.audio, "convert_to_opus_ogg_temp", self._convert), \
                mock.patch.object(whatsapp.requests, "post", return_value=response):
            success, _ = whatsapp.send_audio_message("123456789", self.source)
        self.assertTrue(success)
        self.assertFalse(os.path.exists(self.converted), "converted file left behind")
        self.assertTrue(os.path.exists(self.source), "the caller's own file was removed")

    def test_converted_file_removed_when_sending_fails(self):
        with mock.patch.object(whatsapp.audio, "convert_to_opus_ogg_temp", self._convert), \
                mock.patch.object(whatsapp.requests, "post",
                                  side_effect=whatsapp.requests.RequestException("no bridge")):
            success, _ = whatsapp.send_audio_message("123456789", self.source)
        self.assertFalse(success)
        self.assertFalse(os.path.exists(self.converted), "converted file left behind on failure")

    def test_own_ogg_is_kept(self):
        ogg = os.path.join(self.directory, "ready.ogg")
        with open(ogg, "wb") as handle:
            handle.write(b"ogg")
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "message": "sent"}
        with mock.patch.object(whatsapp.requests, "post", return_value=response):
            success, _ = whatsapp.send_audio_message("123456789", ogg)
        self.assertTrue(success)
        self.assertTrue(os.path.exists(ogg), "a caller's .ogg must survive sending")


class RequestOlderMessagesTest(unittest.TestCase):
    """Просьба догрузить историю: ответ моста передаётся как есть."""

    def test_passes_chat_and_count(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "message": "History now starts earlier"}
        with mock.patch.object(whatsapp.requests, "get", return_value=response) as get:
            success, text = whatsapp.request_older_messages("999@g.us", 25)
        self.assertTrue(success)
        self.assertEqual(text, "History now starts earlier")
        self.assertEqual(get.call_args.kwargs["params"], {"chat": "999@g.us", "count": 25})

    def test_chat_is_required(self):
        with mock.patch.object(whatsapp.requests, "get") as get:
            success, text = whatsapp.request_older_messages("")
        self.assertFalse(success)
        self.assertIn("Chat JID", text)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
