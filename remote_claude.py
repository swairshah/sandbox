#!/usr/bin/env python3
"""
Remote Claude CLI - A REPL that runs Claude with tools proxied to a Sprite sandbox.

Usage:
    export SPRITE_TOKEN="your-token"
    export ANTHROPIC_API_KEY="your-key"
    python remote_claude.py [sprite-name]

The sprite will be created if it doesn't exist.
"""

import os
import sys
import asyncio
import json
from typing import Any
from dotenv import load_dotenv
load_dotenv()

SPRITE_TOKEN = os.environ["SPRITE_TOKEN"]

from sprites import SpritesClient
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


class SpriteToolProvider:
    """Provides Claude tools that proxy to a remote Sprite sandbox."""

    def __init__(self, sprite):
        self.sprite = sprite

    def create_mcp_server(self):
        """Create an MCP server with all tools proxied to the sprite."""

        sprite = self.sprite  # Capture for closures

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
            "Read file contents from the workspace. Supports text files, images, and more.",
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

                output, rc = _run_cmd(cmd)

                if rc != 0:
                    return _error(f"Failed to read file: {output}")

                return _text(output)
            except Exception as e:
                return _error(f"Read error: {e}")

        @tool(
            "Write",
            "Write content to a file. Creates the file if it doesn't exist.",
            {"file_path": str, "content": str}
        )
        async def write_file(args: dict[str, Any]) -> dict[str, Any]:
            file_path = args["file_path"]
            content = args["content"]

            try:
                # Ensure parent directory exists
                parent_dir = os.path.dirname(file_path)
                if parent_dir:
                    _run_cmd(f"mkdir -p {_quote(parent_dir)}")

                # Write using stdin to handle special characters
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
                # Read current content
                content, rc = _run_cmd(f"cat {_quote(file_path)}")
                if rc != 0:
                    return _error(f"Failed to read file: {content}")

                # Check if old_string exists
                if old_string not in content:
                    return _error(f"old_string not found in {file_path}")

                # Perform replacement
                if replace_all:
                    new_content = content.replace(old_string, new_string)
                    count = content.count(old_string)
                else:
                    new_content = content.replace(old_string, new_string, 1)
                    count = 1

                # Write back
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
                files = [f for f in files if f]  # Filter empty

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

        # ============== Execution ==============

        @tool(
            "Bash",
            "Execute a bash command in the workspace.",
            {"command": str, "timeout": int}
        )
        async def run_bash(args: dict[str, Any]) -> dict[str, Any]:
            command = args["command"]

            try:
                output, rc = _run_cmd(command)

                if rc != 0:
                    output += f"\n[exit code: {rc}]"

                return _text(output or "(no output)")
            except Exception as e:
                return _error(f"Bash error: {e}")

        # ============== Notebook (stub) ==============

        @tool(
            "NotebookEdit",
            "Edit Jupyter notebook cells.",
            {"notebook_path": str, "cell_id": str, "new_source": str, "cell_type": str, "edit_mode": str}
        )
        async def notebook_edit(args: dict[str, Any]) -> dict[str, Any]:
            # For now, just a stub - notebooks are complex
            return _error("NotebookEdit not yet implemented for remote sprites")

        # ============== List Directory ==============

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
                output, rc = _run_cmd(cmd)

                if rc != 0:
                    return _error(f"Failed to list directory: {output}")

                return _text(output)
            except Exception as e:
                return _error(f"LS error: {e}")

        # Create the MCP server with all tools
        return create_sdk_mcp_server(
            name="sprite",
            version="1.0.0",
            tools=[
                read_file,
                write_file,
                edit_file,
                glob_files,
                grep_files,
                run_bash,
                notebook_edit,
                list_dir,
            ]
        )


def _quote(s: str) -> str:
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _text(text: str) -> dict[str, Any]:
    """Return a successful text response."""
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    """Return an error response."""
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


