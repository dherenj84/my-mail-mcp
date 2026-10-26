import json
from unittest.mock import MagicMock

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from my_mail_mcp.config import Settings
from my_mail_mcp.mail import service as service_module
from my_mail_mcp.server import create_server

ACCOUNTS = [
    {"id": "a", "username": "a@yahoo.com", "password": "pw-a"},
    {"id": "b", "username": "b@yahoo.com", "password": "pw-b", "imap_host": "imap.b"},
]


def multi(accounts=ACCOUNTS):
    return Settings(_env_file=None, mail_accounts=accounts, mcp_auth_token="x" * 32)


def test_env_json_accounts(monkeypatch):
    monkeypatch.setenv("MAIL_ACCOUNTS", json.dumps(ACCOUNTS))
    monkeypatch.setenv("MCP_AUTH_TOKEN", "x" * 32)
    accounts = Settings(_env_file=None).accounts()
    assert list(accounts) == ["a", "b"]
    assert accounts["a"].imap_host == "imap.mail.yahoo.com"
    assert accounts["b"].imap_host == "imap.b"


@pytest.mark.parametrize(
    "extra",
    [
        {"mail_accounts": ACCOUNTS + [ACCOUNTS[0]]},
        {"mail_accounts": [{**ACCOUNTS[0], "password": "placeholder-x"}]},
        {"mail_accounts": [{**ACCOUNTS[0], "id": "bad id"}]},
        {},
    ],
)
def test_invalid_account_config(extra):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_auth_token="x" * 32, **extra)


def test_account_routing_and_errors(monkeypatch):
    logins = []

    def factory(host, **kwargs):
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.side_effect = lambda user, pw: logins.append((host, user, pw))
        client.list_folders.return_value = [((), b"/", "INBOX")]
        return client

    monkeypatch.setattr(service_module, "IMAPClient", factory)

    async def check():
        async with Client(create_server(multi())) as client:
            listed = (await client.call_tool("list_accounts", {})).data
            assert [(a["id"], a["username"]) for a in listed] == [
                ("a", "a@yahoo.com"),
                ("b", "b@yahoo.com"),
            ]
            assert "pw-a" not in str(listed)
            await client.call_tool("list_folders", {"account": "b"})
            assert logins == [("imap.b", "b@yahoo.com", "pw-b")]
            for args in ({}, {"account": "nope"}):
                with pytest.raises(ToolError, match="choose one of: a, b"):
                    await client.call_tool("list_folders", args)
            with pytest.raises(ToolError, match="choose one of"):
                await client.call_tool(
                    "update_flags",
                    {"folder": "INBOX", "uids": [1], "uid_validity": 1, "read": True},
                )
        assert len(logins) == 1

    import asyncio

    asyncio.run(check())


def test_save_sent_copy_can_be_disabled():
    from my_mail_mcp.mail.sending import MailSender
    from my_mail_mcp.mail.service import MailService

    account = multi([{**ACCOUNTS[0], "save_sent_copy": False}]).accounts()["a"]
    service = MailService(account)
    service.connection = MagicMock(side_effect=AssertionError("must not connect"))
    assert MailSender(service)._save_copy(MagicMock()) is False


def test_connections_per_account_are_limited(monkeypatch):
    import threading
    import time

    from my_mail_mcp.mail.service import MAX_CONNECTIONS, MailService

    monkeypatch.setattr(service_module, "IMAPClient", MagicMock())
    service = MailService(multi().accounts()["a"])
    lock = threading.Lock()
    active = peak = 0

    def work():
        nonlocal active, peak
        with service.connection():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == MAX_CONNECTIONS


def test_busy_mailbox_and_login_failure_are_reported(monkeypatch, caplog):
    from imapclient import IMAPClient

    service = service_module.MailService(multi().accounts()["a"])
    monkeypatch.setattr(service_module, "CONNECTION_WAIT_SECONDS", 0.01)
    for _ in range(service_module.MAX_CONNECTIONS):
        service._slots.acquire()
    with pytest.raises(ValueError, match="busy"), service.connection():
        pass
    for _ in range(service_module.MAX_CONNECTIONS):
        service._slots.release()

    client = MagicMock()
    client.__enter__.return_value = client
    client.login.side_effect = IMAPClient.Error("nope")
    factory = MagicMock(return_value=client)
    factory.Error = IMAPClient.Error
    monkeypatch.setattr(service_module, "IMAPClient", factory)
    with (
        caplog.at_level("WARNING"),
        pytest.raises(IMAPClient.Error),
        service.connection(),
    ):
        pass
    assert "account=a" in caplog.text and "login_failed=" in caplog.text
    # The failed login must not leak a connection slot.
    for _ in range(service_module.MAX_CONNECTIONS):
        assert service._slots.acquire(blocking=False)


def test_missing_accounts_fail():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_auth_token="x" * 32)


def test_accounts_still_validate_their_own_credentials():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            mail_accounts=[{**ACCOUNTS[0], "password": "replace-with-x"}],
            mcp_auth_token="x" * 32,
        )
