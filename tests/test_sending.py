from unittest.mock import MagicMock

import pytest
from imapclient.imapclient import SENT

from my_mail_mcp.config import Settings
from my_mail_mcp.mail.sending import MailSender
from my_mail_mcp.mail.service import MailService

ORIGINAL = (
    b"From: Alice <alice@example.org>\r\nTo: test@example.com, bob@example.org\r\n"
    b"Cc: carol@example.org\r\nSubject: Lunch\r\nDate: Mon, 1 Jan 2026 10:00:00 +0000\r\n"
    b"Message-ID: <orig@example.org>\r\n\r\nShall we meet?\r\n"
)


@pytest.fixture
def sender(monkeypatch):
    mail = MailService(
        Settings(
            _env_file=None,
            mail_username="test@example.com",
            mail_password="secret",
            mcp_auth_token="x" * 32,
        )
    )
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 5}
    client.fetch.side_effect = lambda uids, items: (
        {7: {b"RFC822.SIZE": len(ORIGINAL)}}
        if items == ["RFC822.SIZE"]
        else {7: {b"BODY[]": ORIGINAL}}
    )
    client.find_special_folder.side_effect = lambda flag: (
        "Sent Items" if flag == SENT else None
    )
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(mail, "connection", lambda: context)
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    monkeypatch.setattr(
        "my_mail_mcp.mail.sending.smtplib.SMTP_SSL", lambda *a, **k: smtp
    )
    return MailSender(mail), smtp, client


def test_send_delivers_and_saves_copy(sender):
    sending, smtp, client = sender
    result = sending.send_message(
        ["a@example.org"], "Hi", "Body", cc=["c@example.org"], bcc=["b@example.org"]
    )
    smtp.login.assert_called_once_with("test@example.com", "secret")
    message = smtp.send_message.call_args.args[0]
    assert smtp.send_message.call_args.kwargs["to_addrs"] == [
        "a@example.org",
        "c@example.org",
        "b@example.org",
    ]
    assert message["Bcc"] is None
    assert message["From"] == "test@example.com"
    assert client.append.call_args.args[0] == "Sent Items"
    assert result["sent"] and result["saved_to_sent"]


def test_sent_copy_failure_does_not_fail_send(sender):
    sending, _, client = sender
    client.append.side_effect = OSError("boom")
    assert (
        sending.send_message(["a@example.org"], "Hi", "Body")["saved_to_sent"] is False
    )


@pytest.mark.parametrize(
    "to", [[], ["not-an-address"], ["a@b.c\r\nBcc: x@y.z"], ["a@b.c", "d@e"] * 11]
)
def test_send_rejects_bad_recipients(sender, to):
    sending, smtp, _ = sender
    with pytest.raises(ValueError):
        sending.send_message(to, "Hi", "Body")
    smtp.send_message.assert_not_called()


def test_send_rejects_header_injection_in_subject(sender):
    sending, smtp, _ = sender
    with pytest.raises(ValueError):
        sending.send_message(["a@example.org"], "Hi\r\nBcc: evil@example.org", "Body")
    smtp.send_message.assert_not_called()


def test_reply_threads_and_quotes(sender):
    sending, smtp, _ = sender
    sending.reply_message("INBOX", 7, 5, "Yes!")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "alice@example.org"
    assert message["Cc"] is None
    assert message["Subject"] == "Re: Lunch"
    assert message["In-Reply-To"] == "<orig@example.org>"
    assert message["References"] == "<orig@example.org>"
    assert "> Shall we meet?" in message.get_content()


def test_reply_all_excludes_self(sender):
    sending, smtp, _ = sender
    sending.reply_message("INBOX", 7, 5, "Yes!", reply_all=True)
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "alice@example.org"
    assert message["Cc"] == "bob@example.org, carol@example.org"


def test_reply_rejects_stale_uid_validity(sender):
    sending, smtp, _ = sender
    with pytest.raises(ValueError, match="Folder identifiers changed"):
        sending.reply_message("INBOX", 7, 6, "Yes!")
    smtp.send_message.assert_not_called()


def test_forward_includes_original(sender):
    sending, smtp, _ = sender
    result = sending.forward_message("INBOX", 7, 5, ["d@example.org"], "FYI")
    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Fwd: Lunch"
    assert "Forwarded message" in message.get_content()
    assert "Shall we meet?" in message.get_content()
    assert result["attachments_omitted"] == []
