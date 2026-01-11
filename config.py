import os
from functools import lru_cache
from dataclasses import dataclass

# Load .env file for local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not needed in production


# Google OAuth Client IDs
GOOGLE_CLIENT_ID_IOS = "558486289958-glki40l5f7nl1dlvnb9ekhakjn9r2vjc.apps.googleusercontent.com"
GOOGLE_CLIENT_ID_WEB = "558486289958-j768tfatvm4mqkpji50vc3tgo85kf01q.apps.googleusercontent.com"


@dataclass
class Settings:
    google_client_ids: list[str]  # Support multiple client IDs (iOS + Web)
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    host: str = "0.0.0.0"
    port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings(
        google_client_ids=[GOOGLE_CLIENT_ID_IOS, GOOGLE_CLIENT_ID_WEB],
        jwt_secret_key=os.environ.get("JWT_SECRET_KEY", "dev-secret-key"),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        jwt_access_token_expire_minutes=int(os.environ.get("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
        jwt_refresh_token_expire_days=int(os.environ.get("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
