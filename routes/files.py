"""
File system routes for listing and managing workspace files.
"""

from fastapi import APIRouter, HTTPException, Query
from file_manager import list_directory, get_flat_directory, WORKSPACE_DIR

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/tree")
async def get_file_tree(path: str = Query(default="", description="Relative path within workspace")):
    """
    Get the full directory tree starting from a path.
    Returns nested structure with all children.
    """
    try:
        tree = list_directory(path)
        return tree.to_dict()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/list")
async def list_files(path: str = Query(default="", description="Relative path within workspace")):
    """
    Get immediate children of a directory.
    Useful for lazy-loading in the UI.
    """
    try:
        items = get_flat_directory(path)
        return {"path": path or ".", "items": items}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotADirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspace-path")
async def get_workspace_path():
    """
    Get the absolute path of the workspace directory.
    Useful for display purposes.
    """
    return {"path": str(WORKSPACE_DIR)}
