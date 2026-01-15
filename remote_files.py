"""
Remote file manager - reads file system from sprite via commands.
"""

import json
from typing import Optional
from dataclasses import dataclass


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


# Patterns to ignore when listing files
IGNORE_PATTERNS = {
    "__pycache__",
    ".git",
    ".DS_Store",
    "node_modules",
    ".venv",
    "venv",
    ".env",
    ".pytest_cache",
    ".mypy_cache",
    ".cache",
}

IGNORE_EXTENSIONS = {".pyc", ".pyo"}


def should_ignore(name: str) -> bool:
    """Check if a file/directory should be ignored."""
    if name in IGNORE_PATTERNS:
        return True
    for ext in IGNORE_EXTENSIONS:
        if name.endswith(ext):
            return True
    return False


class RemoteFileManager:
    """Manages file operations on a remote sprite."""

    def __init__(self, sprite):
        """
        Initialize with a sprite object.

        Args:
            sprite: A sprites-py Sprite object
        """
        self.sprite = sprite
        self.workspace = self._detect_workspace()

    def _detect_workspace(self) -> str:
        """Detect the workspace directory on the sprite."""
        # Try to get the home directory
        output, code = self._run_cmd("echo $HOME")
        print(f"[remote_files] echo $HOME: output={output!r}, code={code}")
        if code == 0 and output.strip():
            home = output.strip()
            # Verify it exists
            check, _ = self._run_cmd(f'test -d "{home}" && echo "ok"')
            print(f"[remote_files] test -d {home}: {check!r}")
            if "ok" in check:
                return home

        # Fallback to common locations
        for path in ["/root", "/home/sprite", "/home/user", "/"]:
            check, _ = self._run_cmd(f'test -d "{path}" && echo "ok"')
            print(f"[remote_files] test -d {path}: {check!r}")
            if "ok" in check:
                return path

        print("[remote_files] WARNING: no workspace found, using /")
        return "/"

    def _run_cmd(self, cmd: str) -> tuple[str, int]:
        """Run a command on the sprite and return (output, exit_code)."""
        try:
            # Wrap command to always succeed and capture exit code
            wrapped_cmd = f'({cmd}); echo "EXIT_CODE:$?"'
            result = self.sprite.command("bash", "-c", wrapped_cmd)
            output = result.combined_output().decode("utf-8", errors="replace")

            # Parse exit code from output
            if "EXIT_CODE:" in output:
                parts = output.rsplit("EXIT_CODE:", 1)
                actual_output = parts[0]
                try:
                    exit_code = int(parts[1].strip())
                except ValueError:
                    exit_code = 0
                return actual_output, exit_code
            return output, 0
        except Exception as e:
            print(f"[remote_files] command exception: {cmd!r} -> {e}")
            return str(e), 1

    def list_directory(self, relative_path: str = "") -> FileNode:
        """
        List contents of a directory within workspace.
        Returns a FileNode tree structure.
        """
        target_path = f"{self.workspace}/{relative_path}" if relative_path else self.workspace

        # Check if directory exists
        output, code = self._run_cmd(f'test -d "{target_path}" && echo "exists"')
        if "exists" not in output:
            raise FileNotFoundError(f"Directory not found: {relative_path}")

        return self._build_tree(target_path, relative_path or "")

    def _build_tree(self, abs_path: str, relative_base: str) -> FileNode:
        """Recursively build the file tree."""
        # Get directory name
        if relative_base:
            name = relative_base.split("/")[-1] if "/" in relative_base else relative_base
        else:
            name = "workspace"

        rel_path = relative_base or "."

        # List directory contents - portable version using ls
        cmd = f'ls -1F "{abs_path}" 2>/dev/null'
        output, code = self._run_cmd(cmd)

        if code != 0:
            return FileNode(name=name, path=rel_path, type="directory", children=[])

        children = []
        dirs = []
        files = []

        for line in output.strip().split("\n"):
            if not line:
                continue

            # ls -F appends / for directories, * for executables, @ for symlinks
            is_dir = line.endswith("/")
            file_name = line.rstrip("/*@")

            if should_ignore(file_name):
                continue

            if is_dir:
                dirs.append(file_name)
            else:
                files.append(file_name)

        # Sort: directories first, then files, both alphabetically
        for file_name in sorted(dirs):
            child_rel_path = f"{relative_base}/{file_name}" if relative_base else file_name
            child_abs_path = f"{abs_path}/{file_name}"
            child_node = self._build_tree(child_abs_path, child_rel_path)
            children.append(child_node)

        for file_name in sorted(files):
            child_rel_path = f"{relative_base}/{file_name}" if relative_base else file_name
            children.append(FileNode(
                name=file_name,
                path=child_rel_path,
                type="file"
            ))

        return FileNode(name=name, path=rel_path, type="directory", children=children)

    def get_flat_directory(self, relative_path: str = "") -> list[dict]:
        """
        Get a flat list of immediate children in a directory.
        Useful for lazy loading in the UI.
        """
        target_path = f"{self.workspace}/{relative_path}" if relative_path else self.workspace

        # Check if directory exists
        output, code = self._run_cmd(f'test -d "{target_path}" && echo "exists"')
        if "exists" not in output:
            raise FileNotFoundError(f"Directory not found: {relative_path}")

        # List with ls -1F (portable)
        cmd = f'ls -1F "{target_path}" 2>/dev/null'
        output, code = self._run_cmd(cmd)

        items = []
        dirs = []
        files = []

        for line in output.strip().split("\n"):
            if not line:
                continue

            is_dir = line.endswith("/")
            file_name = line.rstrip("/*@")

            if should_ignore(file_name):
                continue

            child_rel_path = f"{relative_path}/{file_name}" if relative_path else file_name

            if is_dir:
                # Check if directory has children
                child_path = f"{target_path}/{file_name}"
                check_output, _ = self._run_cmd(f'ls -1 "{child_path}" 2>/dev/null | head -1')
                has_children = bool(check_output.strip())
                dirs.append({
                    "name": file_name,
                    "path": child_rel_path,
                    "type": "directory",
                    "hasChildren": has_children,
                })
            else:
                files.append({
                    "name": file_name,
                    "path": child_rel_path,
                    "type": "file",
                    "hasChildren": False,
                })

        # Return directories first, then files
        return sorted(dirs, key=lambda x: x["name"]) + sorted(files, key=lambda x: x["name"])

    def read_file_contents(self, relative_path: str, max_size: int = 1024 * 1024) -> dict:
        """
        Read the contents of a file within workspace.
        """
        if not relative_path:
            raise ValueError("File path is required")

        target_path = f"{self.workspace}/{relative_path}"

        # Check if file exists and is not a directory
        output, code = self._run_cmd(f'test -f "{target_path}" && echo "file" || (test -d "{target_path}" && echo "dir")')
        output = output.strip()

        if output == "dir":
            raise IsADirectoryError(f"Cannot read directory: {relative_path}")
        if output != "file":
            raise FileNotFoundError(f"File not found: {relative_path} (check output: {output!r})")

        # Get file size
        size_output, _ = self._run_cmd(f'stat -c%s "{target_path}" 2>/dev/null || stat -f%z "{target_path}"')
        try:
            file_size = int(size_output.strip())
        except ValueError:
            file_size = 0

        # Get file extension
        ext = ""
        if "." in relative_path:
            ext = "." + relative_path.rsplit(".", 1)[-1].lower()

        # Check for binary file
        binary_extensions = {
            '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp',
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.zip', '.tar', '.gz', '.rar', '.7z',
            '.exe', '.dll', '.so', '.dylib',
            '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv',
            '.ttf', '.woff', '.woff2', '.eot',
            '.pyc', '.pyo', '.class',
        }

        if ext in binary_extensions:
            return {
                "path": relative_path,
                "name": relative_path.split("/")[-1],
                "content": None,
                "size": file_size,
                "truncated": False,
                "is_binary": True,
                "extension": ext,
            }

        # Read file contents
        truncated = file_size > max_size
        read_size = min(file_size, max_size)

        content_output, code = self._run_cmd(f'head -c {read_size} "{target_path}"')

        if code != 0:
            # Might be binary
            return {
                "path": relative_path,
                "name": relative_path.split("/")[-1],
                "content": None,
                "size": file_size,
                "truncated": False,
                "is_binary": True,
                "extension": ext,
            }

        return {
            "path": relative_path,
            "name": relative_path.split("/")[-1],
            "content": content_output,
            "size": file_size,
            "truncated": truncated,
            "is_binary": False,
            "extension": ext,
        }
