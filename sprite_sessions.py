"""
Sprite session manager - handles per-user sprites and Claude SDK clients.
"""

import os
import asyncio
import httpx
from typing import Any, Optional
from dataclasses import dataclass

from sprites import SpritesClient

SPRITE_TOKEN = os.environ.get("SPRITE_TOKEN", "")
SPRITES_API_BASE = "https://api.sprites.dev/v1"


async def get_sprite_url(sprite_name: str) -> str | None:
    """Fetch the sprite's public URL from the API."""
    if not SPRITE_TOKEN:
        return None

    async with httpx.AsyncClient() as client:
        try:
            # Get current URL settings
            response = await client.get(
                f"{SPRITES_API_BASE}/sprites/{sprite_name}/url",
                headers={"Authorization": f"Bearer {SPRITE_TOKEN}"}
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("url")

            # If no URL configured, configure it with default auth
            response = await client.patch(
                f"{SPRITES_API_BASE}/sprites/{sprite_name}/url",
                headers={"Authorization": f"Bearer {SPRITE_TOKEN}"},
                json={"auth": "default"}
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("url")

        except Exception as e:
            print(f"[sprites] failed to get URL for {sprite_name}: {e}")

    return None

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    tool,
    create_sdk_mcp_server,
)

from database import Database, get_database, User, Conversation, Message


def _quote(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _text(text: str) -> dict[str, Any]:
    """Return a successful text response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    """Return an error response."""
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


class SpriteToolProvider:
    """Provides Claude tools that proxy to a remote Sprite sandbox."""

    def __init__(self, sprite):
        self.sprite = sprite

    def create_mcp_server(self):
        """Create an MCP server with all tools proxied to the sprite."""

        sprite = self.sprite

        def _run_cmd(cmd: str) -> tuple[str, int]:
            """Execute a command via sprite.command and return (output, returncode)."""
            try:
                output = sprite.command("bash", "-c", cmd).combined_output()
                return output.decode(), 0
            except Exception as e:
                return str(e), 1

        def _run_cmd_stdin(cmd: str, stdin_data: bytes) -> tuple[str, int]:
            """Execute a command with stdin via sprite.command."""
            try:
                output = sprite.command("bash", "-c", cmd).stdin(stdin_data).combined_output()
                return output.decode(), 0
            except Exception as e:
                return str(e), 1

        # ============== File Operations ==============

        @tool(
            "Read",
            "Read file contents from the workspace.",
            {"file_path": str, "offset": int, "limit": int}
        )
        async def read_file(args: dict[str, Any]) -> dict[str, Any]:
            print(f"[tool] Read called with: {args}")
            file_path = args["file_path"]
            offset = args.get("offset", 0)
            limit = args.get("limit", 2000)

            try:
                if offset > 0 or limit < 2000:
                    cmd = f"sed -n '{offset + 1},{offset + limit}p' {_quote(file_path)} | cat -n"
                else:
                    cmd = f"cat -n {_quote(file_path)}"

                output, rc = _run_cmd(cmd)
                if rc != 0:
                    return _error(f"Failed to read file: {output}")
                return _text(output)
            except Exception as e:
                return _error(f"Read error: {e}")

        @tool(
            "Write",
            "Write content to a file.",
            {"file_path": str, "content": str}
        )
        async def write_file(args: dict[str, Any]) -> dict[str, Any]:
            print(f"[tool] Write called with file_path: {args.get('file_path')}")
            file_path = args["file_path"]
            content = args["content"]

            try:
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    _run_cmd(f"mkdir -p {_quote(parent_dir)}")

                output, rc = _run_cmd_stdin(f"cat > {_quote(file_path)}", content.encode())
                if rc != 0:
                    return _error(f"Failed to write file: {output}")
                return _text(f"Successfully wrote to {file_path}")
            except Exception as e:
                return _error(f"Write error: {e}")

        @tool(
            "Edit",
            "Perform search-and-replace edits in a file.",
            {"file_path": str, "old_string": str, "new_string": str, "replace_all": bool}
        )
        async def edit_file(args: dict[str, Any]) -> dict[str, Any]:
            file_path = args["file_path"]
            old_string = args["old_string"]
            new_string = args["new_string"]
            replace_all = args.get("replace_all", False)

            try:
                content, rc = _run_cmd(f"cat {_quote(file_path)}")
                if rc != 0:
                    return _error(f"Failed to read file: {content}")

                if old_string not in content:
                    return _error(f"old_string not found in {file_path}")

                if replace_all:
                    new_content = content.replace(old_string, new_string)
                    count = content.count(old_string)
                else:
                    new_content = content.replace(old_string, new_string, 1)
                    count = 1

                output, rc = _run_cmd_stdin(f"cat > {_quote(file_path)}", new_content.encode())
                if rc != 0:
                    return _error(f"Failed to write file: {output}")
                return _text(f"Replaced {count} occurrence(s) in {file_path}")
            except Exception as e:
                return _error(f"Edit error: {e}")

        @tool(
            "Glob",
            "Find files matching a glob pattern.",
            {"pattern": str, "path": str}
        )
        async def glob_files(args: dict[str, Any]) -> dict[str, Any]:
            pattern = args["pattern"]
            path = args.get("path", ".")

            try:
                cmd = f"cd {_quote(path)} && find . -type f -name {_quote(pattern)} 2>/dev/null | head -100"
                output, rc = _run_cmd(cmd)

                files = output.strip().split("\n")
                files = [f for f in files if f]
                return _text(f"Found {len(files)} files:\n" + "\n".join(files))
            except Exception as e:
                return _error(f"Glob error: {e}")

        @tool(
            "Grep",
            "Search file contents using regex patterns.",
            {"pattern": str, "path": str, "include": str}
        )
        async def grep_files(args: dict[str, Any]) -> dict[str, Any]:
            pattern = args["pattern"]
            path = args.get("path", ".")
            include = args.get("include", "")

            try:
                cmd = f"grep -rn {_quote(pattern)} {_quote(path)}"
                if include:
                    cmd = f"grep -rn --include={_quote(include)} {_quote(pattern)} {_quote(path)}"
                cmd += " 2>/dev/null | head -50"

                output, rc = _run_cmd(cmd)
                if not output.strip():
                    return _text(f"No matches found for pattern: {pattern}")
                return _text(output)
            except Exception as e:
                return _error(f"Grep error: {e}")

        @tool(
            "Bash",
            "Execute a bash command in the workspace.",
            {"command": str, "timeout": int}
        )
        async def run_bash(args: dict[str, Any]) -> dict[str, Any]:
            print(f"[tool] Bash called with: {args}")
            command = args["command"]

            try:
                output, rc = _run_cmd(command)
                if rc != 0:
                    output += f"\n[exit code: {rc}]"
                return _text(output or "(no output)")
            except Exception as e:
                return _error(f"Bash error: {e}")

        @tool(
            "LS",
            "List directory contents.",
            {"path": str, "all": bool}
        )
        async def list_dir(args: dict[str, Any]) -> dict[str, Any]:
            print(f"[tool] LS called with: {args}")
            path = args.get("path", ".")
            show_all = args.get("all", False)

            try:
                cmd = f"ls -la {_quote(path)}" if show_all else f"ls -l {_quote(path)}"
                output, rc = _run_cmd(cmd)
                if rc != 0:
                    return _error(f"Failed to list directory: {output}")
                return _text(output)
            except Exception as e:
                return _error(f"LS error: {e}")

        return create_sdk_mcp_server(
            name="sprite",
            version="1.0.0",
            tools=[read_file, write_file, edit_file, glob_files, grep_files, run_bash, list_dir]
        )


@dataclass
class UserSession:
    """Represents an active user session with sprite and Claude client."""
    user_id: str
    sprite_name: str
    sprite: Any  # Sprite handle
    tool_provider: SpriteToolProvider
    claude_client: Optional[ClaudeSDKClient] = None
    conversation: Optional[Conversation] = None


class SpriteSessionManager:
    """Manages per-user sprites and Claude SDK sessions."""

    def __init__(self):
        sprite_token = os.environ.get("SPRITE_TOKEN")
        if not sprite_token:
            raise RuntimeError("SPRITE_TOKEN environment variable is required")
        self.sprites_client = SpritesClient(token=sprite_token)
        self.sessions: dict[str, UserSession] = {}
        self.db: Optional[Database] = None

    async def initialize(self):
        """Initialize the session manager."""
        self.db = await get_database()
        print("SpriteSessionManager initialized")

    async def get_or_create_session(self, user_id: str) -> UserSession:
        """Get existing session or create new one for user."""
        print(f"[session] get_or_create_session for {user_id}")

        # Check in-memory cache first
        if user_id in self.sessions:
            session = self.sessions[user_id]
            await self.db.update_user_last_active(user_id)
            print(f"[session] found cached session for {user_id}")
            return session

        # Check database for existing user
        user = await self.db.get_user(user_id)

        if user:
            # User exists, get their sprite
            print(f"[session] found existing user {user_id}, sprite: {user.sprite_name}")
            sprite = self.sprites_client.sprite(user.sprite_name)

            # Check if we need to refresh the sprite URL
            if not user.sprite_url or not user.sprite_url.endswith('.sprites.app'):
                print(f"[session] refreshing sprite URL for {user_id}")
                sprite_url = await get_sprite_url(user.sprite_name)
                if sprite_url:
                    await self.db.update_user_sprite_url(user_id, sprite_url)
                    print(f"[session] updated sprite URL: {sprite_url}")
        else:
            # Create new user and sprite
            sprite_name = f"monios-{user_id}"
            print(f"[session] creating new sprite: {sprite_name}")
            try:
                self.sprites_client.create_sprite(sprite_name)
                print(f"[session] sprite created: {sprite_name}")
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
                print(f"[session] sprite already exists: {sprite_name}")

            sprite = self.sprites_client.sprite(sprite_name)

            # Get sprite URL from API and save to database
            sprite_url = await get_sprite_url(sprite_name)
            print(f"[session] sprite URL: {sprite_url}")
            user = await self.db.create_user(user_id, sprite_name, sprite_url)

        # Create session
        tool_provider = SpriteToolProvider(sprite)
        session = UserSession(
            user_id=user_id,
            sprite_name=user.sprite_name,
            sprite=sprite,
            tool_provider=tool_provider
        )

        # Get or create conversation
        conversation = await self.db.get_latest_conversation(user_id)
        if not conversation:
            conversation = await self.db.create_conversation(user_id)
        session.conversation = conversation

        self.sessions[user_id] = session
        return session

    async def get_claude_client(self, user_id: str) -> ClaudeSDKClient:
        """Get or create Claude client for user."""
        session = await self.get_or_create_session(user_id)

        if session.claude_client is None:
            print(f"[session] creating Claude client for {user_id}")
            mcp_server = session.tool_provider.create_mcp_server()
            print(f"[session] MCP server created with tools")

            options = ClaudeAgentOptions(
                mcp_servers={"sprite": mcp_server},
                allowed_tools=[
                    "mcp__sprite__Read",
                    "mcp__sprite__Write",
                    "mcp__sprite__Edit",
                    "mcp__sprite__Glob",
                    "mcp__sprite__Grep",
                    "mcp__sprite__Bash",
                    "mcp__sprite__LS",
                ],
                disallowed_tools=[
                    "Read", "Write", "Edit", "Glob", "Grep", "Bash",
                    "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite",
                ],
                permission_mode="acceptEdits",
                # Resume from previous session if available
                resume=session.conversation.session_id if session.conversation else None,
            )

            session.claude_client = ClaudeSDKClient(options=options)
            print(f"[session] connecting Claude client for {user_id}")
            await session.claude_client.connect()
            print(f"[session] Claude client connected for {user_id}")

        return session.claude_client

    async def chat(
        self,
        user_id: str,
        message: str,
        on_text: Optional[callable] = None,
        on_tool_use: Optional[callable] = None,
        on_tool_result: Optional[callable] = None,
    ) -> tuple[str, list[dict]]:
        """
        Send a message and get response.
        Returns (response_text, tool_uses).
        """
        print(f"[chat] starting chat for {user_id}: {message[:50]}...")
        session = await self.get_or_create_session(user_id)
        client = await self.get_claude_client(user_id)

        # Store user message
        await self.db.add_message(session.conversation.id, "user", message)

        # Send to Claude
        print(f"[chat] sending query to Claude")
        await client.query(message)
        print(f"[chat] query sent, receiving response")

        response_text = ""
        tool_uses = []
        session_id = None

        async for msg in client.receive_response():
            print(f"[chat] received message: {type(msg).__name__}")
            if isinstance(msg, AssistantMessage):
                print(f"[chat] AssistantMessage with {len(msg.content)} blocks")
                for block in msg.content:
                    print(f"[chat] block type: {type(block).__name__}")
                    if isinstance(block, TextBlock):
                        print(f"[chat] TextBlock: {block.text[:50]}...")
                        response_text += block.text
                        if on_text:
                            on_text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        print(f"[chat] ToolUseBlock: name={block.name}, id={block.id}")
                        print(f"[chat] ToolUseBlock input: {block.input}")
                        tool_use = {"name": block.name, "input": block.input, "id": block.id}
                        tool_uses.append(tool_use)
                        if on_tool_use:
                            on_tool_use(tool_use)
                    elif isinstance(block, ToolResultBlock):
                        print(f"[chat] ToolResultBlock: {block.tool_use_id}")
                        # Find matching tool use and add result
                        for tu in tool_uses:
                            if tu.get("id") == block.tool_use_id:
                                tu["result"] = block.content
                                tu["is_error"] = block.is_error
                        if on_tool_result:
                            on_tool_result(block)

            elif isinstance(msg, ResultMessage):
                print(f"[chat] ResultMessage: session_id={msg.session_id}")
                session_id = msg.session_id

        print(f"[chat] done receiving, response length: {len(response_text)}")
        # Store assistant response
        await self.db.add_message(
            session.conversation.id,
            "assistant",
            response_text,
            tool_uses=tool_uses if tool_uses else None
        )

        # Update session ID for resume
        if session_id and session.conversation:
            await self.db.update_conversation_session(session.conversation.id, session_id)
            session.conversation.session_id = session_id

        return response_text, tool_uses

    async def new_conversation(self, user_id: str) -> Conversation:
        """Start a new conversation for user."""
        session = await self.get_or_create_session(user_id)

        # Disconnect existing client
        if session.claude_client:
            await session.claude_client.disconnect()
            session.claude_client = None

        # Create new conversation
        conversation = await self.db.create_conversation(user_id)
        session.conversation = conversation

        return conversation

    async def get_conversation_history(self, user_id: str, limit: int = 100) -> list[Message]:
        """Get message history for user's current conversation."""
        session = await self.get_or_create_session(user_id)
        if session.conversation:
            return await self.db.get_conversation_messages(session.conversation.id, limit)
        return []

    async def cleanup_session(self, user_id: str):
        """Clean up a user's session."""
        if user_id in self.sessions:
            session = self.sessions[user_id]
            if session.claude_client:
                try:
                    await asyncio.wait_for(session.claude_client.disconnect(), timeout=2.0)
                except asyncio.TimeoutError:
                    print(f"[session] disconnect timed out for {user_id}")
                except Exception as e:
                    print(f"[session] disconnect error for {user_id}: {e}")
            del self.sessions[user_id]

    async def cleanup_all(self):
        """Clean up all sessions."""
        print(f"[session] cleaning up {len(self.sessions)} sessions")
        for user_id in list(self.sessions.keys()):
            await self.cleanup_session(user_id)
        print("[session] cleanup complete")


# Global session manager instance
_manager: Optional[SpriteSessionManager] = None


async def get_session_manager() -> SpriteSessionManager:
    """Get or create session manager instance."""
    global _manager
    if _manager is None:
        _manager = SpriteSessionManager()
        await _manager.initialize()
    return _manager


async def cleanup_session_manager():
    """Clean up session manager."""
    global _manager
    if _manager:
        await _manager.cleanup_all()
        _manager = None
