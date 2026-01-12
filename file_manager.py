"""
File manager module for directory operations and file system watching.
Provides real-time file system updates via WebSocket notifications.
"""

import os
import asyncio
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, asdict
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

# Base workspace directory
WORKSPACE_DIR = Path(__file__).parent / "workspace"


@dataclass
class FileNode:
    """Represents a file or directory in the tree."""
    name: str
    path: str  # Relative path from workspace
    type: str  # "file" or "directory"
    children: Optional[list["FileNode"]] = None

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "path": self.path,
            "type": self.type,
        }
        if self.children is not None:
            result["children"] = [c.to_dict() for c in self.children]
        return result


@dataclass
class FileEvent:
    """Represents a file system change event."""
    event_type: str  # "created", "deleted", "modified", "moved"
    path: str  # Relative path from workspace
    is_directory: bool
    dest_path: Optional[str] = None  # For move events

    def to_dict(self) -> dict:
        return asdict(self)


# Patterns to ignore when listing files
IGNORE_PATTERNS = {
    "__pycache__",
    ".git",
    ".DS_Store",
    "node_modules",
    ".venv",
    "venv",
    ".env",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
}


def should_ignore(name: str) -> bool:
    """Check if a file/directory should be ignored."""
    if name in IGNORE_PATTERNS:
        return True
    for pattern in IGNORE_PATTERNS:
        if pattern.startswith("*") and name.endswith(pattern[1:]):
            return True
    return False


def get_relative_path(absolute_path: Path) -> str:
    """Get the path relative to workspace directory."""
    try:
        return str(absolute_path.relative_to(WORKSPACE_DIR))
    except ValueError:
        return str(absolute_path)


def list_directory(relative_path: str = "") -> FileNode:
    """
    List contents of a directory within workspace.
    Returns a FileNode tree structure.
    """
    target_path = WORKSPACE_DIR / relative_path if relative_path else WORKSPACE_DIR

    if not target_path.exists():
        raise FileNotFoundError(f"Directory not found: {relative_path}")

    if not target_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    return _build_tree(target_path, relative_path or "")


def _build_tree(path: Path, relative_base: str) -> FileNode:
    """Recursively build the file tree."""
    name = path.name or "workspace"
    rel_path = relative_base or "."

    if path.is_file():
        return FileNode(name=name, path=rel_path, type="file")

    children = []
    try:
        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for entry in entries:
            if should_ignore(entry.name):
                continue

            child_rel_path = str(Path(relative_base) / entry.name) if relative_base else entry.name
            children.append(_build_tree(entry, child_rel_path))
    except PermissionError:
        pass

    return FileNode(name=name, path=rel_path, type="directory", children=children)


def read_file_contents(relative_path: str, max_size: int = 1024 * 1024) -> dict:
    """
    Read the contents of a file within workspace.

    Args:
        relative_path: Path relative to workspace directory
        max_size: Maximum file size to read (default 1MB)

    Returns:
        dict with content, size, truncated flag, and file info
    """
    if not relative_path:
        raise ValueError("File path is required")

    target_path = WORKSPACE_DIR / relative_path

    if not target_path.exists():
        raise FileNotFoundError(f"File not found: {relative_path}")

    if target_path.is_dir():
        raise IsADirectoryError(f"Cannot read directory: {relative_path}")

    # Security check: ensure we're still within workspace
    try:
        target_path.resolve().relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        raise PermissionError(f"Access denied: {relative_path}")

    file_size = target_path.stat().st_size
    truncated = file_size > max_size

    # Determine if it's likely a binary file
    binary_extensions = {
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.tar', '.gz', '.rar', '.7z',
        '.exe', '.dll', '.so', '.dylib',
        '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv',
        '.ttf', '.woff', '.woff2', '.eot',
        '.pyc', '.pyo', '.class',
    }

    ext = target_path.suffix.lower()
    is_binary = ext in binary_extensions

    if is_binary:
        return {
            "path": relative_path,
            "name": target_path.name,
            "content": None,
            "size": file_size,
            "truncated": False,
            "is_binary": True,
            "extension": ext,
        }

    try:
        with open(target_path, 'r', encoding='utf-8') as f:
            content = f.read(max_size)
    except UnicodeDecodeError:
        # Probably a binary file
        return {
            "path": relative_path,
            "name": target_path.name,
            "content": None,
            "size": file_size,
            "truncated": False,
            "is_binary": True,
            "extension": ext,
        }

    return {
        "path": relative_path,
        "name": target_path.name,
        "content": content,
        "size": file_size,
        "truncated": truncated,
        "is_binary": False,
        "extension": ext,
    }


