"""Manages per-user Modal sandboxes.

Each user gets their own isolated sandbox with:
- Their own Claude Code instance
- Their own persistent volume at /workspace
- Their own session state
"""

import modal
import hashlib
import re
from pathlib import Path
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


def _run_exec(sb: modal.Sandbox, *args: str) -> tuple[str, str, int]:
    process = sb.exec(*args)
    stdout = process.stdout.read() if process.stdout else ""
    stderr = process.stderr.read() if process.stderr else ""
    rc = process.wait()
    return stdout, stderr, rc


def _ensure_dependency(sb: modal.Sandbox, package: str, module: str) -> None:
    _, _, rc = _run_exec(sb, "python", "-c", f"import {module}")
    if rc == 0:
        return
    print(f"[sandbox_manager] Installing {package} (missing: {module})")
    stdout, stderr, install_rc = _run_exec(
        sb, "python", "-m", "pip", "install", "--no-cache-dir", package
    )
    if install_rc != 0:
        raise RuntimeError(f"Failed to install {package}: {stdout}{stderr}")


def _find_sandbox_server(sb: modal.Sandbox) -> str | None:
    candidates = [
        "/sandbox_server.py",
        "/code/sandbox_server.py",
        "/app/sandbox_server.py",
        "/root/app/sandbox_server.py",
        "/root/sandbox_server.py",
    ]
    for path in candidates:
        _, _, rc = _run_exec(sb, "bash", "-c", f'test -f "{path}"')
        if rc == 0:
            return path
    return None


def _local_sandbox_server_path() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / "sandbox_server.py",
        Path("/code/sandbox_server.py"),
        Path("/root/sandbox_server.py"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _upload_sandbox_server(sb: modal.Sandbox) -> str:
    local_path = _local_sandbox_server_path()
    if not local_path:
        raise RuntimeError("sandbox_server.py not found in API container")
    content = local_path.read_text()
    process = sb.exec("bash", "-c", "cat > /sandbox_server.py")
    process.stdin.write(content)
    process.stdin.write_eof()
    process.stdin.drain()
    rc = process.wait()
    if rc != 0:
        stderr = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"Failed to upload sandbox_server.py: {stderr}")
    return "/sandbox_server.py"


