"""
Modal session manager - handles per-user Modal sandboxes and Claude SDK clients.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
    tool,
)

import sandbox_manager

SYSTEM_PROMPT = "You are a helpful assistant in a terminal-aesthetic chat app called Monios. Keep responses concise and friendly."

_SESSION_FILE = Path(__file__).parent / ".modal_session_ids.json"
_session_ids: dict[str, str] = {}


def _load_session_ids() -> None:
    if _SESSION_FILE.exists():
        try:
            data = json.loads(_SESSION_FILE.read_text())
            if isinstance(data, dict):
                _session_ids.clear()
                _session_ids.update({str(k): str(v) for k, v in data.items()})
        except (OSError, ValueError, json.JSONDecodeError):
            _session_ids.clear()


def _save_session_ids() -> None:
    try:
        _SESSION_FILE.write_text(json.dumps(_session_ids, indent=2))
    except OSError:
        pass


def _quote(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _text(text: str) -> dict[str, Any]:
    """Return a successful text response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    """Return an error response."""
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _combine_output(stdout: str, stderr: str) -> str:
    if not stderr:
        return stdout
    if stdout and not stdout.endswith("\n"):
        return stdout + "\n" + stderr
    return stdout + stderr


def _create_sdk_mcp_server(name: str, tools: list, version: str = "1.0.0") -> dict[str, Any]:
    """Create an MCP server from a list of tools.
    
    Handles both old mcp (0.9.x) and new mcp (1.x) versions.
    """
    from mcp.server import Server
    from mcp.types import ImageContent, TextContent, Tool

    # Old mcp versions don't accept 'version' parameter
    try:
        server = Server(name, version=version)
    except TypeError:
        server = Server(name)

    if tools:
        tool_map = {tool_def.name: tool_def for tool_def in tools}

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            tool_list = []
            for tool_def in tools:
                if isinstance(tool_def.input_schema, dict):
                    if "type" in tool_def.input_schema and "properties" in tool_def.input_schema:
                        schema = tool_def.input_schema
                    else:
                        properties = {}
                        for param_name, param_type in tool_def.input_schema.items():
                            if param_type is str:
                                properties[param_name] = {"type": "string"}
                            elif param_type is int:
                                properties[param_name] = {"type": "integer"}
                            elif param_type is float:
                                properties[param_name] = {"type": "number"}
                            elif param_type is bool:
                                properties[param_name] = {"type": "boolean"}
                            else:
                                properties[param_name] = {"type": "string"}
                        schema = {
                            "type": "object",
                            "properties": properties,
                            "required": list(properties.keys()),
                        }
                else:
                    schema = {"type": "object", "properties": {}}

                tool_list.append(
                    Tool(
                        name=tool_def.name,
                        description=tool_def.description,
                        inputSchema=schema,
                    )
                )
            return tool_list

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
            if name not in tool_map:
                raise ValueError(f"Tool '{name}' not found")

            tool_def = tool_map[name]
            result = await tool_def.handler(arguments)

            content: list[TextContent | ImageContent] = []
            if "content" in result:
                for item in result["content"]:
                    if item.get("type") == "text":
                        content.append(TextContent(type="text", text=item["text"]))
                    if item.get("type") == "image":
                        content.append(
                            ImageContent(
                                type="image",
                                data=item["data"],
                                mimeType=item["mimeType"],
                            )
                        )

            return content

    return {"type": "sdk", "name": name, "instance": server}


class ModalToolProvider:
    """Provides Claude tools that proxy to a Modal sandbox."""

    def __init__(self, user_id: str, workdir: str = "/workspace"):
        self.user_id = user_id
        self.workdir = workdir

    def create_mcp_server(self):
        """Create an MCP server with all tools proxied to the sandbox."""
        user_id = self.user_id
        workdir = self.workdir

        async def _run_cmd(cmd: str) -> tuple[str, int]:
            try:
                sandbox, _, _ = await sandbox_manager.get_or_create_sandbox(user_id)
                process = sandbox.exec("bash", "-c", cmd)
                stdout = process.stdout.read() if process.stdout else ""
                stderr = process.stderr.read() if process.stderr else ""
                rc = process.wait()
                return _combine_output(stdout, stderr), rc
            except Exception as e:
                return str(e), 1

        async def _run_cmd_stdin(cmd: str, stdin_data: str) -> tuple[str, int]:
            try:
                sandbox, _, _ = await sandbox_manager.get_or_create_sandbox(user_id)
                process = sandbox.exec("bash", "-c", cmd)
                process.stdin.write(stdin_data)
                process.stdin.write_eof()
                process.stdin.drain()
                stdout = process.stdout.read() if process.stdout else ""
                stderr = process.stderr.read() if process.stderr else ""
                rc = process.wait()
                return _combine_output(stdout, stderr), rc
            except Exception as e:
                return str(e), 1

        @tool(
            "Read",
            "Read file contents from the workspace.",
            {"file_path": str, "offset": int, "limit": int}
        )
        async def read_file(args: dict[str, Any]) -> dict[str, Any]:
            file_path = args["file_path"]
            offset = args.get("offset", 0)
            limit = args.get("limit", 2000)

            try:
                if offset > 0 or limit < 2000:
                    cmd = f"sed -n '{offset + 1},{offset + limit}p' {_quote(file_path)} | cat -n"
                else:
                    cmd = f"cat -n {_quote(file_path)}"

                output, rc = await _run_cmd(cmd)
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
            file_path = args["file_path"]
            content = args["content"]

            try:
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    await _run_cmd(f"mkdir -p {_quote(parent_dir)}")

                output, rc = await _run_cmd_stdin(f"cat > {_quote(file_path)}", content)
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
                content, rc = await _run_cmd(f"cat {_quote(file_path)}")
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

                output, rc = await _run_cmd_stdin(f"cat > {_quote(file_path)}", new_content)
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
                output, rc = await _run_cmd(cmd)
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

                output, rc = await _run_cmd(cmd)
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
            command = args["command"]

            try:
                output, rc = await _run_cmd(command)
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
            path = args.get("path", ".")
            show_all = args.get("all", False)

            try:
                cmd = f"ls -la {_quote(path)}" if show_all else f"ls -l {_quote(path)}"
                output, rc = await _run_cmd(cmd)
                if rc != 0:
                    return _error(f"Failed to list directory: {output}")
                return _text(output)
            except Exception as e:
                return _error(f"LS error: {e}")

        return _create_sdk_mcp_server(
            name="modal",
            tools=[read_file, write_file, edit_file, glob_files, grep_files, run_bash, list_dir],
        )


