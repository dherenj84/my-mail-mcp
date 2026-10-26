import asyncio
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from pydantic import ValidationError

from my_mail_mcp.config import Settings
from my_mail_mcp.mail.service import MailService
from my_mail_mcp.server import create_server


def settings():
    return Settings(
        _env_file=None,
        mail_username="test@yahoo.com",
        mail_password="private-mail-password",
        mcp_auth_token="x" * 32,
    )


def test_missing_token_fails(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mail_username="test@yahoo.com", mail_password="secret")


def test_secrets_are_redacted():
    assert "private-mail-password" not in repr(settings())


def test_stale_uid_rejected_before_fetch(monkeypatch):
    service = MailService(settings().accounts()["default"])
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 2}
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(service, "connection", lambda: context)
    with pytest.raises(ValueError, match="search again"):
        service.read_message("INBOX", 1, 1)
    client.fetch.assert_not_called()
    client.select_folder.assert_called_once_with("INBOX", readonly=True)


def test_mcp_discovery():
    async def check():
        async with Client(create_server(settings())) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == {
                "list_accounts",
                "list_folders",
                "search_messages",
                "read_message",
                "update_flags",
                "move_messages",
                "copy_messages",
                "archive_messages",
                "trash_messages",
                "restore_messages",
                "permanently_delete_messages",
                "create_folder",
                "rename_folder",
                "delete_folder",
                "send_message",
                "reply_message",
                "forward_message",
            }
            read_tools = {
                "list_accounts",
                "list_folders",
                "search_messages",
                "read_message",
            }
            assert all(
                tool.annotations.read_only_hint == (tool.name in read_tools)
                for tool in tools
            )
            by_name = {tool.name: tool for tool in tools}
            assert by_name["permanently_delete_messages"].annotations.destructive_hint
            assert not by_name["copy_messages"].annotations.idempotent_hint

    asyncio.run(check())


def test_http_rejects_missing_credentials():
    from starlette.testclient import TestClient

    app = create_server(settings()).http_app(stateless_http=True)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/mcp/", json={}).status_code == 401


def test_authenticated_http_discovery_and_tool_call(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(
        MailService, "list_folders", lambda self: [{"name": "INBOX", "flags": []}]
    )
    app = create_server(settings()).http_app(stateless_http=True, json_response=True)
    headers = {
        "Authorization": "Bearer " + "x" * 32,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app) as client:
        invalid = dict(headers, Authorization="Bearer invalid")
        assert client.post("/mcp/", headers=invalid, json={}).status_code == 401
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "regression-test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert "result" in response.json()
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 17
        response = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_folders",
                    "arguments": {},
                },
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result.get("isError", False)
        assert "INBOX" in str(result)


