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
import time
import uuid
from config import get_settings
from routes import auth_router, chat_router
from routes.files import router as files_router
from file_manager import get_file_watcher, list_directory, FileEvent
from terminal import terminal_session
import database

# Use modal_sessions on Modal, sessions locally
IS_MODAL = os.environ.get("MODAL_ENVIRONMENT") is not None

if IS_MODAL:
    from modal_sessions import (
        get_response,
        get_response_streaming,
        clear_session,
        get_session_manager,
        cleanup_session_manager,
    )
    import sandbox_manager
    import httpx

    class SandboxNotReadyError(Exception):
        """Raised when sandbox doesn't exist yet (user needs to send a message first)."""
        pass

    async def _get_sandbox_file_tree(user_id: str, path: str = "") -> dict:
        """Fetch file tree from user's sandbox. Uses lookup_sandbox (read-only)."""
        result = await sandbox_manager.lookup_sandbox(user_id)
        if result is None:
            raise SandboxNotReadyError("Sandbox not initialized. Please send a message first to start your session.")
        _, http_url, _, _ = result
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{http_url}/files/list",
                params={"path": path},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise Exception(f"Failed to fetch file tree: {resp.text}")
            data = resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data.get("data", {})

    async def _read_sandbox_file(user_id: str, path: str) -> dict:
        """Read file contents from user's sandbox. Uses lookup_sandbox (read-only)."""
        result = await sandbox_manager.lookup_sandbox(user_id)
        if result is None:
            raise SandboxNotReadyError("Sandbox not initialized. Please send a message first to start your session.")
        _, http_url, _, _ = result
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{http_url}/files/read",
                params={"path": path},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise Exception(f"Failed to read file: {resp.text}")
            data = resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data.get("data", {})

    async def _push_file_tree_for_user(user_id: str, path: str = "") -> None:
        if not user_id:
            return
        connections = _file_ws_connections_by_user.get(user_id)
        if not connections:
            return
        now = time.time()
        last_sent = _file_refresh_last.get(user_id, 0.0)
        if now - last_sent < _FILE_REFRESH_MIN_INTERVAL:
            return
        if user_id in _file_refresh_inflight:
            return
        _file_refresh_inflight.add(user_id)
        try:
            tree = await _get_sandbox_file_tree(user_id, path)
            for ws in list(connections):
                try:
                    await ws.send_json({"type": "tree", "data": tree})
                except Exception:
                    pass
            _file_refresh_last[user_id] = time.time()
        except SandboxNotReadyError:
            for ws in list(connections):
                try:
                    await ws.send_json({"type": "error", "error": "Not initialized"})
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            _file_refresh_inflight.discard(user_id)

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
_file_ws_connections_by_user: dict[str, set[WebSocket]] = {}
_file_refresh_inflight: set[str] = set()
_file_refresh_last: dict[str, float] = {}
_FILE_REFRESH_MIN_INTERVAL = 1.0


def _register_file_ws(user_id: str, websocket: WebSocket) -> None:
    if not user_id:
        return
    connections = _file_ws_connections_by_user.setdefault(user_id, set())
    connections.add(websocket)


def _unregister_file_ws(user_id: str | None, websocket: WebSocket) -> None:
    if not user_id:
        return
    connections = _file_ws_connections_by_user.get(user_id)
    if not connections:
        return
    connections.discard(websocket)
    if not connections:
        del _file_ws_connections_by_user[user_id]


def _is_file_mutation_tool(name: str | None) -> bool:
    if not name:
        return True
    if name in {"Write", "Edit", "Bash"}:
        return True
    return name.endswith("__Write") or name.endswith("__Edit") or name.endswith("__Bash")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - start/stop file watcher."""
    # Startup
    if IS_MODAL:
        await get_session_manager()
        # File watching in Modal mode is triggered by tool results (no local watcher)
        yield
        # Shutdown
        await cleanup_session_manager()
    else:
        # Local mode: use file watcher
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
    database.clear_messages(request.user_id)
    return {"status": "cleared", "user_id": request.user_id}


@app.get("/chat/history")
async def get_chat_history(user_id: str = "guest", limit: int = 50, offset: int = 0):
    """Get chat history for a user."""
    if IS_MODAL:
        try:
            await sandbox_manager.get_or_create_sandbox(user_id)
        except Exception as e:
            print(f"[chat_history] Failed to initialize sandbox for {user_id}: {e}")
    messages = database.get_messages(user_id, limit, offset)
    total = database.get_message_count(user_id)
    return {
        "messages": messages,
        "total": total,
        "limit": limit,
        "offset": offset,
        "user_id": user_id,
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/sandbox/terminate")
async def terminate_sandbox_endpoint(user_id: str = "guest"):
    """Terminate user's sandbox to force recreation with new settings."""
    if not IS_MODAL:
        return {"terminated": False, "error": "Only available in Modal mode"}

    try:
        result = await sandbox_manager.terminate_sandbox(user_id)
        return {"terminated": result, "user_id": user_id, "message": "Send a chat message to create a new sandbox"}
    except Exception as e:
        return {"terminated": False, "error": str(e)}


