"""Manages per-user Modal sandboxes.

Each user gets their own isolated sandbox with:
- Their own Claude Code instance
- Their own persistent volume at /workspace
- Their own session state

Architecture:
- Modal Dict stores user_id -> sandbox_object_id mapping (persistent across instances)
- Only chat can create sandboxes (via get_or_create_sandbox)
- File explorer and terminal can only lookup existing sandboxes (via lookup_sandbox)
- Uses Sandbox.from_id() which is more reliable than from_name()
"""

import modal
import hashlib
import re
from pathlib import Path
import httpx
import asyncio
import time
import uuid
from typing import Optional

# Reference to the main app - will be set by modal_app.py
_app: Optional[modal.App] = None

# Sandbox image with Claude Code + our sandbox server
_sandbox_image: Optional[modal.Image] = None

# Secrets for Claude API access
_secrets: Optional[list] = None

# Shared code volume (contains sandbox_server.py)
_code_volume: Optional[modal.Volume] = None

# Modal Dict for persistent sandbox ID storage (shared across all container instances)
_sandbox_registry: Optional[modal.Dict] = None

# Local cache: user_id -> (sandbox, http_url, terminal_url, preview_url)
# This is per-container, but Modal Dict is the source of truth
_local_cache: dict[str, tuple[modal.Sandbox, str, str | None, str | None]] = {}

# Registry coordination to avoid duplicate sandboxes per user
_REGISTRY_CREATION_TTL = 120.0  # seconds before a "creating" claim is considered stale
_REGISTRY_WAIT_TIMEOUT = 60.0  # seconds to wait for a concurrent creation to finish
_REGISTRY_POLL_INTERVAL = 1.0  # seconds between registry polls


def _is_registry_ready(entry: object) -> bool:
    if isinstance(entry, str):
        return True
    return isinstance(entry, dict) and entry.get("state") == "ready" and bool(entry.get("sandbox_id"))


