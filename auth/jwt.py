from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from pydantic import BaseModel
from config import get_settings


class TokenData(BaseModel):
    user_id: str
    email: str
    token_type: str = "access"


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def create_access_token(user_id: str, email: str) -> str:
    """Create a short-lived access token for API requests."""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_token_expire_minutes)

    payload = {
        "sub": user_id,
        "email": email,
        "type": "access",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }

    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str, email: str) -> str:
    """Create a long-lived refresh token for obtaining new access tokens."""
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_token_expire_days)

    payload = {
        "sub": user_id,
        "email": email,
        "type": "refresh",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }

    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_token_pair(user_id: str, email: str) -> TokenPair:
    """Create both access and refresh tokens."""
    settings = get_settings()
    return TokenPair(
        access_token=create_access_token(user_id, email),
        refresh_token=create_refresh_token(user_id, email),
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


def verify_token(token: str, expected_type: str = "access") -> TokenData | None:
    """
    Verify a JWT token and return the token data.

    Returns None if token is invalid, expired, or wrong type.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )

        # Check token type
        if payload.get("type") != expected_type:
            return None

        user_id = payload.get("sub")
        email = payload.get("email")

        if user_id is None or email is None:
            return None

        return TokenData(user_id=user_id, email=email, token_type=expected_type)

    except JWTError:
        return None
