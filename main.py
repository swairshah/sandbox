from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
import asyncio
import os
import json
import uuid
from config import get_settings
from routes import auth_router, chat_router
from routes.files import router as files_router
from file_manager import get_file_watcher, list_directory, FileEvent
from terminal import terminal_session

# Use sandbox_manager on Modal, sessions locally
IS_MODAL = os.environ.get("MODAL_ENVIRONMENT") is not None

if IS_MODAL:
    import sandbox_manager
    async def get_response(message: str, user_id: str):
        return await sandbox_manager.send_message(user_id, message)
    async def clear_session(user_id: str):
        return await sandbox_manager.clear_session(user_id)
    # Queue functions not available in Modal mode
    enqueue_message = None
    set_response_callback = None
    start_queue_processor = None
    get_queue_status = None
else:
    from sessions import (
        get_response,
        clear_session,
        enqueue_message,
        set_response_callback,
        start_queue_processor,
        get_queue_status,
    )


# Store active file watcher WebSocket connections
_file_ws_connections: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - start/stop file watcher."""
    # Startup
    loop = asyncio.get_event_loop()
    file_watcher = get_file_watcher()
    file_watcher.start(loop)

    # Subscribe to file events and broadcast to all connected WebSockets
    def broadcast_file_event(event: FileEvent):
        """Broadcast file event to all connected WebSocket clients."""
        if _file_ws_connections:
            event_data = {
                "type": "file_event",
                **event.to_dict()
            }
            # Schedule broadcast for each connection
            for ws in list(_file_ws_connections):
                try:
                    asyncio.create_task(ws.send_json(event_data))
                except Exception:
                    pass

    file_watcher.subscribe(broadcast_file_event)

    yield

    # Shutdown
    file_watcher.stop()


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


# Public chat endpoint for web UI (no auth required)
class WebChatRequest(BaseModel):
    message: str
    user_id: str = "guest"


@app.post("/chat")
async def web_chat(request: WebChatRequest):
    """Public chat endpoint for web UI."""
    try:
        response_text, session_id, tool_events = await get_response(
            request.message, request.user_id
        )

        if not response_text:
            return {"content": "No response generated (empty result)", "user_id": request.user_id}

        return {
            "content": response_text,
            "user_id": request.user_id,
            "tool_events": tool_events,
            "session_id": session_id,
        }

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Chat error: {error_details}")
        await clear_session(request.user_id)
        return {"content": f"Error: {type(e).__name__}: {str(e)}", "user_id": request.user_id}


@app.post("/chat/clear")
async def clear_chat(request: WebChatRequest):
    """Clear chat history for a user."""
    await clear_session(request.user_id)
    return {"status": "cleared", "user_id": request.user_id}


@app.get("/health")
async def health():
    return {"status": "healthy"}


# WebSocket endpoint for queued message processing
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for real-time chat with message queue.

    Client sends JSON messages:
    - {"type": "connect", "user_id": "...", "session_id": "..."}
    - {"type": "message", "content": "...", "message_id": "..."}
    - {"type": "status"} - get queue status

    Server sends JSON responses:
    - {"type": "connected", "user_id": "..."}
    - {"type": "queued", "message_id": "...", "queue_position": N}
    - {"type": "processing_started", "message_id": "..."}
    - {"type": "response", "message_id": "...", "content": "...", ...}
    - {"type": "error", "message_id": "...", "error": "..."}
    - {"type": "cancelled", "message_id": "...", "reason": "..."}
    """
    if IS_MODAL:
        await websocket.close(code=4000, reason="WebSocket not supported in Modal mode")
        return

    await websocket.accept()
    user_id: str | None = None
    session_id: str | None = None

    async def send_response(data: dict):
        """Callback to send responses back to the WebSocket client."""
        try:
            await websocket.send_json(data)
        except Exception as e:
            print(f"Error sending WebSocket message: {e}")

    try:
        while True:
            # Receive message from client
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

            if msg_type == "connect":
                # Initialize connection with user_id
                user_id = msg.get("user_id", f"guest_{uuid.uuid4().hex[:8]}")
                session_id = msg.get("session_id")

                # Set up the response callback for this user
                set_response_callback(user_id, send_response)

                # Start the queue processor if not running
                start_queue_processor(user_id)

                await websocket.send_json({
                    "type": "connected",
                    "user_id": user_id,
                    "session_id": session_id
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
                    await websocket.send_json({
                        "type": "error",
                        "error": "Empty message"
                    })
                    continue

                # Generate message_id if not provided
                message_id = msg.get("message_id", f"msg_{uuid.uuid4().hex[:8]}")

                # Enqueue the message
                result = await enqueue_message(
                    message_id=message_id,
                    content=content,
                    user_id=user_id,
                    session_id=session_id
                )

                # Send queue status back to client
                await websocket.send_json({
                    "type": "queued",
                    **result
                })

            elif msg_type == "status":
                if not user_id:
                    await websocket.send_json({
                        "type": "error",
                        "error": "Not connected"
                    })
                    continue

                status = get_queue_status(user_id)
                await websocket.send_json({
                    "type": "status",
                    **status
                })

            else:
                await websocket.send_json({
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}"
                })

    except WebSocketDisconnect:
        print(f"WebSocket disconnected for user: {user_id}")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Clean up callback when disconnected
        if user_id:
            set_response_callback(user_id, None)


# WebSocket endpoint for real-time file system updates
@app.websocket("/ws/files")
async def websocket_files(websocket: WebSocket):
    """
    WebSocket endpoint for real-time file system updates.

    Client sends JSON messages:
    - {"type": "subscribe"} - Start receiving file events
    - {"type": "get_tree", "path": "..."} - Get directory tree

    Server sends JSON responses:
    - {"type": "subscribed"}
    - {"type": "tree", "data": {...}}
    - {"type": "file_event", "event_type": "created|deleted|modified|moved", ...}
    """
    await websocket.accept()
    _file_ws_connections.add(websocket)

    try:
        # Send initial directory tree
        try:
            tree = list_directory("")
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
                    tree = list_directory(path)
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

            elif msg_type == "subscribe":
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
    finally:
        _file_ws_connections.discard(websocket)


# WebSocket endpoint for PTY terminal
@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    WebSocket endpoint for PTY terminal access.

    Protocol:
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
        await terminal_session(websocket, send_json, receive_text)
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