def _is_registry_creating(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("state") == "creating"


def _is_registry_stale(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    ts = entry.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - ts) > _REGISTRY_CREATION_TTL


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
    global _app, _sandbox_image, _secrets, _code_volume, _sandbox_registry
    _app = app
    _sandbox_image = sandbox_image
    _secrets = secrets or []
    _code_volume = code_volume
    
    # Initialize the Modal Dict for sandbox registry (persistent across instances)
    _sandbox_registry = modal.Dict.from_name("monios-sandbox-registry", create_if_missing=True)
    print(f"[sandbox_manager] Initialized sandbox registry")


def _ensure_registry() -> modal.Dict:
    """Ensure the sandbox registry is initialized and return it."""
    global _sandbox_registry
    if _sandbox_registry is None:
        _sandbox_registry = modal.Dict.from_name("monios-sandbox-registry", create_if_missing=True)
        print(f"[sandbox_manager] Lazily initialized sandbox registry")
    return _sandbox_registry


def _get_sandbox_from_registry(user_id: str) -> tuple[modal.Sandbox, str, str | None, str | None] | None:
    """
    Try to get sandbox from registry by ID.
    Returns (sandbox, http_url, terminal_url, preview_url) if found and running, None otherwise.
    """
    registry = _ensure_registry()
    
    try:
        entry = registry.get(user_id)
        if not entry:
            print(f"[sandbox_manager] No sandbox ID in registry for {user_id}")
            return None

        if _is_registry_creating(entry):
            if _is_registry_stale(entry):
                try:
                    del registry[user_id]
                except Exception:
                    pass
            return None

        if isinstance(entry, str):
            sandbox_id = entry
        elif _is_registry_ready(entry):
            sandbox_id = entry.get("sandbox_id")
        else:
            return None
        
        print(f"[sandbox_manager] Found sandbox ID in registry: {sandbox_id}")
        sb = modal.Sandbox.from_id(sandbox_id)
        
        # Check if still running
        if sb.poll() is not None:
            print(f"[sandbox_manager] Sandbox {sandbox_id} is no longer running")
            # Clean up stale entry
            try:
                del registry[user_id]
            except Exception:
                pass
            return None
        
        # Get tunnel URLs
        tunnels = sb.tunnels()
        http_tunnel = tunnels.get(8080)
        terminal_tunnel = tunnels.get(8081)
        preview_tunnel = tunnels.get(3000)

        if not http_tunnel:
            print(f"[sandbox_manager] Sandbox found but no HTTP tunnel yet")
            return None

        http_url = http_tunnel.url
        terminal_url = terminal_tunnel.url if terminal_tunnel else None
        preview_url = preview_tunnel.url if preview_tunnel else None
        print(f"[sandbox_manager] Got sandbox from registry: http={http_url}, terminal={terminal_url}, preview={preview_url}")
        return sb, http_url, terminal_url, preview_url
        
    except Exception as e:
        print(f"[sandbox_manager] Error getting sandbox from registry: {e}")
        return None


async def lookup_sandbox(user_id: str) -> tuple[modal.Sandbox, str, str | None, str | None] | None:
    """
    Lookup an existing sandbox for a user. Does NOT create one.
    Used by file explorer and terminal which cannot create sandboxes.

    Returns (sandbox, http_url, terminal_url, preview_url) if found, None if no sandbox exists.
    """
    global _local_cache

    print(f"[sandbox_manager] lookup_sandbox for user: {user_id}")

    # Check local cache first
    if user_id in _local_cache:
        sb, http_url, terminal_url, preview_url = _local_cache[user_id]
        if sb.poll() is None:
            print(f"[sandbox_manager] Reusing cached sandbox for {user_id}")
            return sb, http_url, terminal_url, preview_url
        else:
            print(f"[sandbox_manager] Cached sandbox terminated for {user_id}")
            del _local_cache[user_id]

    # Try to get from registry
    result = _get_sandbox_from_registry(user_id)
    if result:
        _local_cache[user_id] = result
        return result

    return None


async def get_or_create_sandbox(user_id: str) -> tuple[modal.Sandbox, str, str | None, str | None]:
    """
    Get existing sandbox or create new one for user.
    ONLY chat should call this function - it's the only one allowed to create sandboxes.

    Returns (sandbox, http_url, terminal_url, preview_url).
    """
    global _local_cache

    print(f"[sandbox_manager] get_or_create_sandbox for user: {user_id}")

    if _sandbox_image is None:
        raise RuntimeError("sandbox_manager.init must set sandbox_image before creating sandboxes")
    
    # Ensure registry is initialized (lazy init if needed)
    registry = _ensure_registry()

    # First try lookup (checks cache and registry)
    result = await lookup_sandbox(user_id)
    if result:
        return result

    async def _wait_for_registry_ready() -> bool:
        start = time.time()
        while (time.time() - start) < _REGISTRY_WAIT_TIMEOUT:
            entry = registry.get(user_id)
            if _is_registry_ready(entry):
                return True
            if _is_registry_creating(entry) and _is_registry_stale(entry):
                return False
            await asyncio.sleep(_REGISTRY_POLL_INTERVAL)
        return False

    creation_token: str | None = None
    # Attempt to claim sandbox creation, or wait if another worker is creating it
    while True:
        entry = registry.get(user_id)

        if _is_registry_creating(entry) and not _is_registry_stale(entry):
            if await _wait_for_registry_ready():
                result = await lookup_sandbox(user_id)
                if result:
                    return result
            # Creation is stale or failed; continue to claim
        elif _is_registry_ready(entry):
            # Registry shows ready but lookup failed; fall through to recreate
            pass

        creation_token = uuid.uuid4().hex
        registry[user_id] = {
            "state": "creating",
            "token": creation_token,
            "ts": time.time(),
        }

        entry = registry.get(user_id)
        if _is_registry_creating(entry) and entry.get("token") == creation_token:
            break

        # Lost the claim; wait briefly and retry
        await asyncio.sleep(0.2)

    # No existing sandbox, create a new one
    print(f"[sandbox_manager] Creating new sandbox for user: {user_id}")
    
    # Create user's volume (persistent across sandbox restarts)
    user_volume = modal.Volume.from_name(
        _sanitize_volume_name(user_id),
        create_if_missing=True
    )

    # Create new sandbox with secrets for Claude API
    volumes = {"/workspace": user_volume}
    if _code_volume:
        volumes["/code"] = _code_volume

    try:
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
            encrypted_ports=[8080, 8081, 3000],  # 8080=HTTP/files, 8081=terminal, 3000=preview
        )
    except Exception:
        entry = registry.get(user_id)
        if _is_registry_creating(entry) and entry.get("token") == creation_token:
            try:
                del registry[user_id]
            except Exception:
                pass
        raise
    
    # Store sandbox ID in registry immediately
    sandbox_id = sb.object_id
    print(f"[sandbox_manager] Sandbox created: {sandbox_id}")
    registry[user_id] = {
        "state": "ready",
        "sandbox_id": sandbox_id,
        "token": creation_token,
        "ts": time.time(),
    }
    print(f"[sandbox_manager] Stored sandbox ID in registry")

    # If another worker overwrote the claim, terminate and use the winner's sandbox
    entry = registry.get(user_id)
    if isinstance(entry, dict) and entry.get("token") != creation_token:
        try:
            sb.terminate()
        except Exception:
            pass
        result = await lookup_sandbox(user_id)
        if result:
            return result

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
    tunnels = await _wait_for_tunnels(sb)
    print(f"[sandbox_manager] Available tunnels: {tunnels}")
    
    http_tunnel = tunnels.get(8080)
    if not http_tunnel:
        raise Exception(f"No tunnel on port 8080. Available: {list(tunnels.keys())}")
    http_url = http_tunnel.url
    print(f"[sandbox_manager] HTTP Tunnel URL: {http_url}")

    terminal_tunnel = tunnels.get(8081)
    terminal_url = terminal_tunnel.url if terminal_tunnel else None
    print(f"[sandbox_manager] Terminal Tunnel URL: {terminal_url}")

    preview_tunnel = tunnels.get(3000)
    preview_url = preview_tunnel.url if preview_tunnel else None
    print(f"[sandbox_manager] Preview Tunnel URL: {preview_url}")

    # Wait for server to be ready
    await _wait_for_ready(http_url)

    # Cache the sandbox with all URLs
    _local_cache[user_id] = (sb, http_url, terminal_url, preview_url)

    return sb, http_url, terminal_url, preview_url


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


