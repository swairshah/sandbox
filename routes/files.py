"""
File system routes for listing and managing workspace files.
"""

import os
from fastapi import APIRouter, HTTPException, Query, Header
from typing import Optional

# Check if running on Modal
IS_MODAL = os.environ.get("MODAL_ENVIRONMENT") is not None

if IS_MODAL:
    import httpx
    import sandbox_manager

    class SandboxNotReadyError(Exception):
        """Raised when sandbox doesn't exist yet (user needs to send a message first)."""
        pass

    async def _get_sandbox_file_tree(user_id: str, path: str = "") -> dict:
        """Fetch file tree from user's sandbox. Uses lookup_sandbox (read-only)."""
        result = await sandbox_manager.lookup_sandbox(user_id)
        if result is None:
            raise SandboxNotReadyError("Sandbox not initialized. Please send a message first to start your session.")
        _, http_url, _ = result
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{http_url}/files/list",
                params={"path": path},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise Exception(f"Failed to fetch file tree: {resp.text}")
            data = resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data.get("data", {})

    async def _read_sandbox_file(user_id: str, path: str) -> dict:
        """Read file contents from user's sandbox. Uses lookup_sandbox (read-only)."""
        result = await sandbox_manager.lookup_sandbox(user_id)
        if result is None:
            raise SandboxNotReadyError("Sandbox not initialized. Please send a message first to start your session.")
        _, http_url, _ = result
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{http_url}/files/read",
                params={"path": path},
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise Exception(f"Failed to read file: {resp.text}")
            data = resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data.get("data", {})
else:
    from file_manager import list_directory, get_flat_directory, read_file_contents, WORKSPACE_DIR

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/tree")
async def get_file_tree(
    path: str = Query(default="", description="Relative path within workspace"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Get the full directory tree starting from a path.
    Returns nested structure with all children.
    """
    try:
        if IS_MODAL:
            user_id = x_user_id or "guest"
            tree = await _get_sandbox_file_tree(user_id, path)
            return tree
        else:
            tree = list_directory(path)
            return tree.to_dict()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if IS_MODAL and isinstance(e, SandboxNotReadyError):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_files(
    path: str = Query(default="", description="Relative path within workspace"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Get immediate children of a directory.
    Useful for lazy-loading in the UI.
    """
    try:
        if IS_MODAL:
            user_id = x_user_id or "guest"
            tree = await _get_sandbox_file_tree(user_id, path)
            return {"path": path or ".", "items": tree.get("children", [])}
        else:
            items = get_flat_directory(path)
            return {"path": path or ".", "items": items}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if IS_MODAL and isinstance(e, SandboxNotReadyError):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workspace-path")
async def get_workspace_path():
    """
    Get the absolute path of the workspace directory.
    Useful for display purposes.
    """
    if IS_MODAL:
        return {"path": "/workspace"}
    else:
        return {"path": str(WORKSPACE_DIR)}


@router.get("/read")
async def read_file(
    path: str = Query(..., description="Relative path to file within workspace"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Read the contents of a file.
    Returns file content with syntax highlighting metadata.
    """
    try:
        if IS_MODAL:
            user_id = x_user_id or "guest"
            result = await _read_sandbox_file(user_id, path)
            return result
        else:
            result = read_file_contents(path)
            return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except IsADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if IS_MODAL and isinstance(e, SandboxNotReadyError):
            raise HTTPException(status_code=503, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))
