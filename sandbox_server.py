"""Small HTTP server that runs inside each user's sandbox.

This handles Claude SDK interactions within the isolated sandbox environment.
"""

import json
import asyncio
import traceback
import os
from collections import deque
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
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
    """HTTP handler for chat requests."""

    def do_POST(self):
        if self.path == "/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            message = data.get("message", "")

            try:
                response_text, session_id, tool_events = _loop.run_until_complete(chat(message))
                result = {
                    "content": response_text,
                    "session_id": session_id,
                    "tool_events": tool_events,
                }
                self.send_response(200)
            except Exception as e:
                result = {
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "stderr_tail": list(_stderr_lines),
                }
                self.send_response(500)

            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        elif self.path == "/clear":
            try:
                _loop.run_until_complete(clear())
                result = {"status": "cleared"}
                self.send_response(200)
            except Exception as e:
                result = {"error": str(e), "traceback": traceback.format_exc()}
                self.send_response(500)

            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress default logging
        pass


def main():
    """Run the sandbox server."""
    port = 8080
    server = HTTPServer(("0.0.0.0", port), ChatHandler)
    print(f"Sandbox server running on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
