"""Manages per-user Modal sandboxes.

Each user gets their own isolated sandbox with:
- Their own Claude Code instance
- Their own persistent volume at /workspace
- Their own session state
"""

import modal
import httpx
import asyncio
from typing import Optional

# Reference to the main app - will be set by modal_app.py
_app: Optional[modal.App] = None

# Sandbox image with Claude Code + our sandbox server
_sandbox_image: Optional[modal.Image] = None

# Secrets for Claude API access
_secrets: Optional[list] = None

# Shared code volume (contains sandbox_server.py)
_code_volume: Optional[modal.Volume] = None

# Track active sandboxes: user_id -> (sandbox, tunnel_url)
_active_sandboxes: dict[str, tuple[modal.Sandbox, str]] = {}


def init(
    app: modal.App,
    sandbox_image: modal.Image,
    secrets: list = None,
    code_volume: modal.Volume = None,
):
    """Initialize the sandbox manager with app and image references."""
    global _app, _sandbox_image, _secrets, _code_volume
    _app = app
    _sandbox_image = sandbox_image
    _secrets = secrets or []
    _code_volume = code_volume


async def get_or_create_sandbox(user_id: str) -> tuple[modal.Sandbox, str]:
    """Get existing sandbox or create new one for user. Returns (sandbox, tunnel_url)."""
    global _active_sandboxes

    print(f"[sandbox_manager] get_or_create_sandbox for user: {user_id}")

    # Check if we have an active sandbox
    if user_id in _active_sandboxes:
        sb, tunnel_url = _active_sandboxes[user_id]
        # Check if still running
        if sb.poll() is None:
            print(f"[sandbox_manager] Reusing existing sandbox for {user_id}")
            return sb, tunnel_url
        else:
            # Sandbox terminated, remove from cache
            print(f"[sandbox_manager] Sandbox terminated, creating new one for {user_id}")
            del _active_sandboxes[user_id]

    # Create user's volume (persistent across sandbox restarts)
    print(f"[sandbox_manager] Creating volume for user: {user_id}")
    user_volume = modal.Volume.from_name(
        f"monios-user-{user_id}",
        create_if_missing=True
    )

    # Create new sandbox with secrets for Claude API
    print(f"[sandbox_manager] Creating sandbox for user: {user_id}")
    volumes = {"/workspace": user_volume}
    if _code_volume:
        volumes["/code"] = _code_volume

    sb = modal.Sandbox.create(
        app=_app,
        image=_sandbox_image,
        secrets=_secrets,
        env={
            "IS_SANDBOX": "1",
            "HOME": "/workspace",
        },
        timeout=3600,  # 1 hour max lifetime
        idle_timeout=300,  # 5 min idle = terminate
        volumes=volumes,
        cpu=1.0,
        memory=512,
        encrypted_ports=[8080],  # Expose sandbox server port
    )
    print(f"[sandbox_manager] Sandbox created: {sb.object_id}")

    # Start the sandbox server inside (don't wait for it to complete)
    print(f"[sandbox_manager] Starting sandbox_server.py")
    run_cmd = getattr(sb, "exec")  # Modal Sandbox API method

    # First check if the file exists
    check_process = run_cmd("ls", "-la", "/code/")
    print(f"[sandbox_manager] /code/ contents: {check_process.stdout.read()}")

    # Start the server from the shared code volume
    process = run_cmd("python", "/code/sandbox_server.py")
    print(f"[sandbox_manager] Process started: {process}")

    # Give it a moment to start and check for immediate errors
    import time
    time.sleep(2)

    # Check if process has early output or errors
    try:
        # Non-blocking read of any available output
        if process.poll() is not None:
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            print(f"[sandbox_manager] Process exited early! returncode={process.poll()}")
            print(f"[sandbox_manager] stdout: {stdout}")
            print(f"[sandbox_manager] stderr: {stderr}")
    except Exception as e:
        print(f"[sandbox_manager] Could not read process output: {e}")

    # Get tunnel URL for HTTP access
    print(f"[sandbox_manager] Getting tunnel...")
    tunnels = sb.tunnels()
    print(f"[sandbox_manager] Available tunnels: {tunnels}")
    tunnel = tunnels.get(8080)
    if not tunnel:
        raise Exception(f"No tunnel on port 8080. Available: {list(tunnels.keys())}")
    tunnel_url = tunnel.url
    print(f"[sandbox_manager] Tunnel URL: {tunnel_url}")

    # Wait for server to be ready
    await _wait_for_ready(tunnel_url)

    # Cache the sandbox
    _active_sandboxes[user_id] = (sb, tunnel_url)

    return sb, tunnel_url


async def _wait_for_ready(tunnel_url: str, timeout: float = 60.0):
    """Wait for sandbox server to be ready."""
    print(f"[sandbox_manager] Waiting for sandbox to be ready at {tunnel_url}")
    async with httpx.AsyncClient() as client:
        start = asyncio.get_event_loop().time()
        attempt = 0
        last_error = None
        while True:
            attempt += 1
            try:
                resp = await client.get(f"{tunnel_url}/health", timeout=5.0)
                print(f"[sandbox_manager] Health check attempt {attempt}: status={resp.status_code}")
                if resp.status_code == 200:
                    print(f"[sandbox_manager] Sandbox ready!")
                    return
            except Exception as e:
                last_error = str(e)
                if attempt % 5 == 0:  # Log every 5th attempt
                    print(f"[sandbox_manager] Health check attempt {attempt} failed: {e}")

            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                raise TimeoutError(f"Sandbox server did not start in {timeout}s. Last error: {last_error}")

            await asyncio.sleep(1.0)


async def send_message(user_id: str, message: str) -> tuple[str, str, list[dict[str, object]]]:
    """Send a message to the user's sandbox and get response."""
    sb, tunnel_url = await get_or_create_sandbox(user_id)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{tunnel_url}/chat",
            json={"message": message},
            timeout=120.0,  # 2 min timeout for Claude responses
        )
        if resp.status_code != 200:
            # Surface sandbox errors directly for debugging
            try:
                error_payload = resp.json()
            except Exception:
                error_payload = {"error": resp.text}
            raise Exception(
                f"Sandbox error status={resp.status_code} payload={error_payload}"
            )

        data = resp.json()

        if "error" in data:
            raise Exception(data["error"])

        return data.get("content", ""), data.get("session_id", ""), data.get("tool_events", [])


async def clear_session(user_id: str) -> bool:
    """Clear session for a user. Optionally terminate sandbox."""
    if user_id not in _active_sandboxes:
        return False

    sb, tunnel_url = _active_sandboxes[user_id]

    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{tunnel_url}/clear", timeout=10.0)
    except:
        pass

    return True


async def terminate_sandbox(user_id: str) -> bool:
    """Terminate a user's sandbox completely."""
    if user_id not in _active_sandboxes:
        return False

    sb, _ = _active_sandboxes[user_id]

    try:
        sb.terminate()
    except:
        pass

    del _active_sandboxes[user_id]
    return True
