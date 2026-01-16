"""
Remote terminal module - bridges WebSocket to sprite's exec endpoint.
"""

import os
import json
import asyncio
import websockets
from urllib.parse import urlencode
from typing import Optional

SPRITE_TOKEN = os.environ.get("SPRITE_TOKEN", "")
SPRITES_API_BASE = "wss://api.sprites.dev/v1"


class RemoteTerminal:
    """Manages a remote terminal session via sprite's exec WebSocket."""

    def __init__(self, sprite_name: str):
        self.sprite_name = sprite_name
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._closed = False
        self._cols = 80
        self._rows = 24

    async def connect(self, cols: int = 80, rows: int = 24) -> bool:
        """Connect to sprite's exec WebSocket."""
        if not SPRITE_TOKEN:
            print("SPRITE_TOKEN not set")
            return False

        self._cols = cols
        self._rows = rows

        # Build URL with query params (command passed as query params)
        params = {
            "cmd": "bash",
            "tty": "true",
            "stdin": "true",
            "cols": str(cols),
            "rows": str(rows),
        }
        # Add env vars as separate params
        env_vars = ["TERM=xterm-256color", "COLORTERM=truecolor"]

        url = f"{SPRITES_API_BASE}/sprites/{self.sprite_name}/exec?{urlencode(params)}"
        for env in env_vars:
            url += f"&env={env}"

        headers = {"Authorization": f"Bearer {SPRITE_TOKEN}"}

        try:
            self.ws = await websockets.connect(url, additional_headers=headers)

            # Wait for session_info message
            try:
                first_msg = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                if isinstance(first_msg, str):
                    data = json.loads(first_msg)
                    if data.get("type") == "session_info":
                        print(f"Connected to sprite exec, session_id: {data.get('session_id')}")
                    elif data.get("type") == "error":
                        print(f"Sprite exec error: {data.get('message')}")
                        return False
            except asyncio.TimeoutError:
                print("Timeout waiting for session_info")
            except json.JSONDecodeError:
                pass  # Might be raw output already

            # send clear to hide shell init noise
            await self.ws.send(b"clear\n")
            return True

        except Exception as e:
            print(f"Failed to connect to sprite exec: {e}")
            return False

    async def resize(self, cols: int, rows: int):
        """Resize the remote terminal."""
        if self.ws and not self._closed:
            try:
                self._cols = cols
                self._rows = rows
                resize_msg = json.dumps({
                    "type": "resize",
                    "cols": cols,
                    "rows": rows
                })
                await self.ws.send(resize_msg)
            except Exception as e:
                print(f"Resize error: {e}")

    async def write(self, data: str):
        """Write input to the remote terminal (raw bytes in TTY mode)."""
        if self.ws and not self._closed:
            try:
                # In TTY mode, send raw bytes
                await self.ws.send(data.encode("utf-8"))
            except Exception as e:
                print(f"Write error: {e}")

    async def read(self) -> Optional[str]:
        """Read output from the remote terminal."""
        if self.ws is None or self._closed:
            return None

        try:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=0.1)

            # In TTY mode, output is raw bytes
            if isinstance(msg, bytes):
                return msg.decode("utf-8", errors="replace")

            # Could be JSON control message
            if isinstance(msg, str):
                try:
                    data = json.loads(msg)
                    msg_type = data.get("type")

                    if msg_type == "exit":
                        print(f"Process exited with code: {data.get('exit_code')}")
                        self._closed = True
                        return None
                    elif msg_type == "port_opened":
                        print(f"Port opened: {data.get('port')}")
                        return None
                    elif msg_type == "error":
                        print(f"Sprite exec error: {data.get('message')}")
                        return None
                    elif msg_type == "session_info":
                        # Already handled during connect
                        return None
                except json.JSONDecodeError:
                    # Raw text output
                    return msg

            return None

        except asyncio.TimeoutError:
            return None
        except websockets.exceptions.ConnectionClosed:
            self._closed = True
            return None
        except Exception as e:
            print(f"Read error: {e}")
            return None

    def is_alive(self) -> bool:
        """Check if the connection is still alive."""
        return self.ws is not None and not self._closed

    async def close(self):
        """Close the remote terminal connection."""
        if self._closed:
            return

        self._closed = True

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


async def terminal_session(websocket, send_json, receive_text, sprite_name: str):
    """
    Run a remote terminal session over WebSocket.

    Protocol:
    - Client sends raw input as text
    - Client sends JSON for control: {"type": "resize", "cols": N, "rows": N}
    - Server sends raw output as text
    """
    terminal = RemoteTerminal(sprite_name)

    # Get initial size from client or use defaults
    cols, rows = 80, 24

    if not await terminal.connect(cols, rows):
        await send_json({"type": "error", "message": "Failed to connect to remote terminal"})
        return

    async def read_output():
        """Read from remote terminal and send to WebSocket."""
        while terminal.is_alive():
            data = await terminal.read()
            if data:
                try:
                    await websocket.send_text(data)
                except Exception:
                    break
            else:
                await asyncio.sleep(0.01)

    # Start the read task
    read_task = asyncio.create_task(read_output())

    try:
        while True:
            data = await receive_text()

            # Check if it's a control message (JSON)
            if data.startswith("{"):
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "resize":
                        await terminal.resize(msg.get("cols", 80), msg.get("rows", 24))
                    continue
                except json.JSONDecodeError:
                    pass  # Not JSON, treat as input

            # Regular input - write to remote terminal
            await terminal.write(data)

    except Exception as e:
        print(f"Terminal session error: {e}")
    finally:
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass
        await terminal.close()


# ============== Legacy local terminal (for fallback) ==============

import pty
import select
import struct
import fcntl
import termios
import signal
from pathlib import Path

WORKSPACE_DIR = Path(__file__).parent / "workspace"


class LocalPtyProcess:
    """Manages a local PTY subprocess (fallback)."""

    def __init__(self, shell: str = "/bin/bash"):
        self.shell = shell
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self._closed = False

    def spawn(self, cols: int = 80, rows: int = 24) -> bool:
        """Spawn the PTY process."""
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        pid, fd = pty.fork()

        if pid == 0:
            os.chdir(str(WORKSPACE_DIR))
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.execlp(self.shell, self.shell)
        else:
            self.pid = pid
            self.fd = fd
            self.resize(cols, rows)

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

    def read(self, size: int = 4096) -> Optional[bytes]:
        """Read data from the PTY."""
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
        """Close the PTY."""
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


async def local_terminal_session(websocket, send_json, receive_text):
    """Run a local terminal session (fallback when sprite not available)."""
    pty_process = LocalPtyProcess()

    cols, rows = 80, 24

    if not pty_process.spawn(cols, rows):
        await send_json({"type": "error", "message": "Failed to spawn terminal"})
        return

    async def read_pty():
        while pty_process.is_alive():
            data = pty_process.read()
            if data:
                try:
                    await websocket.send_text(data.decode("utf-8", errors="replace"))
                except Exception:
                    break
            else:
                await asyncio.sleep(0.01)

    read_task = asyncio.create_task(read_pty())

    try:
        while True:
            data = await receive_text()

            if data.startswith("{"):
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "resize":
                        pty_process.resize(msg.get("cols", 80), msg.get("rows", 24))
                    continue
                except json.JSONDecodeError:
                    pass

            pty_process.write(data.encode("utf-8"))

    except Exception as e:
        print(f"Local terminal session error: {e}")
    finally:
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass
        pty_process.close()
