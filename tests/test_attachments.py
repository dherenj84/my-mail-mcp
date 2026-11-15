import asyncio
import base64
import json
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from imapclient import IMAPClient
from starlette.testclient import TestClient

from my_mail_mcp.config import Settings
from my_mail_mcp.mail.service import MailService
from my_mail_mcp.server import create_server


def settings(**kwargs):
    return Settings(
        _env_file=None,
        mail_accounts=[
            {"id": "a", "username": "test@example.com", "password": "secret"}
        ],
        mcp_auth_token="x" * 32,
        **kwargs,
    )


@pytest.fixture
def mailbox():
    service = MailService(settings().accounts()["a"])
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 5}
    service.connection = MagicMock()
    service.connection.return_value.__enter__.return_value = client
    return service, client


def message(
    data=b"private attachment",
    subtype="pdf",
    filename="invoice.pdf",
    maintype="application",
):
    mail = EmailMessage()
    mail.set_content("Body")
    mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return mail


def fetched(client, mail):
    raw = mail.as_bytes()
    client.fetch.side_effect = [
        {7: {b"RFC822.SIZE": len(raw)}},
        {7: {b"BODY[]<0>": raw}},
    ]
    return raw


@pytest.mark.parametrize(
    "subtype",
    [
        "pdf",
        "zip",
        "octet-stream",
        "vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
def test_binary_roundtrip_and_readonly(mailbox, subtype, caplog):
    service, client = mailbox
    fetched(client, message(subtype=subtype))
    assert service.get_attachment("INBOX", 7, 5, "1") == (
        "invoice.pdf",
        f"application/{subtype}",
        b"private attachment",
    )
    client.select_folder.assert_called_once_with("INBOX", readonly=True)
    assert client.fetch.call_args_list[1].args == ([7], ["BODY.PEEK[]<0.10485761>"])
    assert [call[0] for call in client.method_calls] == [
        "select_folder",
        "fetch",
        "fetch",
    ]
    assert "private attachment" not in caplog.text


@pytest.mark.parametrize(
    "uid,validity,identifier,match",
    [
        (0, 5, "1", "positive"),
        (-1, 5, "1", "positive"),
        (7, 4, "1", "changed"),
        (7, 5, "../secret", "Invalid attachment_id"),
    ],
)
def test_invalid_references(mailbox, uid, validity, identifier, match):
    service, client = mailbox
    with pytest.raises(ValueError, match=match):
        service.get_attachment("INBOX", uid, validity, identifier)
    client.fetch.assert_not_called()


def test_missing_message_and_attachment(mailbox):
    service, client = mailbox
    client.fetch.return_value = {}
    with pytest.raises(ValueError, match="Message not found"):
        service.get_attachment("INBOX", 7, 5, "1")
    fetched(client, message())
    with pytest.raises(ValueError, match="Attachment not found"):
        service.get_attachment("INBOX", 7, 5, "2")
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": 1}}, {}]
    with pytest.raises(ValueError, match="no longer available"):
        service.get_attachment("INBOX", 7, 5, "1")


def test_size_limits_and_inaccurate_imap_size(mailbox):
    service, client = mailbox
    service.attachment_max_bytes = 3
    fetched(client, message(b"123"))
    assert service.get_attachment("INBOX", 7, 5, "1")[2] == b"123"
    fetched(client, message(b"1234"))
    with pytest.raises(ValueError, match="decoded file-size"):
        service.get_attachment("INBOX", 7, 5, "1")
    client.fetch.reset_mock(side_effect=True)
    service.attachment_message_max_bytes = 10
    client.fetch.return_value = {7: {b"RFC822.SIZE": 11}}
    with pytest.raises(ValueError, match="message-size"):
        service.get_attachment("INBOX", 7, 5, "1")
    client.fetch.assert_called_once_with([7], ["RFC822.SIZE"])
    client.fetch.side_effect = [
        {7: {b"RFC822.SIZE": 1}},
        {7: {b"BODY[]<0>": b"x" * 11}},
    ]
    with pytest.raises(ValueError, match="size limit"):
        service.get_attachment("INBOX", 7, 5, "1")
    assert client.fetch.call_args.args == ([7], ["BODY.PEEK[]<0.11>"])


def test_duplicate_unicode_and_unsafe_names_are_metadata_only(mailbox):
    service, client = mailbox
    mail = message(b"one", filename="../café.pdf")
    mail.add_attachment(
        b"two", maintype="application", subtype="pdf", filename="../café.pdf"
    )
    raw = fetched(client, mail)
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": len(raw)}}, {7: {b"BODY[]": raw}}]
    attachments = service.read_message("INBOX", 7, 5)["attachments"]
    assert [a["attachment_id"] for a in attachments] == ["1", "2"]
    assert all(a["filename"] == "../café.pdf" for a in attachments)
    fetched(client, mail)
    assert service.get_attachment("INBOX", 7, 5, "2")[2] == b"two"


def test_nested_inline_unnamed_and_empty_attachment(mailbox):
    service, client = mailbox
    mail = EmailMessage()
    mail.set_content("plain")
    mail.add_alternative('<img src="cid:logo">', subtype="html")
    mail.get_payload()[1].add_related(
        b"logo", maintype="image", subtype="png", cid="logo"
    )
    mail.add_attachment(b"", maintype="application", subtype="pdf")
    # Related image is nested in the body alternative; attachment enumeration must find it.
    attachments = list(service._attachments(mail))
    assert any(p.get_content_type() == "image/png" for _, p in attachments)
    fetched(client, mail)
    empty_id = next(
        i for i, p in attachments if p.get_content_type() == "application/pdf"
    )
    assert service.get_attachment("INBOX", 7, 5, empty_id)[2] == b""


def test_malformed_base64_rejected(mailbox):
    service, client = mailbox
    mail = message()
    mail.get_payload()[1].set_payload("%%%%invalid!!!!")
    fetched(client, mail)
    with pytest.raises(ValueError, match="encoding"):
        service.get_attachment("INBOX", 7, 5, "1")


@pytest.mark.parametrize(
    "mime,data,kind",
    [
        ("application/pdf", b"%PDF-private", "blob"),
        ("image/png", b"\x89PNG\r\n\x1a\n", "image"),
        ("text/plain", b"hello", "text"),
        ("text/plain", b"\xff", "blob"),
    ],
)
def test_http_mcp_content_auth_and_limits(monkeypatch, mime, data, kind, caplog):
    retrieval = MagicMock(return_value=("private-name", mime, data))
    monkeypatch.setattr(MailService, "get_attachment", retrieval)
    app = create_server(settings(attachment_max_bytes=23)).http_app(
        stateless_http=True, json_response=True
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_attachment",
            "arguments": {
                "folder": "INBOX",
                "uid": 7,
                "uid_validity": 5,
                "attachment_id": "1",
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app) as client:
        assert client.post("/mcp/", headers=headers, json=request).status_code == 401
        retrieval.assert_not_called()
        headers["Authorization"] = "Bearer " + "x" * 32
        response = client.post("/mcp/", headers=headers, json=request)
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError", False)
        assert not result.get("structuredContent")  # Avoid duplicating base64.
        assert json.loads(result["content"][0]["text"])["size_bytes"] == len(data)
        block = result["content"][1]
        if kind == "image":
            assert block["type"] == "image"
            assert base64.b64decode(block["data"]) == data
        else:
            assert block["type"] == "resource"
            resource = block["resource"]
            assert resource["mimeType"] == mime
            assert (
                resource["text"] == "hello"
                if kind == "text"
                else base64.b64decode(resource["blob"]) == data
            )
        assert "private-name" not in caplog.text
        assert "PDF-private" not in caplog.text


def test_account_selection_limits_and_safe_errors(monkeypatch):
    calls = []

    def retrieve(self, *args):
        calls.append(
            (
                self.settings.id,
                self.attachment_max_bytes,
                self.attachment_message_max_bytes,
            )
        )
        raise IMAPClient.Error("sensitive attachment content")

    monkeypatch.setattr(MailService, "get_attachment", retrieve)
    configured = settings(attachment_max_bytes=123, attachment_message_max_bytes=456)
    configured.mail_accounts.append(
        configured.mail_accounts[0].model_copy(update={"id": "b"})
    )

    async def check():
        async with Client(create_server(configured)) as client:
            args = {
                "folder": "INBOX",
                "uid": 7,
                "uid_validity": 5,
                "attachment_id": "1",
            }
            for account in (None, "missing"):
                with pytest.raises(ToolError, match="choose one of"):
                    await client.call_tool(
                        "get_attachment",
                        {**args, **({"account": account} if account else {})},
                    )
            assert not calls
            with pytest.raises(ToolError, match="Attachment retrieval failed") as error:
                await client.call_tool("get_attachment", {**args, "account": "b"})
            assert "sensitive" not in str(error.value)

    asyncio.run(check())
    assert calls == [("b", 123, 456)]


@pytest.mark.parametrize(
    "field", ["attachment_max_bytes", "attachment_message_max_bytes"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_invalid_limit_configuration(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


def test_quoted_printable_and_attached_email(mailbox):
    service, client = mailbox
    mail = EmailMessage()
    mail.set_content("Body")
    mail.add_attachment(
        "café", subtype="plain", filename="notes.txt", cte="quoted-printable"
    )
    fetched(client, mail)
    assert service.get_attachment("INBOX", 7, 5, "1")[2].decode().strip() == "café"
    attached = EmailMessage()
    attached["Subject"] = "Attached subject"
    attached.set_content("Attached body")
    mail.add_attachment(attached, filename="forwarded.eml")
    fetched(client, mail)
    filename, mime, data = service.get_attachment("INBOX", 7, 5, "2")
    assert (filename, mime) == ("forwarded.eml", "message/rfc822")
    assert b"Attached subject" in data and b"Attached body" in data


def test_unsupported_transfer_encoding(mailbox):
    service, client = mailbox
    mail = message()
    mail.get_payload()[1].replace_header("Content-Transfer-Encoding", "x-unknown")
    fetched(client, mail)
    with pytest.raises(ValueError, match="encoding"):
        service.get_attachment("INBOX", 7, 5, "1")


def test_message_limit_exact_boundary(mailbox):
    service, client = mailbox
    raw = fetched(client, message())
    service.attachment_message_max_bytes = len(raw)
    assert service.get_attachment("INBOX", 7, 5, "1")[2] == b"private attachment"


def test_limits_from_environment(monkeypatch):
    monkeypatch.setenv("ATTACHMENT_MAX_BYTES", "123")
    monkeypatch.setenv("ATTACHMENT_MESSAGE_MAX_BYTES", "456")
    configured = settings()
    assert configured.attachment_max_bytes == 123
    assert configured.attachment_message_max_bytes == 456