def _sanitize_name(user_id: str, prefix: str = "monios-user") -> str:
    """Sanitize user_id for use in Modal resource names (volumes, sandboxes)."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", user_id).strip("-")
    if not slug:
        slug = "user"
    suffix = hashlib.sha1(user_id.encode()).hexdigest()[:8]
    # Keep under 64 chars
    max_slug_len = 64 - len(prefix) - len(suffix) - 2
    if max_slug_len < 1:
        slug = "user"
        max_slug_len = 64 - len(prefix) - len(suffix) - 2
    if len(slug) > max_slug_len:
        slug = slug[:max_slug_len].rstrip("-")
    return f"{prefix}-{slug}-{suffix}"


def _sanitize_volume_name(user_id: str) -> str:
    return _sanitize_name(user_id, "monios-user")


def _sanitize_sandbox_name(user_id: str) -> str:
    return _sanitize_name(user_id, "monios-sb")


def init(
    app: Optional[modal.App],
    sandbox_image: Optional[modal.Image],
    secrets: list = None,
    code_volume: Optional[modal.Volume] = None,
):
    """Initialize the sandbox manager with app and image references."""
    global _app, _sandbox_image, _secrets, _code_volume
    _app = app
    _sandbox_image = sandbox_image
    _secrets = secrets or []
    _code_volume = code_volume


async def get_or_create_sandbox(user_id: str) -> tuple[modal.Sandbox, str, str | None]:
    """Get existing sandbox or create new one for user. Returns (sandbox, http_url, terminal_url)."""
    global _active_sandboxes

    print(f"[sandbox_manager] get_or_create_sandbox for user: {user_id}")

    if _sandbox_image is None:
        raise RuntimeError("sandbox_manager.init must set sandbox_image before creating sandboxes")

    sandbox_name = _sanitize_sandbox_name(user_id)
    app_name = _app.name if _app else "monios-api"

    # First, check local cache
    if user_id in _active_sandboxes:
        sb, http_url, terminal_url = _active_sandboxes[user_id]
        # Check if still running
        if sb.poll() is None:
            print(f"[sandbox_manager] Reusing cached sandbox for {user_id}")
            return sb, http_url, terminal_url
        else:
            # Sandbox terminated, remove from cache
            print(f"[sandbox_manager] Cached sandbox terminated for {user_id}")
            del _active_sandboxes[user_id]

    # Helper to lookup existing sandbox and get its URLs
    def _try_get_existing_sandbox():
        try:
            sb = modal.Sandbox.from_name(app_name, sandbox_name)
            if sb.poll() is None:
                print(f"[sandbox_manager] Found existing named sandbox for {user_id}: {sb.object_id}")
                tunnels = sb.tunnels()
                http_tunnel = tunnels.get(8080)
                terminal_tunnel = tunnels.get(8081)
                if http_tunnel:
                    return sb, http_tunnel.url, terminal_tunnel.url if terminal_tunnel else None
                print(f"[sandbox_manager] Named sandbox found but no tunnel")
            else:
                print(f"[sandbox_manager] Named sandbox exists but not running")
        except modal.exception.NotFoundError:
            print(f"[sandbox_manager] No existing named sandbox for {user_id}")
        except Exception as e:
            print(f"[sandbox_manager] Error looking up named sandbox: {e}")
        return None

    # Try to find existing named sandbox from Modal
    result = _try_get_existing_sandbox()
    if result:
        sb, http_url, terminal_url = result
        _active_sandboxes[user_id] = (sb, http_url, terminal_url)
        print(f"[sandbox_manager] Reusing named sandbox: http={http_url}, terminal={terminal_url}")
        return sb, http_url, terminal_url

    # Create user's volume (persistent across sandbox restarts)
    print(f"[sandbox_manager] Creating volume for user: {user_id}")
    user_volume = modal.Volume.from_name(
        _sanitize_volume_name(user_id),
        create_if_missing=True
    )

    # Create new sandbox with secrets for Claude API
    print(f"[sandbox_manager] Creating named sandbox '{sandbox_name}' for user: {user_id}")
    volumes = {"/workspace": user_volume}
    workdir = "/workspace"
    if _code_volume:
        volumes["/code"] = _code_volume

    try:
        sb = modal.Sandbox.create(
            app=_app,
            name=sandbox_name,  # Named sandbox for cross-instance lookup
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
            encrypted_ports=[8080, 8081],  # 8080=HTTP/files, 8081=terminal WebSocket
        )
    except modal.exception.AlreadyExistsError:
        # Race condition: another request created the sandbox between our check and create
        print(f"[sandbox_manager] Sandbox already exists (race condition), retrying lookup")
        await asyncio.sleep(1)  # Give it a moment to initialize
        result = _try_get_existing_sandbox()
        if result:
            sb, http_url, terminal_url = result
            _active_sandboxes[user_id] = (sb, http_url, terminal_url)
            return sb, http_url, terminal_url
        else:
            raise RuntimeError(f"Sandbox '{sandbox_name}' exists but cannot be accessed")
    print(f"[sandbox_manager] Sandbox created: {sb.object_id}")

    # Start the sandbox server inside (don't wait for it to complete)
    print(f"[sandbox_manager] Starting sandbox_server.py")
    run_cmd = getattr(sb, "exec")  # Modal Sandbox API method

    # First check if the file exists
    check_process = run_cmd("ls", "-la", "/code/")
    print(f"[sandbox_manager] /code/ contents: {check_process.stdout.read()}")
    check_process = run_cmd("ls", "-la", "/app/")
    print(f"[sandbox_manager] /app/ contents: {check_process.stdout.read()}")

    _ensure_dependency(sb, "claude-agent-sdk", "claude_agent_sdk")
    _ensure_dependency(sb, "websockets", "websockets")

    # Ensure workspace exists
    _run_exec(sb, "bash", "-c", "mkdir -p /workspace")

    # Start the server from the shared code volume or upload on demand
    server_path = _find_sandbox_server(sb)
    if not server_path:
        server_path = _upload_sandbox_server(sb)
    process = run_cmd("python", server_path)
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

    # Get tunnel URLs for HTTP and terminal access
    print(f"[sandbox_manager] Getting tunnels...")
    tunnels = sb.tunnels()
    print(f"[sandbox_manager] Available tunnels: {tunnels}")
    
    http_tunnel = tunnels.get(8080)
    if not http_tunnel:
        raise Exception(f"No tunnel on port 8080. Available: {list(tunnels.keys())}")
    http_url = http_tunnel.url
    print(f"[sandbox_manager] HTTP Tunnel URL: {http_url}")
    
    terminal_tunnel = tunnels.get(8081)
    terminal_url = terminal_tunnel.url if terminal_tunnel else None
    print(f"[sandbox_manager] Terminal Tunnel URL: {terminal_url}")

    # Wait for server to be ready
    await _wait_for_ready(http_url)

    # Cache the sandbox with both URLs
    _active_sandboxes[user_id] = (sb, http_url, terminal_url)

    return sb, http_url, terminal_url


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
    sb, tunnel_url, _ = await get_or_create_sandbox(user_id)

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

    sb, tunnel_url, _ = _active_sandboxes[user_id]

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

    sb, _, _ = _active_sandboxes[user_id]

    try:
        sb.terminate()
    except:
        pass

    del _active_sandboxes[user_id]
    return True
