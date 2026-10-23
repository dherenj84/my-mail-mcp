from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from my_mail_mcp.config import Settings
from my_mail_mcp.mail.service import MailService


@pytest.fixture
def mailbox(monkeypatch):
    service = MailService(
        Settings(
            _env_file=None,
            mail_username="test@example.com",
            mail_password="secret",
            mcp_auth_token="x" * 32,
        )
    )
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 5}
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(service, "connection", lambda: context)
    return service, client


def test_search_pagination_and_encoded_subject(mailbox):
    service, client = mailbox
    client.search.return_value = [1, 3, 2]
    client.fetch.return_value = {
        2: {b"ENVELOPE": SimpleNamespace(subject=b"=?utf-8?b?Y2Fmw6k=?="), b"FLAGS": ()}
    }
    result = service.search_messages("INBOX", "invoice", 1, 1)
    assert result["messages"] == [{"uid": 2, "subject": "caf\u00e9", "flags": []}]
    assert result["total"] == 3
    assert result["next_offset"] == 2
    assert result["uid_validity"] == 5
    client.select_folder.assert_called_once_with("INBOX", readonly=True)
    client.search.assert_called_once_with(["TEXT", "invoice"], charset="UTF-8")
    client.fetch.assert_called_once_with([2], ["ENVELOPE", "FLAGS"])


def test_empty_search_does_not_fetch(mailbox):
    service, client = mailbox
    client.search.return_value = []
    result = service.search_messages("INBOX", "", 20, 0)
    assert result["messages"] == []
    assert result["next_offset"] is None
    client.fetch.assert_not_called()


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (1, -1)])
def test_invalid_pagination(mailbox, limit, offset):
    service, client = mailbox
    with pytest.raises(ValueError):
        service.search_messages("INBOX", "", limit, offset)
    client.select_folder.assert_not_called()


def test_read_uses_peek_and_returns_attachment_metadata(mailbox):
    service, client = mailbox
    message = EmailMessage()
    message["Subject"] = "Hello"
    message.set_content("Body")
    message.add_attachment(
        b"example", maintype="application", subtype="pdf", filename="test.pdf"
    )
    raw = message.as_bytes()
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": len(raw)}}, {7: {b"BODY[]": raw}}]
    result = service.read_message("INBOX", 7, 5)
    assert result["body"].strip() == "Body"
    assert not result["body_truncated"]
    assert result["attachments"] == [
        {"filename": "test.pdf", "content_type": "application/pdf"}
    ]
    assert client.fetch.call_args_list[1].args == ([7], ["BODY.PEEK[]"])
    client.select_folder.assert_called_once_with("INBOX", readonly=True)


def test_oversize_message_rejected_before_body_download(mailbox):
    service, client = mailbox
    client.fetch.return_value = {7: {b"RFC822.SIZE": 11 * 1024 * 1024}}
    with pytest.raises(ValueError, match="10 MiB"):
        service.read_message("INBOX", 7, 5)
    client.fetch.assert_called_once_with([7], ["RFC822.SIZE"])


def test_message_removed_between_fetches(mailbox):
    service, client = mailbox
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": 123}}, {}]
    with pytest.raises(ValueError, match="no longer available"):
        service.read_message("INBOX", 7, 5)


def test_validation_errors_do_not_disclose_input():
    with pytest.raises(ValueError) as error:
        Settings(
            _env_file=None,
            mail_username="test@example.com",
            mail_password="private-password",
            mcp_auth_token="short-secret",
        )
    assert "short-secret" not in str(error.value)
    assert "private-password" not in str(error.value)


def test_search_handles_disappearing_message(mailbox):
    service, client = mailbox
    client.search.return_value = [7]
    client.fetch.return_value = {}
    result = service.search_messages("INBOX", "", 20, 0)
    assert result["total"] == 1
    assert result["messages"] == []
    assert result["unavailable_uids"] == [7]


def test_subject_decodes_raw_utf8():
    assert MailService._subject("caf\u00e9".encode()) == "caf\u00e9"


def test_unknown_subject_charset_does_not_break_search():
    assert MailService._subject(b"=?unknown-charset?b?SGVsbG8=?=")


@pytest.mark.parametrize(
    "field,value",
    [
        ("mail_password", "PLACEHOLDER-REPLACE-WITH-YAHOO-APP-PASSWORD"),
        ("mail_password", "replace-with-yahoo-app-password"),
        ("mail_password", "   "),
        ("mcp_auth_token", "PLACEHOLDER-REPLACE-WITH-A-SECURE-RANDOM-MCP-TOKEN"),
        ("mcp_auth_token", "replace-with-a-long-random-secret"),
        ("mail_username", "placeholder@example.invalid"),
        ("mail_username", "   "),
    ],
)
def test_placeholder_credentials_fail_closed(field, value):
    data = {
        "mail_username": "test@example.com",
        "mail_password": "secret",
        "mcp_auth_token": "x" * 32,
    }
    data[field] = value
    with pytest.raises(ValueError):
        Settings(_env_file=None, **data)
