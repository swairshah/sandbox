# MCP Version Compatibility Debug Notes

## Problem

Claude Agent SDK's custom MCP tools weren't being registered - Claude only saw built-in tools (Task, KillShell, etc.) instead of the custom MCP-proxied tools (Read, Write, Bash, etc.).

## Root Cause

Dependency version conflicts caused pip to resolve to `mcp==0.9.1`, which has an incompatible API with both:
1. The SDK's `create_sdk_mcp_server()` function
2. Our custom fallback wrapper

### The Conflict Chain

```
requirements.txt:
  pydantic==2.5.3
  uvicorn[standard]==0.27.0
  claude-agent-sdk

claude-agent-sdk requires mcp
mcp>=1.20.0 requires:
  - pydantic>=2.11.0  ❌ conflicts with pydantic==2.5.3
  - uvicorn>=0.31.1   ❌ conflicts with uvicorn==0.27.0

Result: pip resolves to mcp==0.9.1 (old version that fits constraints)
```

### Why mcp 0.9.x Fails

1. **Server API difference**: `mcp.server.Server(name, version=...)` - the `version` parameter doesn't exist in 0.9.x
2. **Tool registration**: The `@server.list_tools()` and `@server.call_tool()` decorator pattern works differently
3. **SDK incompatibility**: `create_sdk_mcp_server()` from claude-agent-sdk assumes mcp 1.x API

A try/except fallback for the `version` parameter wasn't enough - the entire tool registration mechanism differs between versions.

## Solution

Update pinned dependencies to allow mcp 1.20+:

```txt
# Before (causes mcp==0.9.1)
pydantic==2.5.3
uvicorn[standard]==0.27.0

# After (allows mcp>=1.20.0)  
pydantic>=2.11.0
uvicorn[standard]>=0.31.0
```

Then use the SDK's built-in `create_sdk_mcp_server()` directly:

```python
from claude_agent_sdk import create_sdk_mcp_server, tool

@tool("Read", "Read file contents", {"file_path": str})
async def read_file(args):
    ...

mcp_server = create_sdk_mcp_server(
    name="modal",
    version="1.0.0",
    tools=[read_file, ...]
)
```

## How to Debug This in Future

1. **Check installed mcp version on Modal**:
   ```bash
   pip freeze | grep mcp
   ```

2. **Check what tools Claude sees** - Ask Claude "what tools do you have access to?"

3. **If only built-in tools appear**, it's likely an MCP registration issue

4. **Check for dependency conflicts during Modal deploy** - Look for pip resolver warnings

## Reference

- Working example: `/Users/swair/work/agents/boxed-claude/modal_claude.py`
- claude-agent-sdk docs: https://platform.claude.com/docs/en/agent-sdk/custom-tools
