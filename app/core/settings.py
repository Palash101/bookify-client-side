from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
from functools import lru_cache
import os
from dotenv import load_dotenv

load_dotenv(".env")


def configure_gcp_credentials() -> None:
    """Point Google client libraries at the service account key from .env."""
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path:
        resolved = os.path.abspath(creds_path)
        if os.path.isfile(resolved):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = resolved


configure_gcp_credentials()


class Settings(BaseSettings):
    PROJECT_NAME: str = "Bookify"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1/client"
    
    # Database
    DB_HOST: str = Field(..., env="DB_HOST")
    DB_PORT: int = Field(..., env="DB_PORT")
    DB_USER: str = Field(..., env="DB_USER")
    DB_PASSWORD: str = Field(..., env="DB_PASSWORD")
    DB_NAME: str = Field(..., env="DB_NAME")
    DATABASE_URL: Optional[str] = Field(default=None, env="DATABASE_URL")
    
    @property
    def database_url(self) -> str:
        """
        Construct DATABASE_URL from individual components if not provided.
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
    
    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-here-change-in-production")
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    # Access JWT lifetime (login / Bearer). Override via ACCESS_TOKEN_EXPIRE_MINUTES in .env
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=1440,
        ge=1,
        description="Access token expiry in minutes (default 24h). Use lower values in production if you prefer short-lived tokens + refresh.",
    )
    # Refresh JWT lifetime
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(
        default=30,
        ge=1,
        description="Refresh token expiry in days",
    )
    
    # CORS (include 127.0.0.1 variants — browser treats localhost vs 127.0.0.1 as different origins)
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://bookify-web-app-fawn.vercel.app"
    ]
    
    # Email
    SMTP_HOST: str = Field(default="smtpout.secureserver.net", env="SMTP_HOST")
    SMTP_PORT: int = Field(default=587, env="SMTP_PORT")
    SMTP_USER: str = Field(default="harendra@craftandcode.in", env="SMTP_USER")
    SMTP_PASSWORD: str = Field(default="Harendra@123", env="SMTP_PASSWORD")
    SMTP_FROM_EMAIL: str = Field(default="harendra@craftandcode.in", env="SMTP_FROM_EMAIL")
    SMTP_FROM_NAME: str = Field(default="Bookify", env="SMTP_FROM_NAME")
    SMTP_USE_TLS: bool = Field(default=True, env="SMTP_USE_TLS")
    
    # After Stripe (etc.) hits /payment/success on this server, user is redirected here (mobile deep link).
    PAYMENT_SUCCESS_DEEP_LINK: str = Field(
        default="bookify://payment/success",
        env="PAYMENT_SUCCESS_DEEP_LINK",
    )

    # Environment
    # Prefer `MODE` if present (used by some modules), otherwise fall back to ENVIRONMENT.
    MODE: str = Field(default=os.getenv("MODE", os.getenv("ENVIRONMENT", "development")), env="MODE")
    ENVIRONMENT: str = Field(default=os.getenv("ENVIRONMENT", "development"), env="ENVIRONMENT")
    GCP_PROJECT_ID: str = Field(default=os.getenv("GCP_PROJECT_ID", ""), env="GCP_PROJECT_ID")
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = Field(
        default=None, env="GOOGLE_APPLICATION_CREDENTIALS"
    )
    # Set false locally when GCP_PROJECT_ID is set but ADC lacks Secret Manager access.
    USE_GCP_SECRET_MANAGER: bool = Field(default=True, env="USE_GCP_SECRET_MANAGER")
    JWT_SIGNING_SECRET_ID: str = Field(
        default="bookify-dev-auth-secret", env="JWT_SIGNING_SECRET_ID"
    )

    # Pub/Sub — one topic for all domain events; consumer fans out internally.
    PUBSUB_TOPIC_ID: str = Field(default="bookify-events", env="PUBSUB_TOPIC_ID")
    PUBSUB_EMULATOR_HOST: Optional[str] = Field(default=None, env="PUBSUB_EMULATOR_HOST")
    PUBSUB_ENABLE_MESSAGE_ORDERING: bool = Field(
        default=False, env="PUBSUB_ENABLE_MESSAGE_ORDERING"
    )
    # Force console publisher even when GCP_PROJECT_ID is set (local dev / tests).
    PUBLISHER_CONSOLE: bool = Field(default=False, env="PUBLISHER_CONSOLE")

    DEBUG: bool = os.getenv("DEBUG", "True").lower() == "true"

    @property
    def publisher_is_console(self) -> bool:
        if self.PUBLISHER_CONSOLE:
            return True
        return not self.GCP_PROJECT_ID
    
    class Config:
        case_sensitive = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Backwards-compatible module-level instance
settings = get_settings()