@dataclass
class UserSession:
    user_id: str
    tool_provider: ModalToolProvider
    claude_client: Optional[ClaudeSDKClient] = None
    session_id: Optional[str] = None


class ModalSessionManager:
    """Manages per-user Modal sandboxes and Claude SDK sessions."""

    def __init__(self):
        _load_session_ids()
        self.sessions: dict[str, UserSession] = {}

    async def get_or_create_session(self, user_id: str) -> UserSession:
        if user_id in self.sessions:
            return self.sessions[user_id]

        await sandbox_manager.get_or_create_sandbox(user_id)
        tool_provider = ModalToolProvider(user_id)

        session = UserSession(
            user_id=user_id,
            tool_provider=tool_provider,
            session_id=_session_ids.get(user_id),
        )
        self.sessions[user_id] = session
        return session

    async def get_claude_client(self, user_id: str) -> ClaudeSDKClient:
        session = await self.get_or_create_session(user_id)

        if session.claude_client is None:
            mcp_server = session.tool_provider.create_mcp_server()

            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                mcp_servers={"modal": mcp_server},
                allowed_tools=[
                    "mcp__modal__Read",
                    "mcp__modal__Write",
                    "mcp__modal__Edit",
                    "mcp__modal__Glob",
                    "mcp__modal__Grep",
                    "mcp__modal__Bash",
                    "mcp__modal__LS",
                ],
                disallowed_tools=[
                    "Read",
                    "Write",
                    "Edit",
                    "Glob",
                    "Grep",
                    "Bash",
                    "NotebookEdit",
                    "WebFetch",
                    "WebSearch",
                    "Task",
                    "TodoWrite",
                ],
                permission_mode="bypassPermissions",
                max_turns=10,
                cwd="/code/workspace",
            )

            session.claude_client = ClaudeSDKClient(options=options)
            await session.claude_client.connect()

        return session.claude_client

    async def chat(
        self,
        user_id: str,
        message: str,
        session_id: str | None = None,
        on_text: Optional[callable] = None,
        on_tool_use: Optional[callable] = None,
        on_tool_result: Optional[callable] = None,
    ) -> tuple[str, str | None, list[dict[str, object]]]:
        session = await self.get_or_create_session(user_id)
        client = await self.get_claude_client(user_id)

        effective_session_id = session_id or session.session_id
        if effective_session_id:
            await client.query(prompt=message, session_id=effective_session_id)
        else:
            await client.query(prompt=message)

        response_text = ""
        tool_events: list[dict[str, object]] = []
        new_session_id = None

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
                        if on_text:
                            on_text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        event = {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input,
                            "tool_use_id": block.id,
                        }
                        tool_events.append(event)
                        if on_tool_use:
                            on_tool_use(event)
                    elif isinstance(block, ToolResultBlock):
                        event = {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                        tool_events.append(event)
                        if on_tool_result:
                            on_tool_result(block)

            elif isinstance(msg, ResultMessage):
                new_session_id = msg.session_id

        if new_session_id:
            session.session_id = new_session_id
            _session_ids[user_id] = new_session_id
            _save_session_ids()

        return response_text, new_session_id, tool_events

    async def clear_session(self, user_id: str) -> bool:
        if user_id not in self.sessions:
            return False

        session = self.sessions[user_id]
        if session.claude_client:
            try:
                await session.claude_client.disconnect()
            except Exception:
                pass
            session.claude_client = None

        session.session_id = None
        if user_id in _session_ids:
            del _session_ids[user_id]
            _save_session_ids()

        return True

    async def cleanup_all(self) -> None:
        for user_id in list(self.sessions.keys()):
            await self.clear_session(user_id)


_manager: Optional[ModalSessionManager] = None


async def get_session_manager() -> ModalSessionManager:
    global _manager
    if _manager is None:
        _manager = ModalSessionManager()
    return _manager


async def cleanup_session_manager() -> None:
    global _manager
    if _manager:
        await _manager.cleanup_all()
        _manager = None


async def get_response(
    message: str,
    user_id: str,
    session_id: str | None = None,
) -> tuple[str, str | None, list[dict[str, object]]]:
    manager = await get_session_manager()
    return await manager.chat(user_id, message, session_id=session_id)


async def clear_session(user_id: str) -> bool:
    manager = await get_session_manager()
    return await manager.clear_session(user_id)
