from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://marketplace:marketplace@localhost:15432/marketplace"
    jwt_secret: SecretStr = SecretStr("local-development-secret-change-before-deploy-64-chars-long")
    jwt_issuer: str = "marketplace"
    jwt_audience: str = "marketplace-api"
    access_token_minutes: int = Field(default=20, ge=15, le=30)
    refresh_token_days: int = Field(default=14, ge=7, le=30)
    order_cooldown_minutes: int = Field(default=1, ge=0, le=1440)
    log_body_limit: int = Field(default=65536, ge=0)
    max_request_bytes: int = Field(default=262144, ge=1024)
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: SecretStr | None = None


settings = Settings()
