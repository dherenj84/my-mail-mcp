"""Outgoing mail: send, reply and forward over SMTP, with a copy saved to Sent."""

import logging
import smtplib
import ssl
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

import certifi
from imapclient.imapclient import SENT

from my_mail_mcp.mail.service import MailService

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 20
MAX_BODY_CHARS = 200_000
MAX_ORIGINAL_BYTES = 10 * 1024 * 1024


class MailSender:
    def __init__(self, mail: MailService):
        self.mail = mail

    @staticmethod
    def _addresses(value: list[str] | None, field: str) -> list[str]:
        result = []
        for _, address in getaddresses(value or [], strict=True):
            if (
                not address
                or address.count("@") != 1
                or any(c in address for c in " <>,;")
            ):
                raise ValueError(f"Invalid address in {field}")
            result.append(address)
        return result

    def _original(self, folder: str, uid: int, uid_validity: int) -> EmailMessage:
        if uid <= 0:
            raise ValueError("uid must be positive")
        with self.mail.connection() as client:
            selected = client.select_folder(folder, readonly=True)
            if selected[b"UIDVALIDITY"] != uid_validity:
                raise ValueError("Folder identifiers changed; search again")
            sizes = client.fetch([uid], ["RFC822.SIZE"])
            if uid not in sizes:
                raise ValueError("Message not found")
            if sizes[uid][b"RFC822.SIZE"] > MAX_ORIGINAL_BYTES:
                raise ValueError("Original message exceeds the 10 MiB limit")
            raw = client.fetch([uid], ["BODY.PEEK[]"])
            if uid not in raw or b"BODY[]" not in raw[uid]:
                raise ValueError("Message no longer available; search again")
            return BytesParser(policy=policy.default).parsebytes(raw[uid][b"BODY[]"])

    @staticmethod
    def _text(message: EmailMessage) -> str:
        body = message.get_body(preferencelist=("plain", "html"))
        return body.get_content() if body else ""

    @staticmethod
    def _quote(text: str) -> str:
        return "\n".join("> " + line for line in text.splitlines())

    def _build(
        self,
        to: list[str],
        cc: list[str],
        bcc: list[str],
        subject: str,
        body: str,
    ) -> EmailMessage:
        if not to:
            raise ValueError("At least one recipient is required in to")
        if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
            raise ValueError(f"At most {MAX_RECIPIENTS} recipients are allowed")
        if len(body) > MAX_BODY_CHARS:
            raise ValueError("Body is too long")
        sender = self.mail.settings.mail_username
        message = EmailMessage(policy=policy.SMTP)
        try:
            message["From"] = sender
            message["To"] = ", ".join(to)
            if cc:
                message["Cc"] = ", ".join(cc)
            message["Subject"] = subject
        except ValueError:
            raise ValueError("Headers must not contain line breaks") from None
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
        message.set_content(body)
        return message

    def _deliver(self, message: EmailMessage, recipients: list[str]) -> dict:
        settings = self.mail.settings
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        started = time.perf_counter()
        if settings.mail_smtp_starttls:
            smtp = smtplib.SMTP(
                settings.mail_smtp_host, settings.mail_smtp_port, timeout=30
            )
        else:
            smtp = smtplib.SMTP_SSL(
                settings.mail_smtp_host,
                settings.mail_smtp_port,
                timeout=30,
                context=context,
            )
        with smtp:
            if settings.mail_smtp_starttls:
                smtp.starttls(context=context)
            smtp.login(
                settings.mail_username, settings.mail_password.get_secret_value()
            )
            smtp.send_message(
                message, from_addr=settings.mail_username, to_addrs=recipients
            )
        logger.info(
            "smtp send=%.2fs recipients=%d",
            time.perf_counter() - started,
            len(recipients),
        )
        return {
            "sent": True,
            "message_id": message["Message-ID"],
            "to": message["To"],
            "cc": message.get("Cc"),
            "subject": message["Subject"],
            "saved_to_sent": self._save_copy(message),
        }

    def _save_copy(self, message: EmailMessage) -> bool:
        # The message is already delivered; a failed copy must not look like a failed send.
        try:
            with self.mail.connection() as client:
                folder = (
                    self.mail.settings.mail_sent_folder
                    or client.find_special_folder(SENT)
                    or "Sent"
                )
                client.append(folder, message.as_bytes(), flags=[r"\Seen"])
            return True
        except Exception:
            logger.exception("Sent message could not be saved to the Sent folder")
            return False

    def send_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict:
        to_list = self._addresses(to, "to")
        cc_list = self._addresses(cc, "cc")
        bcc_list = self._addresses(bcc, "bcc")
        message = self._build(to_list, cc_list, bcc_list, subject, body)
        return self._deliver(message, to_list + cc_list + bcc_list)

    def reply_message(
        self,
        folder: str,
        uid: int,
        uid_validity: int,
        body: str,
        reply_all: bool = False,
        cc: list[str] | None = None,
    ) -> dict:
        original = self._original(folder, uid, uid_validity)
        me = self.mail.settings.mail_username.casefold()
        reply_to = original.get_all("Reply-To") or original.get_all("From") or []
        to_list = self._addresses([str(v) for v in reply_to], "original sender")
        cc_list = self._addresses(cc, "cc")
        if reply_all:
            others = [
                str(v) for v in original.get_all("To", []) + original.get_all("Cc", [])
            ]
            for address in self._addresses(others, "original recipients"):
                if address.casefold() != me and address not in to_list + cc_list:
                    cc_list.append(address)
        to_list = [a for a in to_list if a.casefold() != me] or to_list
        subject = str(original.get("Subject", ""))
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        sender = parseaddr(str(original.get("From", "")))[1] or "the sender"
        quoted = self._quote(self._text(original))
        full_body = (
            f"{body}\n\nOn {original.get('Date', '')}, {sender} wrote:\n{quoted}\n"
        )
        message = self._build(to_list, cc_list, [], subject, full_body)
        parent_id = original.get("Message-ID")
        if parent_id:
            message["In-Reply-To"] = str(parent_id)
            message["References"] = " ".join(
                filter(
                    None, [str(original.get("References", "")).strip(), str(parent_id)]
                )
            )
        return self._deliver(message, to_list + cc_list)

    def forward_message(
        self,
        folder: str,
        uid: int,
        uid_validity: int,
        to: list[str],
        body: str = "",
        cc: list[str] | None = None,
    ) -> dict:
        to_list = self._addresses(to, "to")
        cc_list = self._addresses(cc, "cc")
        original = self._original(folder, uid, uid_validity)
        subject = str(original.get("Subject", ""))
        if not subject.lower().startswith(("fwd:", "fw:")):
            subject = f"Fwd: {subject}"
        attachments = [
            part.get_filename() or "unnamed" for part in original.iter_attachments()
        ]
        header = "\n".join(
            f"{name}: {original.get(name, '')}"
            for name in ("From", "Date", "Subject", "To")
        )
        note = (
            f"\n[Attachments not forwarded: {', '.join(attachments)}]"
            if attachments
            else ""
        )
        full_body = (
            f"{body}\n\n---------- Forwarded message ----------\n{header}\n\n"
            f"{self._text(original)}{note}\n"
        )
        message = self._build(to_list, cc_list, [], subject, full_body)
        result = self._deliver(message, to_list + cc_list)
        result["attachments_omitted"] = attachments
        return result