class RemoteClaude:
    """REPL for Claude with tools proxied to a Sprite."""

    def __init__(self, sprite_name: str):
        self.sprite_name = sprite_name
        self.sprites_client = SpritesClient(token=os.environ["SPRITE_TOKEN"])
        self.sprite = None
        self.claude_client = None
        self.tool_provider = None

    async def setup(self):
        """Initialize the sprite and Claude client."""
        print(f"Connecting to sprite: {self.sprite_name}")

        # Create sprite if needed
        try:
            self.sprites_client.create_sprite(self.sprite_name)
            print(f"  Created new sprite: {self.sprite_name}")
        except Exception as e:
            if "already exists" in str(e).lower():
                print(f"  Using existing sprite: {self.sprite_name}")
            else:
                raise

        self.sprite = self.sprites_client.sprite(self.sprite_name)

        # Test connection
        output = self.sprite.command("echo", "Sprite connected!").output()
        print(f"  {output.decode().strip()}")

        # Create tool provider
        self.tool_provider = SpriteToolProvider(self.sprite)
        mcp_server = self.tool_provider.create_mcp_server()

        # Configure Claude with sprite-proxied tools
        options = ClaudeAgentOptions(
            mcp_servers={"sprite": mcp_server},
            allowed_tools=[
                "mcp__sprite__Read",
                "mcp__sprite__Write",
                "mcp__sprite__Edit",
                "mcp__sprite__Glob",
                "mcp__sprite__Grep",
                "mcp__sprite__Bash",
                "mcp__sprite__NotebookEdit",
                "mcp__sprite__LS",
            ],
            # Disable built-in tools so Claude uses our MCP proxied versions
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
            permission_mode="acceptEdits",
        )

        self.claude_client = ClaudeSDKClient(options=options)
        await self.claude_client.connect()
        print("  Claude connected with sprite-proxied tools\n")

    async def chat(self, message: str) -> str:
        """Send a message and get response."""
        await self.claude_client.query(message)

        response_text = ""
        async for msg in self.claude_client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        print(f"  > {block.name}: {_truncate(str(block.input), 80)}")
                    elif isinstance(block, ToolResultBlock):
                        status = "x" if block.is_error else "|"
                        print(f"  {status} {_truncate(str(block.content), 80)}")
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    response_text += f"\n[Error: {msg.result}]"

        return response_text

    async def repl(self):
        """Run the interactive REPL."""
        print("=" * 60)
        print("Remote Claude REPL")
        print("Type your messages, 'exit' to quit, 'clear' for new session")
        print("=" * 60)
        print()

        while True:
            try:
                user_input = input("\033[1;32mYou:\033[0m ").strip()

                if not user_input:
                    continue

                if user_input.lower() == "exit":
                    print("\nGoodbye.")
                    break

                if user_input.lower() == "clear":
                    await self.claude_client.disconnect()
                    await self.setup()
                    print("Session cleared.\n")
                    continue

                print()
                response = await self.chat(user_input)
                print(f"\n{response}\n")

            except KeyboardInterrupt:
                print("\n\nInterrupted. Goodbye.")
                break
            except EOFError:
                print("\n\nGoodbye.")
                break
            except Exception as e:
                print(f"\nError: {e}\n")

    async def cleanup(self):
        """Clean up resources."""
        if self.claude_client:
            await self.claude_client.disconnect()


def _truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis."""
    s = s.replace("\n", " ")
    if len(s) > max_len:
        return s[:max_len - 3] + "..."
    return s


async def main():
    # Get sprite name from args or default
    sprite_name = sys.argv[1] if len(sys.argv) > 1 else f"claude-sandbox-{os.getenv('USER', 'default')}"

    # Check required env vars
    if not os.environ.get("SPRITE_TOKEN"):
        print("Error: SPRITE_TOKEN environment variable required")
        print("       Get your token from https://sprites.dev")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable required")
        sys.exit(1)

    remote = RemoteClaude(sprite_name)

    try:
        await remote.setup()
        await remote.repl()
    finally:
        await remote.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
