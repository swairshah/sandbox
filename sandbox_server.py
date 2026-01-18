"""Small HTTP server that runs inside each user's sandbox.

This handles Claude SDK interactions within the isolated sandbox environment.
Also provides file browsing and terminal access.
"""

import json
import asyncio
import traceback
import os
import pty
import select
import struct
import fcntl
import termios
import signal
import threading
from collections import deque
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    SystemMessage,
    ToolUseBlock,
    ToolResultBlock,
)

SYSTEM_PROMPT = "You are a helpful assistant in a terminal-aesthetic chat app called Monios. Keep responses concise and friendly."

# Workspace directory for file operations
WORKSPACE_DIR = Path("/workspace")

# Patterns to ignore when listing files
IGNORE_PATTERNS = {
    "__pycache__", ".git", ".DS_Store", "node_modules", ".venv", "venv",
    ".env", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache", ".session_id",
}


def _should_ignore(name: str) -> bool:
    """Check if a file/directory should be ignored."""
    if name in IGNORE_PATTERNS:
        return True
    for pattern in IGNORE_PATTERNS:
        if pattern.startswith("*") and name.endswith(pattern[1:]):
            return True
    return False


def list_directory(relative_path: str = "") -> dict:
    """List contents of a directory within workspace."""
    target_path = WORKSPACE_DIR / relative_path if relative_path else WORKSPACE_DIR
    
    if not target_path.exists():
        raise FileNotFoundError(f"Directory not found: {relative_path}")
    
    if not target_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")
    
    return _build_tree(target_path, relative_path or "")


def _build_tree(path: Path, relative_base: str) -> dict:
    """Recursively build the file tree."""
    name = path.name or "workspace"
    rel_path = relative_base or "."
    
    if path.is_file():
        return {"name": name, "path": rel_path, "type": "file"}
    
    children = []
    try:
        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for entry in entries:
            if _should_ignore(entry.name):
                continue
            child_rel_path = str(Path(relative_base) / entry.name) if relative_base else entry.name
            children.append(_build_tree(entry, child_rel_path))
    except PermissionError:
        pass
    
    return {"name": name, "path": rel_path, "type": "directory", "children": children}


def read_file_contents(relative_path: str, max_size: int = 1024 * 1024) -> dict:
    """Read the contents of a file within workspace."""
    if not relative_path:
        raise ValueError("File path is required")
    
    target_path = WORKSPACE_DIR / relative_path
    
    if not target_path.exists():
        raise FileNotFoundError(f"File not found: {relative_path}")
    
    if target_path.is_dir():
        raise IsADirectoryError(f"Cannot read directory: {relative_path}")
    
    # Security check
    try:
        target_path.resolve().relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        raise PermissionError(f"Access denied: {relative_path}")
    
    file_size = target_path.stat().st_size
    truncated = file_size > max_size
    
    binary_extensions = {
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.tar', '.gz', '.rar', '.7z',
        '.exe', '.dll', '.so', '.dylib',
        '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv',
        '.ttf', '.woff', '.woff2', '.eot',
        '.pyc', '.pyo', '.class',
    }
    
    ext = target_path.suffix.lower()
    is_binary = ext in binary_extensions
    
    if is_binary:
        return {
            "path": relative_path,
            "name": target_path.name,
            "content": None,
            "size": file_size,
            "truncated": False,
            "is_binary": True,
            "extension": ext,
        }
    
    try:
        with open(target_path, 'r', encoding='utf-8') as f:
            content = f.read(max_size)
    except UnicodeDecodeError:
        return {
            "path": relative_path,
            "name": target_path.name,
            "content": None,
            "size": file_size,
            "truncated": False,
            "is_binary": True,
            "extension": ext,
        }
    
    return {
        "path": relative_path,
        "name": target_path.name,
        "content": content,
        "size": file_size,
        "truncated": truncated,
        "is_binary": False,
        "extension": ext,
    }


# Single client per sandbox (one user per sandbox)
_client: ClaudeSDKClient | None = None
_session_id: str | None = None
_stderr_lines: deque[str] = deque(maxlen=200)
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
_SESSION_FILE = Path("/workspace/.session_id")


def _on_stderr(line: str) -> None:
    _stderr_lines.append(line)


def _missing_api_key() -> bool:
    return not (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def _load_session_id() -> str | None:
    try:
        return _SESSION_FILE.read_text().strip() or None
    except OSError:
        return None


def _save_session_id(session_id: str) -> None:
    try:
        _SESSION_FILE.write_text(session_id)
    except OSError:
        pass


def _clear_session_id() -> None:
    try:
        _SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


async def get_client() -> ClaudeSDKClient:
    """Get or create the Claude SDK client."""
    global _client, _session_id
    if _client is None:
        if _missing_api_key():
            raise RuntimeError(
                "Missing API key. Set ANTHROPIC_API_KEY (or CLAUDE_API_KEY) in monios-secrets."
            )
        if _session_id is None:
            _session_id = _load_session_id()
        options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=10,
            cwd="/workspace",  # User's isolated workspace
            resume=_session_id,
            extra_args={"debug-to-stderr": None},
            stderr=_on_stderr,
        )
        _client = ClaudeSDKClient(options=options)
        await _client.connect()
    return _client


