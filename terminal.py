"""
PTY terminal module for WebSocket-based terminal access.
Spawns a shell and streams I/O over WebSocket.
"""

import os
import pty
import select
import struct
import fcntl
import termios
import asyncio
import time
import signal
from typing import Optional
from pathlib import Path

# Working directory for terminal sessions
WORKSPACE_DIR = Path(__file__).parent / "workspace"


class PtyProcess:
    """Manages a PTY subprocess."""

    def __init__(self, shell: str = "/bin/bash"):
        self.shell = shell
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self._closed = False

    def spawn(self, cols: int = 80, rows: int = 24) -> bool:
        """Spawn the PTY process."""
        # Ensure workspace exists
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        pid, fd = pty.fork()

        if pid == 0:
            # Child process
            os.chdir(str(WORKSPACE_DIR))
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            os.execlp(self.shell, self.shell)
        else:
            # Parent process
            self.pid = pid
            self.fd = fd

            # Set initial size
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

    def read(self, size: int = 4096) -> Optional[bytes]:
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
                # Give it a moment to terminate gracefully
                for _ in range(10):
                    pid, _ = os.waitpid(self.pid, os.WNOHANG)
                    if pid != 0:
                        break
                    time.sleep(0.1)
                else:
                    # Force kill if still running
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
            self.pid = None


async def terminal_session(websocket, send_json, receive_text):
    """
    Run a terminal session over WebSocket.

    Protocol:
    - Client sends raw input as text
    - Client sends JSON for control: {"type": "resize", "cols": N, "rows": N}
    - Server sends raw output as text
    """
    import json

    pty_process = PtyProcess()

    # Get initial size from client or use defaults
    cols, rows = 80, 24

    if not pty_process.spawn(cols, rows):
        await send_json({"type": "error", "message": "Failed to spawn terminal"})
        return

    async def read_pty():
        """Read from PTY and send to WebSocket."""
        while pty_process.is_alive():
            data = pty_process.read()
            if data:
                try:
                    # Send as text (decode with replacement for invalid chars)
                    await websocket.send_text(data.decode("utf-8", errors="replace"))
                except Exception:
                    break
            else:
                await asyncio.sleep(0.01)  # Small sleep to prevent busy loop

    # Start the read task
    read_task = asyncio.create_task(read_pty())

    try:
        while True:
            data = await receive_text()

            # Check if it's a control message (JSON)
            if data.startswith("{"):
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "resize":
                        pty_process.resize(msg.get("cols", 80), msg.get("rows", 24))
                    continue
                except json.JSONDecodeError:
                    pass  # Not JSON, treat as input

            # Regular input - write to PTY
            pty_process.write(data.encode("utf-8"))

    except Exception as e:
        print(f"Terminal session error: {e}")
    finally:
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass
        pty_process.close()
