from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
import asyncio
import signal
import os
import json
import uuid
from config import get_settings
from routes import auth_router, chat_router
from routes.files import router as files_router
from routes.preview import router as preview_router
from terminal import terminal_session
from remote_files import RemoteFileManager
from sprite_sessions import get_session_manager, cleanup_session_manager
from database import close_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - start/stop services."""
    # Startup
    # Initialize sprite session manager
    await get_session_manager()

    yield

    # Shutdown
    print("Shutting down...")
    try:
        await asyncio.wait_for(cleanup_session_manager(), timeout=5.0)
    except asyncio.TimeoutError:
        print("Session cleanup timed out")
    try:
        await asyncio.wait_for(close_database(), timeout=2.0)
    except asyncio.TimeoutError:
        print("Database close timed out")
    print("Shutdown complete")


app = FastAPI(
    title="Monios API",
    description="Backend API for Monios chat application",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(files_router)
app.include_router(preview_router, prefix="/api")


# Public chat endpoint for web UI (no auth required)
class WebChatRequest(BaseModel):
    message: str
    user_id: str = "guest"


@app.post("/chat")
async def web_chat(request: WebChatRequest):
    """Public chat endpoint for web UI."""
    try:
        manager = await get_session_manager()
        response_text, tool_uses = await manager.chat(request.user_id, request.message)

        if not response_text:
            return {"content": "No response generated (empty result)", "user_id": request.user_id}

        return {
            "content": response_text,
            "user_id": request.user_id,
            "tool_events": tool_uses,
        }

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Chat error: {error_details}")
        # Cleanup session on error
        manager = await get_session_manager()
        await manager.cleanup_session(request.user_id)
        return {"content": f"Error: {type(e).__name__}: {str(e)}", "user_id": request.user_id}


@app.post("/chat/clear")
async def clear_chat(request: WebChatRequest):
    """Start a new conversation for a user."""
    manager = await get_session_manager()
    await manager.new_conversation(request.user_id)
    return {"status": "cleared", "user_id": request.user_id}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/sprite/{user_id}")
async def get_sprite_info(user_id: str):
    """Get sprite info for a user (for iframe previews)."""
    manager = await get_session_manager()
    session = await manager.get_or_create_session(user_id)
    user = await manager.db.get_user(user_id)

    return {
        "user_id": user_id,
        "sprite_name": session.sprite_name,
        "sprite_url": user.sprite_url if user else None,
    }


@app.get("/api/conversations/{user_id}")
async def get_user_conversations(user_id: str, limit: int = 10):
    """Get a user's conversation list."""
    manager = await get_session_manager()
    conversations = await manager.db.get_user_conversations(user_id, limit)
    return {
        "conversations": [
            {
                "id": c.id,
                "session_id": c.session_id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in conversations
        ]
    }


# WebSocket endpoint for real-time chat
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for real-time chat with streaming responses.

    Client sends JSON messages:
    - {"type": "connect", "user_id": "..."}
    - {"type": "message", "content": "...", "message_id": "..."}
    - {"type": "history"} - get conversation history
    - {"type": "new_conversation"} - start fresh conversation

    Server sends JSON responses:
    - {"type": "connected", "user_id": "...", "sprite_name": "..."}
    - {"type": "processing", "message_id": "..."}
    - {"type": "text", "message_id": "...", "content": "..."}
    - {"type": "tool_use", "message_id": "...", "name": "...", "input": {...}}
    - {"type": "tool_result", "message_id": "...", "content": "...", "is_error": bool}
    - {"type": "done", "message_id": "...", "content": "...", "tool_uses": [...]}
    - {"type": "error", "message_id": "...", "error": "..."}
    - {"type": "history", "messages": [...]}
    """
    await websocket.accept()
    user_id: str | None = None
    manager = await get_session_manager()

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "Invalid JSON"})
                continue

            msg_type = msg.get("type")

            if msg_type == "connect":
                user_id = msg.get("user_id", f"guest_{uuid.uuid4().hex[:8]}")
                session = await manager.get_or_create_session(user_id)

                await websocket.send_json({
                    "type": "connected",
                    "user_id": user_id,
                    "sprite_name": session.sprite_name,
                })

            elif msg_type == "message":
                if not user_id:
                    await websocket.send_json({
                        "type": "error",
                        "error": "Not connected. Send connect message first."
                    })
                    continue

                content = msg.get("content", "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "error": "Empty message"})
                    continue

                message_id = msg.get("message_id", f"msg_{uuid.uuid4().hex[:8]}")

                # Notify client we're processing
                await websocket.send_json({"type": "processing", "message_id": message_id})

                try:
                    # Stream responses back to client
                    async def send_text(t):
                        print(f"[ws] sending text: {t[:30]}...")
                        await websocket.send_json({"type": "text", "message_id": message_id, "content": t})

                    async def send_tool_use(t):
                        print(f"[ws] sending tool_use: {t['name']}")
                        await websocket.send_json({"type": "tool_use", "message_id": message_id, **t})

                    async def send_tool_result(r):
                        print(f"[ws] sending tool_result")
                        await websocket.send_json({
                            "type": "tool_result",
                            "message_id": message_id,
                            "content": r.content,
                            "is_error": r.is_error
                        })

                    response_text, tool_uses = await manager.chat(
                        user_id=user_id,
                        message=content,
                        on_text=lambda t: asyncio.create_task(send_text(t)),
                        on_tool_use=lambda t: asyncio.create_task(send_tool_use(t)),
                        on_tool_result=lambda r: asyncio.create_task(send_tool_result(r)),
                    )

                    print(f"[ws] sending response message")
                    await websocket.send_json({
                        "type": "response",
                        "message_id": message_id,
                        "content": response_text,
                        "tool_events": tool_uses,
                    })
                    print(f"[ws] response message sent")

                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "message_id": message_id,
                        "error": str(e)
                    })

            elif msg_type == "history":
                if not user_id:
                    await websocket.send_json({"type": "error", "error": "Not connected"})
                    continue

                messages = await manager.get_conversation_history(user_id)
                await websocket.send_json({
                    "type": "history",
                    "messages": [
                        {
                            "role": m.role,
                            "content": m.content,
                            "tool_uses": m.tool_uses,
                            "created_at": m.created_at.isoformat() if hasattr(m.created_at, 'isoformat') else m.created_at
                        }
                        for m in messages
                    ]
                })

            elif msg_type == "new_conversation":
                if not user_id:
                    await websocket.send_json({"type": "error", "error": "Not connected"})
                    continue

                await manager.new_conversation(user_id)
                await websocket.send_json({"type": "conversation_cleared"})

            else:
                await websocket.send_json({
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}"
                })

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for user: {user_id}")
    except Exception as e:
        print(f"WebSocket error: {e}")


# WebSocket endpoint for real-time file system updates
@app.websocket("/ws/files")
async def websocket_files(websocket: WebSocket):
    """
    WebSocket endpoint for remote file system browsing via sprite.

    Protocol:
    - First message must be JSON: {"type": "connect", "user_id": "..."}
    - Client sends JSON: {"type": "get_tree", "path": "..."}
    - Client sends JSON: {"type": "refresh"}

    Server sends JSON responses:
    - {"type": "connected", "sprite_name": "..."}
    - {"type": "tree", "data": {...}}
    - {"type": "error", "error": "..."}
    """
    await websocket.accept()
    file_manager: RemoteFileManager | None = None

    try:
        # First message should be connect with user_id
        first_msg = await websocket.receive_text()
        try:
            msg = json.loads(first_msg)
            if msg.get("type") != "connect" or not msg.get("user_id"):
                await websocket.send_json({
                    "type": "error",
                    "error": "First message must be connect with user_id"
                })
                return

            user_id = msg["user_id"]
        except json.JSONDecodeError:
            await websocket.send_json({"type": "error", "error": "Invalid JSON"})
            return

        # Get user's sprite
        manager = await get_session_manager()
        session = await manager.get_or_create_session(user_id)
        file_manager = RemoteFileManager(session.sprite)

        await websocket.send_json({
            "type": "connected",
            "sprite_name": session.sprite_name
        })

        # Send initial directory tree
        try:
            tree = file_manager.list_directory("")
            await websocket.send_json({
                "type": "tree",
                "data": tree.to_dict()
            })
        except Exception as e:
            await websocket.send_json({
                "type": "error",
                "error": f"Failed to load directory tree: {str(e)}"
            })

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "error": "Invalid JSON"
                })
                continue

            msg_type = msg.get("type")

            if msg_type == "get_tree":
                path = msg.get("path", "")
                try:
                    tree = file_manager.list_directory(path)
                    await websocket.send_json({
                        "type": "tree",
                        "data": tree.to_dict()
                    })
                except FileNotFoundError as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": str(e)
                    })
                except NotADirectoryError as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": str(e)
                    })
                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": f"Failed to get tree: {str(e)}"
                    })

            elif msg_type == "refresh":
                # Refresh the full tree
                try:
                    tree = file_manager.list_directory("")
                    await websocket.send_json({
                        "type": "tree",
                        "data": tree.to_dict()
                    })
                except Exception as e:
                    await websocket.send_json({
                        "type": "error",
                        "error": f"Failed to refresh: {str(e)}"
                    })

            elif msg_type == "subscribe":
                # For compatibility - just acknowledge
                await websocket.send_json({"type": "subscribed"})

            else:
                await websocket.send_json({
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}"
                })

    except WebSocketDisconnect:
        print("File watcher WebSocket disconnected")
    except Exception as e:
        print(f"File watcher WebSocket error: {e}")


# WebSocket endpoint for remote terminal via sprite
@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    WebSocket endpoint for remote terminal access via sprite.

    Protocol:
    - First message must be JSON: {"type": "connect", "user_id": "..."}
    - Client sends raw text input (keystrokes)
    - Client sends JSON for control: {"type": "resize", "cols": N, "rows": N}
    - Server sends raw text output (terminal output)
    """
    await websocket.accept()

    async def send_json(data: dict):
        await websocket.send_json(data)

    async def receive_text() -> str:
        return await websocket.receive_text()

    try:
        # First message should be connect with user_id
        first_msg = await websocket.receive_text()
        print(f"[terminal] First message received: {first_msg[:200]}")
        try:
            msg = json.loads(first_msg)
            if msg.get("type") != "connect" or not msg.get("user_id"):
                print(f"[terminal] Invalid connect: type={msg.get('type')}, user_id={msg.get('user_id')}")
                await send_json({"type": "error", "message": "First message must be connect with user_id"})
                return

            user_id = msg["user_id"]
        except json.JSONDecodeError:
            await send_json({"type": "error", "message": "Invalid JSON"})
            return

        # Get user's sprite name
        manager = await get_session_manager()
        session = await manager.get_or_create_session(user_id)
        sprite_name = session.sprite_name

        await send_json({"type": "connected", "sprite_name": sprite_name})

        # Start remote terminal session
        await terminal_session(websocket, send_json, receive_text, sprite_name)

    except WebSocketDisconnect:
        print("Terminal WebSocket disconnected")
    except Exception as e:
        print(f"Terminal WebSocket error: {e}")


# Serve static frontend files
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend", "dist")

if os.path.exists(FRONTEND_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")

    @app.get("/")
    async def serve_frontend():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
else:
    @app.get("/")
    async def root():
        return {
            "name": "Monios API",
            "version": "1.0.0",
            "status": "running",
            "note": "Frontend not built. Run 'cd frontend && bun install && bun run build'"
        }


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