def get_flat_directory(relative_path: str = "") -> list[dict]:
    """
    Get a flat list of immediate children in a directory.
    Useful for lazy loading in the UI.
    """
    target_path = WORKSPACE_DIR / relative_path if relative_path else WORKSPACE_DIR

    if not target_path.exists():
        raise FileNotFoundError(f"Directory not found: {relative_path}")

    if not target_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path}")

    items = []
    try:
        entries = sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for entry in entries:
            if should_ignore(entry.name):
                continue

            child_rel_path = str(Path(relative_path) / entry.name) if relative_path else entry.name
            items.append({
                "name": entry.name,
                "path": child_rel_path,
                "type": "directory" if entry.is_dir() else "file",
                "hasChildren": entry.is_dir() and any(entry.iterdir()) if entry.is_dir() else False,
            })
    except PermissionError:
        pass

    return items


class WorkspaceEventHandler(FileSystemEventHandler):
    """Handle file system events and notify via callback."""

    def __init__(self, callback: Callable[[FileEvent], None]):
        super().__init__()
        self.callback = callback

    def _create_event(self, event: FileSystemEvent, event_type: str) -> Optional[FileEvent]:
        """Create a FileEvent from a watchdog event."""
        path = Path(event.src_path)

        # Skip ignored files
        if should_ignore(path.name):
            return None

        # Get relative path
        rel_path = get_relative_path(path)

        return FileEvent(
            event_type=event_type,
            path=rel_path,
            is_directory=event.is_directory,
        )

    def on_created(self, event: FileSystemEvent):
        file_event = self._create_event(event, "created")
        if file_event:
            self.callback(file_event)

    def on_deleted(self, event: FileSystemEvent):
        file_event = self._create_event(event, "deleted")
        if file_event:
            self.callback(file_event)

    def on_modified(self, event: FileSystemEvent):
        # Only notify for file modifications, not directories
        if not event.is_directory:
            file_event = self._create_event(event, "modified")
            if file_event:
                self.callback(file_event)

    def on_moved(self, event: FileSystemEvent):
        path = Path(event.src_path)
        dest_path = Path(event.dest_path)

        # Skip ignored files
        if should_ignore(path.name) or should_ignore(dest_path.name):
            return

        file_event = FileEvent(
            event_type="moved",
            path=get_relative_path(path),
            is_directory=event.is_directory,
            dest_path=get_relative_path(dest_path),
        )
        self.callback(file_event)


class FileWatcher:
    """
    Watches the workspace directory for changes and notifies subscribers.
    Uses asyncio for thread-safe callback execution.
    """

    def __init__(self):
        self.observer: Optional[Observer] = None
        self.callbacks: list[Callable[[FileEvent], None]] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _sync_callback(self, event: FileEvent):
        """Synchronous callback that schedules async notifications."""
        if self._loop and self.callbacks:
            for callback in self.callbacks:
                self._loop.call_soon_threadsafe(callback, event)

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start watching the workspace directory."""
        if self.observer is not None:
            return

        self._loop = loop

        # Ensure workspace directory exists
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

        event_handler = WorkspaceEventHandler(self._sync_callback)
        self.observer = Observer()
        self.observer.schedule(event_handler, str(WORKSPACE_DIR), recursive=True)
        self.observer.start()

    def stop(self):
        """Stop watching."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None

    def subscribe(self, callback: Callable[[FileEvent], None]):
        """Subscribe to file events."""
        self.callbacks.append(callback)

    def unsubscribe(self, callback: Callable[[FileEvent], None]):
        """Unsubscribe from file events."""
        if callback in self.callbacks:
            self.callbacks.remove(callback)


# Global file watcher instance
_file_watcher: Optional[FileWatcher] = None


def get_file_watcher() -> FileWatcher:
    """Get the global file watcher instance."""
    global _file_watcher
    if _file_watcher is None:
        _file_watcher = FileWatcher()
    return _file_watcher
