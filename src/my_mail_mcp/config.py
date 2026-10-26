import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _real_secret(value: SecretStr | None) -> SecretStr | None:
    if value is None:
        return value
    raw = value.get_secret_value()
    if not raw.strip() or raw.casefold().startswith(("placeholder-", "replace-with-")):
        raise ValueError(
            "Replace placeholder credentials with a real secret before starting"
        )
    return value


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = "default"
    username: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)
    imap_host: str = "imap.mail.yahoo.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    archive_folder: str | None = None
    trash_folder: str | None = None
    sent_folder: str | None = None
    save_sent_copy: bool = True
    smtp_host: str = "smtp.mail.yahoo.com"
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_starttls: bool = False

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise ValueError("Account id must be 1-64 letters, digits, _, . or -")
        return value

    @field_validator("password")
    @classmethod
    def require_real_password(cls, value: SecretStr) -> SecretStr:
        return _real_secret(value)

    @field_validator("username")
    @classmethod
    def require_real_username(cls, value: str) -> str:
        if not value.strip() or value.casefold() == "placeholder@example.invalid":
            raise ValueError("Configure a real mailbox username before starting")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    # JSON list of mailboxes; see the README "Multiple mailboxes" section.
    mail_accounts: list[Account] = []
    mcp_auth_token: SecretStr = Field(min_length=32)
    port: int = Field(default=8080, ge=1, le=65535)
    # Both must be set to enable OAuth; the static bearer token keeps working.
    auth_issuer_url: str | None = None
    public_base_url: str | None = None

    @model_validator(mode="after")
    def require_mailboxes(self):
        if not self.mail_accounts:
            raise ValueError("Configure at least one mailbox in MAIL_ACCOUNTS")
        ids = [account.id for account in self.mail_accounts]
        if len(set(ids)) != len(ids):
            raise ValueError("MAIL_ACCOUNTS ids must be unique")
        return self

    def accounts(self) -> dict[str, Account]:
        return {account.id: account for account in self.mail_accounts}

    @model_validator(mode="after")
    def require_complete_oauth_settings(self):
        if bool(self.auth_issuer_url) != bool(self.public_base_url):
            raise ValueError(
                "Set both AUTH_ISSUER_URL and PUBLIC_BASE_URL to enable OAuth, or neither"
            )
        for url in (self.auth_issuer_url, self.public_base_url):
            if url and not url.startswith("https://"):
                raise ValueError("OAuth URLs must use https")
        return self

    @field_validator("mcp_auth_token")
    @classmethod
    def require_real_secret(cls, value: SecretStr) -> SecretStr:
        return _real_secret(value)
