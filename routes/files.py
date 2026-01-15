"""
File system routes for listing and managing workspace files on remote sprites.
"""

from fastapi import APIRouter, HTTPException, Query
from sprite_sessions import get_session_manager
from remote_files import RemoteFileManager

router = APIRouter(prefix="/api/files", tags=["files"])


async def get_file_manager(user_id: str) -> RemoteFileManager:
    """Get a RemoteFileManager for a user's sprite."""
    manager = await get_session_manager()
    session = await manager.get_or_create_session(user_id)
    return RemoteFileManager(session.sprite)


@router.get("/tree")
async def get_file_tree(
    user_id: str = Query(..., description="User ID to get sprite for"),
    path: str = Query(default="", description="Relative path within workspace")
):
    """
    Get the full directory tree starting from a path.
    Returns nested structure with all children.
    """
    try:
        file_manager = await get_file_manager(user_id)
        tree = file_manager.list_directory(path)
        return tree.to_dict()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list directory: {str(e)}")


@router.get("/list")
async def list_files(
    user_id: str = Query(..., description="User ID to get sprite for"),
    path: str = Query(default="", description="Relative path within workspace")
):
    """
    Get immediate children of a directory.
    Useful for lazy-loading in the UI.
    """
    try:
        file_manager = await get_file_manager(user_id)
        items = file_manager.get_flat_directory(path)
        return {"path": path or ".", "items": items}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.get("/workspace-path")
async def get_workspace_path(
    user_id: str = Query(..., description="User ID to get sprite for")
):
    """
    Get the absolute path of the workspace directory.
    Useful for display purposes.
    """
    try:
        file_manager = await get_file_manager(user_id)
        return {"path": file_manager.workspace}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/read")
async def read_file(
    user_id: str = Query(..., description="User ID to get sprite for"),
    path: str = Query(..., description="Relative path to file within workspace")
):
    """
    Read the contents of a file.
    Returns file content with syntax highlighting metadata.
    """
    try:
        file_manager = await get_file_manager(user_id)
        result = file_manager.read_file_contents(path)
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
        raise HTTPException(status_code=500, detail=f"Failed to read file: {str(e)}")