@app.get("/preview")
async def get_preview(user_id: str = "guest"):
    """Get preview URL for user's sandbox."""
    if not IS_MODAL:
        return {"preview_url": None, "error": "Preview only available in Modal mode"}

    try:
        # Get sandbox reference (may be cached)
        result = await sandbox_manager.lookup_sandbox(user_id)
        if result is None:
            return {"preview_url": None, "error": "No sandbox found", "user_id": user_id}

        sb, _, _, _ = result

        # Always fetch FRESH tunnel info - don't use cached preview_url
        tunnels = sb.tunnels()
        preview_tunnel = tunnels.get(3000)
        fresh_preview_url = preview_tunnel.url if preview_tunnel else None

        return {
            "preview_url": fresh_preview_url,
            "user_id": user_id,
            "available_ports": list(tunnels.keys()),
        }
    except Exception as e:
        import traceback
        return {"preview_url": None, "error": str(e), "traceback": traceback.format_exc(), "user_id": user_id}


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
        await websocket.accept()
        user_id: str | None = None

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
                    await websocket.send_json({"type": "connected", "user_id": user_id})

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
                    await websocket.send_json({"type": "processing_started", "message_id": message_id})

                    # Save user message to database
                    database.save_message(user_id, "user", content)

                    # Streaming callbacks to send tool events as they happen
                    tool_use_names: dict[str, str] = {}

                    async def on_tool_use(event):
                        tool_use_id = event.get("tool_use_id")
                        name = event.get("name")
                        if tool_use_id and name:
                            tool_use_names[tool_use_id] = name
                        await websocket.send_json({
                            "type": "tool_use",
                            "message_id": message_id,
                            **event,
                        })

                    async def on_tool_result(event):
                        await websocket.send_json({
                            "type": "tool_result", 
                            "message_id": message_id,
                            **event,
                        })
                        tool_name = tool_use_names.get(event.get("tool_use_id"))
                        if user_id and _is_file_mutation_tool(tool_name):
                            await _push_file_tree_for_user(user_id)

                    try:
                        response_text, session_id, tool_events = await get_response_streaming(
                            content, user_id,
                            on_tool_use=on_tool_use,
                            on_tool_result=on_tool_result,
                        )
                        
                        # Save assistant response to database
                        database.save_message(user_id, "assistant", response_text, tool_events, session_id)
                        
                        await websocket.send_json({
                            "type": "response",
                            "message_id": message_id,
                            "content": response_text,
                            "tool_events": tool_events,
                            "session_id": session_id,
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message_id": message_id,
                            "error": str(e),
                        })

                elif msg_type == "status":
                    await websocket.send_json({
                        "type": "status",
                        "queue_size": 0,
                        "max_queue_size": 0,
                        "is_processing": False,
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
    - {"type": "connect", "user_id": "..."} - Connect with user ID (Modal mode)
    - {"type": "subscribe"} - Start receiving file events
    - {"type": "get_tree", "path": "..."} - Get directory tree

    Server sends JSON responses:
    - {"type": "subscribed"}
    - {"type": "tree", "data": {...}}
    - {"type": "file_event", "event_type": "created|deleted|modified|moved", ...}
    """
    await websocket.accept()
    _file_ws_connections.add(websocket)
    user_id: str | None = None

    try:
        if IS_MODAL:
            # Modal mode: wait for connect message with user_id first
            # Then fetch tree from sandbox
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
                    _register_file_ws(user_id, websocket)
                    # Send initial tree from sandbox
                    try:
                        tree = await _get_sandbox_file_tree(user_id, "")
                        await websocket.send_json({"type": "connected", "user_id": user_id})
                        await websocket.send_json({"type": "tree", "data": tree})
                    except Exception as e:
                        if isinstance(e, SandboxNotReadyError):
                            await websocket.send_json({"type": "error", "error": "Not initialized"})
                        else:
                            await websocket.send_json({"type": "error", "error": f"Failed to load directory tree: {str(e)}"})

                elif msg_type == "get_tree":
                    if not user_id:
                        await websocket.send_json({"type": "error", "error": "Not connected"})
                        continue
                    path = msg.get("path", "")
                    try:
                        tree = await _get_sandbox_file_tree(user_id, path)
                        await websocket.send_json({"type": "tree", "data": tree})
                    except Exception as e:
                        if isinstance(e, SandboxNotReadyError):
                            await websocket.send_json({"type": "error", "error": "Not initialized"})
                        else:
                            await websocket.send_json({"type": "error", "error": str(e)})

                elif msg_type == "subscribe":
                    await websocket.send_json({"type": "subscribed"})

                elif msg_type == "refresh":
                    # Manual refresh request
                    if user_id:
                        try:
                            tree = await _get_sandbox_file_tree(user_id, "")
                            await websocket.send_json({"type": "tree", "data": tree})
                        except Exception as e:
                            if isinstance(e, SandboxNotReadyError):
                                await websocket.send_json({"type": "error", "error": "Not initialized"})
                            else:
                                await websocket.send_json({"type": "error", "error": str(e)})

                else:
                    await websocket.send_json({"type": "error", "error": f"Unknown message type: {msg_type}"})
        else:
            # Local mode: use local file_manager
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
        if IS_MODAL:
            _unregister_file_ws(user_id, websocket)
        _file_ws_connections.discard(websocket)


# WebSocket endpoint for PTY terminal
@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    WebSocket endpoint for PTY terminal access.

    Protocol:
    - Client sends raw text input (keystrokes)
    - Client sends JSON for control: {"type": "resize", "cols": N, "rows": N}
    - Client sends JSON for connect (Modal mode): {"type": "connect", "user_id": "..."}
    - Server sends raw text output (terminal output)
    """
    await websocket.accept()

    if IS_MODAL:
        # Modal mode: proxy to sandbox's terminal WebSocket
        import websockets
        
        user_id: str | None = None
        sandbox_ws = None
        relay_task = None
        
        try:
            while True:
                data = await websocket.receive_text()
                
                # Check for connect message first
                if data.startswith("{"):
                    try:
                        msg = json.loads(data)
                        if msg.get("type") == "connect":
                            user_id = msg.get("user_id", f"guest_{uuid.uuid4().hex[:8]}")
                            print(f"[terminal] Connecting user {user_id} to sandbox terminal...")
                            
                            try:
                                # Get sandbox terminal URL (lookup only, don't create)
                                result = await sandbox_manager.lookup_sandbox(user_id)
                                if result is None:
                                    await websocket.send_json({
                                        "type": "error",
                                        "error": "Sandbox not initialized. Please send a message first to start your session."
                                    })
                                    continue
                                _, _, terminal_url, _ = result
                                if not terminal_url:
                                    await websocket.send_json({"type": "error", "error": "Terminal not available"})
                                    continue
                                
                                # Convert HTTPS URL to WSS
                                ws_url = terminal_url.replace("https://", "wss://").replace("http://", "ws://")
                                print(f"[terminal] Connecting to sandbox WebSocket: {ws_url}")
                                
                                # Connect to sandbox terminal
                                sandbox_ws = await websockets.connect(ws_url)
                                await websocket.send_json({"type": "connected", "user_id": user_id})
                                print(f"[terminal] Connected to sandbox for user {user_id}")
                                
                                # Start bidirectional relay from sandbox to client
                                async def relay_from_sandbox():
                                    try:
                                        async for message in sandbox_ws:
                                            await websocket.send_text(message)
                                    except websockets.exceptions.ConnectionClosed:
                                        print("[terminal] Sandbox WebSocket closed")
                                    except Exception as e:
                                        print(f"[terminal] Relay error: {e}")
                                
                                relay_task = asyncio.create_task(relay_from_sandbox())
                            except Exception as e:
                                print(f"[terminal] Failed to connect to sandbox: {e}")
                                await websocket.send_json({"type": "error", "error": f"Failed to connect: {str(e)}"})
                            continue
                            
                        elif msg.get("type") == "resize" and sandbox_ws:
                            await sandbox_ws.send(data)
                            continue
                    except json.JSONDecodeError:
                        pass
                
                # Forward to sandbox
                if sandbox_ws:
                    try:
                        await sandbox_ws.send(data)
                    except Exception as e:
                        print(f"[terminal] Failed to send to sandbox: {e}")
                        await websocket.send_json({"type": "error", "error": f"Send failed: {str(e)}"})
                else:
                    await websocket.send_json({"type": "error", "error": "Not connected. Send connect message first."})
                    
        except WebSocketDisconnect:
            print(f"[terminal] WebSocket disconnected for user: {user_id}")
        except Exception as e:
            print(f"[terminal] WebSocket error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if relay_task:
                relay_task.cancel()
                try:
                    await relay_task
                except asyncio.CancelledError:
                    pass
            if sandbox_ws:
                await sandbox_ws.close()
    else:
        # Local mode: use local PTY
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
