"""
Reverse proxy for sprite preview URLs.

Proxies requests to sprite HTTP servers with authentication, allowing the frontend
to render sprite web apps in iframes without exposing the token.
"""

import os
import httpx
from fastapi import APIRouter, Request, Response, HTTPException, Query

from database import get_database

router = APIRouter()

SPRITE_TOKEN = os.environ.get("SPRITE_TOKEN", "")
SPRITES_API_BASE = "https://api.sprites.dev/v1"


async def get_sprite_name(user_id: str) -> str | None:
    """Get the sprite name for a user from database."""
    db = await get_database()
    user = await db.get_user(user_id)
    if user:
        return user.sprite_name
    return None


async def fetch_sprite_url(sprite_name: str) -> str | None:
    """Fetch the sprite's public URL from the sprites API."""
    if not SPRITE_TOKEN:
        print("[preview] SPRITE_TOKEN not set!")
        return None

    async with httpx.AsyncClient() as client:
        # Try getting sprite info first
        try:
            url = f"{SPRITES_API_BASE}/sprites/{sprite_name}"
            print(f"[preview] fetching sprite info from: {url}")
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {SPRITE_TOKEN}"}
            )
            print(f"[preview] sprite info response: {response.status_code}, body: {response.text[:500]}")
            if response.status_code == 200:
                data = response.json()
                # Try different possible fields
                sprite_url = data.get("url") or data.get("public_url") or data.get("http_url")
                if sprite_url:
                    return sprite_url
        except Exception as e:
            print(f"[preview] failed to fetch sprite info: {e}")

        # Try /url endpoint
        try:
            url = f"{SPRITES_API_BASE}/sprites/{sprite_name}/url"
            print(f"[preview] fetching sprite URL from: {url}")
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {SPRITE_TOKEN}"}
            )
            print(f"[preview] url response: {response.status_code}, body: {response.text}")
            if response.status_code == 200:
                data = response.json()
                return data.get("url")
        except Exception as e:
            print(f"[preview] failed to fetch URL: {e}")

    return None


@router.api_route("/preview/{user_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_to_sprite(user_id: str, path: str, request: Request):
    """Proxy requests to the user's sprite HTTP server with authentication."""

    sprite_name = await get_sprite_name(user_id)
    if not sprite_name:
        raise HTTPException(status_code=404, detail="Sprite not found for user")

    if not SPRITE_TOKEN:
        raise HTTPException(status_code=500, detail="SPRITE_TOKEN not configured")

    # Fetch the sprite URL directly from the API
    sprite_url = await fetch_sprite_url(sprite_name)
    print(f"[preview] user_id={user_id}, sprite_name={sprite_name}, sprite_url={sprite_url}")

    if not sprite_url:
        raise HTTPException(status_code=404, detail="Sprite URL not configured. Run: sprite url -s {sprite_name} update --auth default")

    # Build target URL
    target_url = f"{sprite_url.rstrip('/')}/{path}"
    print(f"[preview] proxying to: {target_url}")

    # Add query params
    if request.query_params:
        target_url += f"?{request.query_params}"

    # Forward headers (except host)
    headers = dict(request.headers)
    headers.pop("host", None)
    headers["Authorization"] = f"Bearer {SPRITE_TOKEN}"

    # Get request body if present
    body = await request.body()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body if body else None,
                follow_redirects=True,
            )

            # Filter response headers
            response_headers = dict(response.headers)
            # Remove hop-by-hop headers and content-length (let Starlette recalculate)
            for header in ["transfer-encoding", "connection", "keep-alive", "content-length", "content-encoding"]:
                response_headers.pop(header, None)

            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Proxy error: {e}")


@router.get("/preview/{user_id}")
async def proxy_root(user_id: str, request: Request):
    """Proxy root path."""
    return await proxy_to_sprite(user_id, "", request)
