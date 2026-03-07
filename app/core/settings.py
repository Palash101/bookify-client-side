from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
import os
from dotenv import load_dotenv

load_dotenv(".env.dev")


class Settings(BaseSettings):
    PROJECT_NAME: str = "Bookify"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Database
    DB_HOST: str = Field(default="34.55.176.77", env="DB_HOST")
    DB_PORT: int = Field(default=5432, env="DB_PORT")
    DB_USER: str = Field(default="postgres", env="DB_USER")
    DB_PASSWORD: str = Field(default="Bookify#1234", env="DB_PASSWORD")
    DB_NAME: str = Field(default="bookify_dev", env="DB_NAME")
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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
    
    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]
    
    # Email
    SMTP_HOST: str = Field(default="smtpout.secureserver.net", env="SMTP_HOST")
    SMTP_PORT: int = Field(default=587, env="SMTP_PORT")
    SMTP_USER: str = Field(default="harendra@craftandcode.in", env="SMTP_USER")
    SMTP_PASSWORD: str = Field(default="Harendra@123", env="SMTP_PASSWORD")
    SMTP_FROM_EMAIL: str = Field(default="harendra@craftandcode.in", env="SMTP_FROM_EMAIL")
    SMTP_FROM_NAME: str = Field(default="Bookify", env="SMTP_FROM_NAME")
    SMTP_USE_TLS: bool = Field(default=True, env="SMTP_USE_TLS")
    
    # Environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    DEBUG: bool = os.getenv("DEBUG", "True").lower() == "true"
    
    class Config:
        case_sensitive = True


settings = Settings()