async def chat(message: str) -> tuple[str, str, list[dict[str, object]]]:
    """Send message and get response."""
    global _session_id
    client = await get_client()

    if _session_id:
        await client.query(prompt=message, session_id=_session_id)
    else:
        await client.query(prompt=message)

    response_text = ""
    tool_events: list[dict[str, object]] = []
    new_session_id = None

    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage):
            data = msg.data
            new_session_id = data.get("session_id", None)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
                elif isinstance(block, ToolUseBlock):
                    tool_events.append(
                        {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input,
                            "tool_use_id": block.id,
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )

    if new_session_id:
        _session_id = new_session_id
        _save_session_id(new_session_id)

    return response_text, _session_id, tool_events


async def clear():
    """Clear the session."""
    global _client, _session_id
    if _client:
        try:
            await _client.disconnect()
        except:
            pass
        _client = None
    _session_id = None
    _clear_session_id()


class ChatHandler(BaseHTTPRequestHandler):
    """HTTP handler for chat, file, and terminal requests."""

    def _send_json(self, data: dict, status: int = 200):
        """Helper to send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        if self.path == "/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            message = data.get("message", "")

            try:
                response_text, session_id, tool_events = _loop.run_until_complete(chat(message))
                self._send_json({
                    "content": response_text,
                    "session_id": session_id,
                    "tool_events": tool_events,
                })
            except Exception as e:
                self._send_json({
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "stderr_tail": list(_stderr_lines),
                }, 500)

        elif self.path == "/clear":
            try:
                _loop.run_until_complete(clear())
                self._send_json({"status": "cleared"})
            except Exception as e:
                self._send_json({"error": str(e), "traceback": traceback.format_exc()}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok"})

        elif path == "/files/list":
            rel_path = query.get("path", [""])[0]
            try:
                tree = list_directory(rel_path)
                self._send_json({"type": "tree", "data": tree})
            except FileNotFoundError as e:
                self._send_json({"error": str(e)}, 404)
            except NotADirectoryError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/files/read":
            rel_path = query.get("path", [""])[0]
            try:
                content = read_file_contents(rel_path)
                self._send_json({"type": "file", "data": content})
            except FileNotFoundError as e:
                self._send_json({"error": str(e)}, 404)
            except IsADirectoryError as e:
                self._send_json({"error": str(e)}, 400)
            except PermissionError as e:
                self._send_json({"error": str(e)}, 403)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default logging
        pass


# ============== PTY Terminal ==============

class PtyTerminal:
    """Manages a PTY subprocess for terminal access."""

    def __init__(self, shell: str = "/bin/bash"):
        self.shell = shell
        self.pid = None
        self.fd = None
        self._closed = False

    def spawn(self, cols: int = 80, rows: int = 24) -> bool:
        """Spawn the PTY process."""
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        pid, fd = pty.fork()

        if pid == 0:
            # Child process
            os.chdir(str(WORKSPACE_DIR))
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.environ["HOME"] = str(WORKSPACE_DIR)
            os.execlp(self.shell, self.shell)
        else:
            # Parent process
            self.pid = pid
            self.fd = fd
            self.resize(cols, rows)
            # Make fd non-blocking
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return True

    def resize(self, cols: int, rows: int):
        """Resize the PTY."""
        if self.fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def write(self, data: bytes):
        """Write data to the PTY."""
        if self.fd is not None and not self._closed:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def read(self, size: int = 4096) -> bytes | None:
        """Read data from the PTY (non-blocking)."""
        if self.fd is None or self._closed:
            return None
        try:
            r, _, _ = select.select([self.fd], [], [], 0)
            if r:
                return os.read(self.fd, size)
        except (OSError, ValueError):
            pass
        return None

    def is_alive(self) -> bool:
        """Check if the PTY process is still running."""
        if self.pid is None:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def close(self):
        """Close the PTY and terminate the process."""
        if self._closed:
            return
        self._closed = True

        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
                for _ in range(10):
                    pid, _ = os.waitpid(self.pid, os.WNOHANG)
                    if pid != 0:
                        break
                    import time
                    time.sleep(0.1)
                else:
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
            self.pid = None


# Global terminal instance (one per sandbox)
_terminal: PtyTerminal | None = None


async def handle_terminal_websocket(websocket):
    """Handle a WebSocket connection for terminal access."""
    global _terminal

    # Create terminal if needed
    if _terminal is None or not _terminal.is_alive():
        _terminal = PtyTerminal()
        _terminal.spawn()

    async def read_pty():
        """Read from PTY and send to WebSocket."""
        while _terminal and _terminal.is_alive():
            data = _terminal.read()
            if data:
                try:
                    await websocket.send(data.decode("utf-8", errors="replace"))
                except Exception:
                    break
            else:
                await asyncio.sleep(0.01)

    read_task = asyncio.create_task(read_pty())

    try:
        async for message in websocket:
            # Check if it's a control message (JSON)
            if message.startswith("{"):
                try:
                    msg = json.loads(message)
                    if msg.get("type") == "resize":
                        _terminal.resize(msg.get("cols", 80), msg.get("rows", 24))
                    continue
                except json.JSONDecodeError:
                    pass
            # Regular input
            _terminal.write(message.encode("utf-8"))
    except Exception as e:
        print(f"Terminal WebSocket error: {e}")
    finally:
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass


async def run_terminal_server(port: int = 8081):
    """Run the WebSocket terminal server."""
    try:
        import websockets
    except ImportError:
        print("websockets not installed, terminal server disabled")
        return

    async def handler(websocket, path=None):
        await handle_terminal_websocket(websocket)

    server = await websockets.serve(handler, "0.0.0.0", port)
    print(f"Terminal WebSocket server running on port {port}")
    await server.wait_closed()


def main():
    """Run the sandbox server."""
    http_port = 8080
    terminal_port = 8081

    # Start terminal WebSocket server in a thread
    def run_terminal():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_terminal_server(terminal_port))
        except Exception as e:
            print(f"Terminal server error: {e}")

    terminal_thread = threading.Thread(target=run_terminal, daemon=True)
    terminal_thread.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", http_port), ChatHandler)
    print(f"Sandbox HTTP server running on port {http_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
