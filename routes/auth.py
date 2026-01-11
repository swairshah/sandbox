from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from auth.google import verify_google_token, GoogleVerificationError
from auth.jwt import create_token_pair, verify_token, TokenPair

router = APIRouter(prefix="/auth", tags=["authentication"])


class GoogleAuthRequest(BaseModel):
    id_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str | None
    picture: str | None


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenPair


@router.post("/google", response_model=AuthResponse)
async def google_auth(request: GoogleAuthRequest):
    """
    Authenticate with Google ID token.

    Flow:
    1. iOS app signs in with Google SDK
    2. iOS app sends ID token to this endpoint
    3. Backend verifies token with Google
    4. Backend creates user (if new) and returns JWT tokens
    """
    try:
        # Verify the Google ID token
        google_user = verify_google_token(request.id_token)

        # In production, you'd lookup/create user in database here
        # For now, we use Google ID as user ID
        user_id = google_user.google_id

        # Create JWT token pair
        tokens = create_token_pair(user_id, google_user.email)

        return AuthResponse(
            user=UserResponse(
                id=user_id,
                email=google_user.email,
                name=google_user.name,
                picture=google_user.picture,
            ),
            tokens=tokens,
        )

    except GoogleVerificationError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


@router.post("/refresh", response_model=TokenPair)
async def refresh_tokens(request: RefreshRequest):
    """
    Get new access token using refresh token.

    Use this when access token expires (typically every 30 minutes).
    """
    token_data = verify_token(request.refresh_token, expected_type="refresh")

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Issue new token pair
    return create_token_pair(token_data.user_id, token_data.email)


@router.post("/logout")
async def logout():
    """
    Logout endpoint.

    In a stateless JWT system, the client just discards tokens.
    For added security, you could maintain a token blacklist in Redis.
    """
    return {"message": "Logged out successfully"}
