from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    mail_username: str = Field(min_length=1)
    mail_password: SecretStr = Field(min_length=1)
    mail_imap_host: str = "imap.mail.yahoo.com"
    mail_imap_port: int = Field(default=993, ge=1, le=65535)
    mail_archive_folder: str | None = None
    mail_trash_folder: str | None = None
    mail_smtp_host: str = "smtp.mail.yahoo.com"
    mail_smtp_port: int = Field(default=465, ge=1, le=65535)
    mail_smtp_starttls: bool = False
    mcp_auth_token: SecretStr = Field(min_length=32)
    port: int = Field(default=8080, ge=1, le=65535)
    # Both must be set to enable OAuth; the static bearer token keeps working.
    auth_issuer_url: str | None = None
    public_base_url: str | None = None

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

    @field_validator("mail_password", "mcp_auth_token")
    @classmethod
    def require_real_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or raw.casefold().startswith(
            ("placeholder-", "replace-with-")
        ):
            raise ValueError(
                "Replace placeholder credentials with a real secret before starting"
            )
        return value

    @field_validator("mail_username")
    @classmethod
    def require_real_username(cls, value: str) -> str:
        if not value.strip() or value.casefold() == "placeholder@example.invalid":
            raise ValueError("Configure a real mailbox username before starting")
        return value
