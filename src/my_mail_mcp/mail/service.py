import logging
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser

import certifi
from imapclient import IMAPClient

from my_mail_mcp.config import Account

logger = logging.getLogger(__name__)

# Providers cap simultaneous logins per mailbox; extra requests wait for a free slot.
MAX_CONNECTIONS = 3
CONNECTION_WAIT_SECONDS = 60


class MailService:
    def __init__(self, settings: Account):
        self.settings = settings
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    @contextmanager
    def connection(self):
        # Each request owns its connection; instances share no mailbox state.
        operation = sys._getframe(2).f_code.co_name
        started = time.perf_counter()
        if not self._slots.acquire(timeout=CONNECTION_WAIT_SECONDS):
            logger.warning("imap account=%s op=%s busy", self.settings.id, operation)
            raise ValueError(
                "Mailbox is busy with too many simultaneous requests; retry shortly"
            )
        try:
            yield from self._session(operation, started)
        finally:
            self._slots.release()

    def _session(self, operation: str, started: float):
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        with IMAPClient(
            self.settings.imap_host,
            port=self.settings.imap_port,
            ssl=True,
            ssl_context=context,
            timeout=30,
        ) as client:
            try:
                client.login(
                    self.settings.username,
                    self.settings.password.get_secret_value(),
                )
            except IMAPClient.Error as exc:
                logger.warning(
                    "imap account=%s op=%s login_failed=%s",
                    self.settings.id,
                    operation,
                    type(exc).__name__,
                )
                raise
            connected = time.perf_counter()
            try:
                yield client
            finally:
                done = time.perf_counter()
                logger.info(
                    "imap account=%s op=%s connect_login=%.2fs work=%.2fs",
                    self.settings.id,
                    operation,
                    connected - started,
                    done - connected,
                )

    def list_folders(self) -> list[dict]:
        with self.connection() as client:
            return [
                {
                    "name": name,
                    "flags": [f.decode() for f in flags],
                    "delimiter": delimiter.decode()
                    if isinstance(delimiter, bytes)
                    else delimiter,
                }
                for flags, delimiter, name in client.list_folders()
            ]

    def search_messages(
        self,
        folder: str,
        query: str,
        limit: int,
        offset: int,
        since: str | None = None,
        before: str | None = None,
        from_address: str | None = None,
        unread_only: bool = False,
    ) -> dict:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1–100 and offset must be nonnegative")
        criteria = ["TEXT", query] if query else []
        if since:
            criteria += ["SINCE", self._day(since, "since")]
        if before:
            criteria += ["BEFORE", self._day(before, "before")]
        if from_address:
            criteria += ["FROM", from_address]
        if unread_only:
            criteria.append("UNSEEN")
        with self.connection() as client:
            selected = client.select_folder(folder, readonly=True)
            uids = sorted(
                client.search(criteria or ["ALL"], charset="UTF-8"), reverse=True
            )
            page = uids[offset : offset + limit]
            fetched = client.fetch(page, ["ENVELOPE", "FLAGS"]) if page else {}
            return {
                "folder": folder,
                "uid_validity": selected[b"UIDVALIDITY"],
                "total": len(uids),
                "next_offset": offset + limit if offset + limit < len(uids) else None,
                "messages": [
                    {
                        "uid": uid,
                        "subject": self._subject(fetched[uid][b"ENVELOPE"].subject),
                        "from": self._sender(fetched[uid][b"ENVELOPE"].from_),
                        "date": self._date(fetched[uid][b"ENVELOPE"].date),
                        "flags": [f.decode() for f in fetched[uid][b"FLAGS"]],
                    }
                    for uid in page
                    if uid in fetched
                ],
                "unavailable_uids": [uid for uid in page if uid not in fetched],
            }

    @staticmethod
    def _day(value: str, name: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a date like 2026-10-26") from None

    @staticmethod
    def _date(value) -> str | None:
        return value.isoformat() if value else None

    @classmethod
    def _sender(cls, addresses) -> str:
        if not addresses:
            return ""
        first = addresses[0]
        mailbox = (first.mailbox or b"").decode("utf-8", "replace")
        host = (first.host or b"").decode("utf-8", "replace")
        name = cls._subject(first.name)
        address = f"{mailbox}@{host}"
        return f"{name} <{address}>" if name else address

    @staticmethod
    def _subject(raw: bytes | None) -> str:
        text = (raw or b"").decode("utf-8", "replace")
        try:
            return str(make_header(decode_header(text)))
        except (LookupError, ValueError):
            # A malformed charset declaration must not break the whole search.
            return text

    def read_message(self, folder: str, uid: int, uid_validity: int) -> dict:
        if uid <= 0:
            raise ValueError("uid must be positive")
        with self.connection() as client:
            selected = client.select_folder(folder, readonly=True)
            if selected[b"UIDVALIDITY"] != uid_validity:
                raise ValueError("Folder identifiers changed; search again")
            sizes = client.fetch([uid], ["RFC822.SIZE"])
            if uid not in sizes:
                raise ValueError("Message not found")
            if sizes[uid][b"RFC822.SIZE"] > 10 * 1024 * 1024:
                raise ValueError("Message exceeds the initial 10 MiB read limit")
            raw = client.fetch([uid], ["BODY.PEEK[]"])
            if uid not in raw or b"BODY[]" not in raw[uid]:
                raise ValueError("Message no longer available; search again")
            message = BytesParser(policy=policy.default).parsebytes(raw[uid][b"BODY[]"])
            body = message.get_body(preferencelist=("plain", "html"))
            content = body.get_content() if body else ""
            return {
                "uid": uid,
                "folder": folder,
                "uid_validity": uid_validity,
                "subject": str(message.get("Subject", "")),
                "from": str(message.get("From", "")),
                "to": str(message.get("To", "")),
                "date": str(message.get("Date", "")),
                "body": content[:100000],
                "body_truncated": len(content) > 100000,
                "body_content_type": body.get_content_type() if body else None,
                "attachments": [
                    {
                        "filename": part.get_filename(),
                        "content_type": part.get_content_type(),
                    }
                    for part in message.iter_attachments()
                ],
            }
