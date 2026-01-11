import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .jwt import verify_token, TokenData

# Bearer token security scheme
bearer_scheme = HTTPBearer(auto_error=False)

# Dev mode bypass - only enable when DEV_MODE=1
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)
) -> TokenData:
    """
    Dependency that extracts and validates the JWT from the Authorization header.

    Usage:
        @app.get("/protected")
        async def protected_route(user: TokenData = Depends(get_current_user)):
            return {"email": user.email}
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Dev mode bypass - accept fake dev tokens
    if DEV_MODE and credentials.credentials.startswith("dev_access_token_"):
        return TokenData(
            user_id="dev_user",
            email="dev@example.com",
            exp=None
        )

    token_data = verify_token(credentials.credentials)

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token_data


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)
) -> TokenData | None:
    """
    Optional authentication - returns None if no valid token provided.

    Useful for endpoints that behave differently for authenticated vs anonymous users.
    """
    if credentials is None:
        return None

    return verify_token(credentials.credentials)
