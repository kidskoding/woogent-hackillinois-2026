from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    db_host: str = "mysql"
    db_port: int = 3306
    db_name: str = "wordpress"
    db_user: str = "wordpress"
    db_password: str = "wordpress"

    # Domain
    wc_domain: str = "http://localhost:8000"
    wp_domain: str = "http://localhost:8080"

    # Security
    secret_key: str = "change-me-before-production-use"

    # OAuth clients — comma-separated "id:secret" pairs
    oauth_client_id: str = "gemini-demo-client"
    oauth_client_secret: str = "gemini-demo-secret"

    # Stripe — set to sk_test_... to enable real test charges
    stripe_test_key: str = ""

    @property
    def db_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def ucp_version(self) -> str:
        return "2026-01-11"


@lru_cache
def get_settings() -> Settings:
    return Settings()
