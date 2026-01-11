from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any
from typing import Optional
from datetime import datetime, timezone
import random
import os
from auth.middleware import get_current_user
from auth.jwt import TokenData

# Use sandbox_manager on Modal, sessions locally
IS_MODAL = os.environ.get("MODAL_ENVIRONMENT") is not None

if IS_MODAL:
    import sandbox_manager
    async def get_response(message: str, user_id: str, session_id: str | None = None):
        return await sandbox_manager.send_message(user_id, message)
    async def clear_session(user_id: str):
        return await sandbox_manager.clear_session(user_id)
else:
    from sessions import get_response, clear_session

router = APIRouter(prefix="/api", tags=["chat"])


class ChatMessage(BaseModel):
    content: str


class ToolEvent(BaseModel):
    type: str
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: Any | None = None
    is_error: bool | None = None


class ChatResponse(BaseModel):
    session_id: Optional[str] = None
    id: str
    content: str
    tool_events: list[ToolEvent] = []
    timestamp: str
    user_email: str


@router.post("/chat", response_model=ChatResponse)
async def chat(
    message: ChatMessage,
    user: TokenData = Depends(get_current_user),
    session_id: str | None = None
):
    """Protected chat endpoint with conversation history."""
    try:
        response_text, session_id, tool_events = await get_response(
            message.content, user.user_id, session_id
        )

        if not response_text:
            response_text = "I couldn't generate a response. Please try again."

        return ChatResponse(
            session_id=session_id,
            id=f"msg_{random.randint(100000, 999999)}",
            content=response_text,
            tool_events=tool_events,
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_email=user.email,
        )

    except Exception as e:
        print(f"Claude SDK error: {e}")
        await clear_session(user.user_id)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get response: {str(e)}"
        )


@router.post("/chat/clear")
async def clear_ios_chat(user: TokenData = Depends(get_current_user)):
    """Clear chat history for authenticated iOS user."""
    await clear_session(user.user_id)
    return {"status": "cleared", "user_id": user.user_id}


@router.get("/chat/history")
async def get_chat_history(
    user: TokenData = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0,
):
    """Get chat history for the authenticated user."""
    return {
        "messages": [],
        "total": 0,
        "limit": limit,
        "offset": offset,
        "user_id": user.user_id,
    }


@router.get("/session")
async def get_session(user: TokenData = Depends(get_current_user)):
    """Get current session info."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "authenticated": True,
    }