def test_imap_uses_verified_certificate_context(monkeypatch):
    import ssl

    from my_mail_mcp.mail import service as module

    factory = MagicMock()
    monkeypatch.setattr(module, "IMAPClient", factory)
    with MailService(settings().accounts()["default"]).connection():
        pass
    context = factory.call_args.kwargs["ssl_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.cert_store_stats()["x509_ca"] > 0


def test_mcp_mutation_dispatch_and_error_handling(monkeypatch):
    from imapclient import IMAPClient

    from my_mail_mcp.mail.operations import MailboxOperations

    async def check():
        operation = MagicMock(return_value={"operation": "update_flags"})
        monkeypatch.setattr(MailboxOperations, "update_flags", operation)
        async with Client(create_server(settings())) as client:
            result = await client.call_tool(
                "update_flags",
                {"folder": "INBOX", "uids": [7], "uid_validity": 5, "read": True},
            )
            assert not result.is_error
            operation.assert_called_once_with("INBOX", [7], 5, True, None, None)
            operation.side_effect = ValueError(
                "Folder identifiers changed; search again"
            )
            result = await client.call_tool(
                "update_flags",
                {"folder": "INBOX", "uids": [7], "uid_validity": 4, "read": True},
                raise_on_error=False,
            )
            assert result.is_error
            assert "search again" in str(result.content)
            operation.side_effect = IMAPClient.Error("private-server-details")
            result = await client.call_tool(
                "update_flags",
                {"folder": "INBOX", "uids": [7], "uid_validity": 5, "read": True},
                raise_on_error=False,
            )
            assert result.is_error
            assert "Changes may already have applied" in str(result.content)
            assert "private-server-details" not in str(result.content)

    asyncio.run(check())


def test_http_mutation_requires_authentication(monkeypatch):
    from starlette.testclient import TestClient

    from my_mail_mcp.mail.operations import MailboxOperations

    operation = MagicMock(return_value={"operation": "update_flags"})
    monkeypatch.setattr(MailboxOperations, "update_flags", operation)
    app = create_server(settings()).http_app(stateless_http=True, json_response=True)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "update_flags",
            "arguments": {
                "folder": "INBOX",
                "uids": [7],
                "uid_validity": 5,
                "starred": True,
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app) as client:
        assert client.post("/mcp/", json=request, headers=headers).status_code == 401
        operation.assert_not_called()
        headers["Authorization"] = "Bearer " + "x" * 32
        response = client.post("/mcp/", json=request, headers=headers)
        assert response.status_code == 200
        assert not response.json()["result"].get("isError", False)
        operation.assert_called_once_with("INBOX", [7], 5, None, True, None)


ISSUER = "https://tenant.example.us.auth0.com/"
BASE_URL = "https://mail.example.run.app"


def oauth_settings():
    return Settings(
        _env_file=None,
        mail_username="test@yahoo.com",
        mail_password="private-mail-password",
        mcp_auth_token="x" * 32,
        auth_issuer_url=ISSUER,
        public_base_url=BASE_URL,
    )


def test_oauth_settings_must_be_complete_and_https():
    base = {
        "_env_file": None,
        "mail_username": "test@yahoo.com",
        "mail_password": "private-mail-password",
        "mcp_auth_token": "x" * 32,
    }
    with pytest.raises(ValidationError):
        Settings(**base, auth_issuer_url=ISSUER)
    with pytest.raises(ValidationError):
        Settings(**base, auth_issuer_url="http://a/", public_base_url="http://b")


def test_oauth_jwt_validation_and_static_fallback(monkeypatch):
    from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair
    from starlette.testclient import TestClient

    from my_mail_mcp import server as module

    keys = RSAKeyPair.generate()
    monkeypatch.setattr(
        module,
        "JWTVerifier",
        lambda **kw: JWTVerifier(
            public_key=keys.public_key,
            **{k: v for k, v in kw.items() if k != "jwks_uri"},
        ),
    )
    app = create_server(oauth_settings()).http_app(
        stateless_http=True, json_response=True
    )
    audience = BASE_URL + "/mcp"

    def call(token):
        return client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        ).status_code

    good = keys.create_token(subject="user", issuer=ISSUER, audience=audience)
    wrong_audience = keys.create_token(
        subject="user", issuer=ISSUER, audience="https://other.example/mcp"
    )
    wrong_issuer = keys.create_token(
        subject="user", issuer="https://evil.example/", audience=audience
    )
    expired = keys.create_token(
        subject="user", issuer=ISSUER, audience=audience, expires_in_seconds=-60
    )
    forged = RSAKeyPair.generate().create_token(
        subject="user", issuer=ISSUER, audience=audience
    )
    with TestClient(app) as client:
        assert call(good) == 200
        assert call("x" * 32) == 200
        for bad in (wrong_audience, wrong_issuer, expired, forged, "garbage"):
            assert call(bad) == 401
        assert client.post("/mcp/", json={}).status_code == 401
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        body = metadata.json()
        assert body["resource"] == audience
        assert [s.rstrip("/") for s in body["authorization_servers"]] == [
            ISSUER.rstrip("/")
        ]