async def _wait_for_tunnels(sb: modal.Sandbox, timeout: float = 30.0) -> dict:
    """Wait for sandbox tunnels to become available."""
    start = time.time()
    while (time.time() - start) < timeout:
        tunnels = sb.tunnels()
        if 8080 in tunnels:
            return tunnels
        await asyncio.sleep(0.5)
    raise TimeoutError("Sandbox tunnels not available in time")


async def send_message(user_id: str, message: str) -> tuple[str, str, list[dict[str, object]]]:
    """Send a message to the user's sandbox and get response."""
    sb, tunnel_url, _, _ = await get_or_create_sandbox(user_id)

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
    if user_id not in _local_cache:
        return False

    sb, tunnel_url, _, _ = _local_cache[user_id]

    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{tunnel_url}/clear", timeout=10.0)
    except:
        pass

    return True


async def terminate_sandbox(user_id: str) -> bool:
    """Terminate a user's sandbox completely."""
    global _local_cache

    if user_id not in _local_cache:
        return False

    sb, _, _, _ = _local_cache[user_id]

    try:
        sb.terminate()
    except:
        pass

    # Clean up local cache
    del _local_cache[user_id]

    # Clean up registry
    if _sandbox_registry is not None:
        try:
            del _sandbox_registry[user_id]
        except Exception:
            pass

    return True


async def get_preview_url(user_id: str) -> str | None:
    """Get the preview URL for a user's sandbox if available."""
    result = await lookup_sandbox(user_id)
    if result is None:
        return None
    _, _, _, preview_url = result
    return preview_url
