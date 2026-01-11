from google.oauth2 import id_token
from google.auth.transport import requests
from pydantic import BaseModel
from config import get_settings


class GoogleUser(BaseModel):
    email: str
    name: str | None = None
    picture: str | None = None
    google_id: str


class GoogleVerificationError(Exception):
    pass


def verify_google_token(token: str) -> GoogleUser:
    """
    Verify a Google ID token and extract user information.

    This verifies:
    - Token signature (using Google's public keys)
    - Token expiry
    - Token audience (matches one of our client IDs - iOS or Web)
    - Token issuer (accounts.google.com)
    """
    settings = get_settings()

    # Try each client ID (iOS and Web use different client IDs)
    last_error = None
    for client_id in settings.google_client_ids:
        try:
            # Verify the token with Google's API
            idinfo = id_token.verify_oauth2_token(
                token,
                requests.Request(),
                client_id
            )

            # Verify issuer
            if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
                raise GoogleVerificationError("Invalid token issuer")

            # Extract user info
            return GoogleUser(
                email=idinfo["email"],
                name=idinfo.get("name"),
                picture=idinfo.get("picture"),
                google_id=idinfo["sub"]
            )

        except ValueError as e:
            last_error = e
            continue

    raise GoogleVerificationError(f"Invalid token: {str(last_error)}")
