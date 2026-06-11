import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Annotated

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError
from pydantic import Field


MAX_READ_SIZE = 100 * 1024


def _safe_path(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ToolError(f"Path not found, sir: {path}")
    return p


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} kilobytes"
    elif size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} megabytes"
    else:
        return f"{size / 1024 ** 3:.1f} gigabytes"


def _format_time(t: float) -> str:
    return time.strftime("%d %B %Y at %H:%M", time.localtime(t))


# ─── tool: list_directory ─────────────────────────────────────────────────

@function_tool()
async def list_directory(
    path: Annotated[str, Field(description="Directory path")],
    context: RunContext,
) -> str:
    """List directory contents."""
    p = _safe_path(path)
    if not p.is_dir():
        raise ToolError(f"That path is not a directory, sir.")

    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    folders = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]

    lines = [f"Contents of {p.name}, sir:"]
    if folders:
        lines.append("")
        lines.append("Directories:")
        for f in folders[:30]:
            lines.append(f"  {f.name}/")
    if files:
        lines.append("")
        lines.append("Files:")
        for f in files[:50]:
            size = _format_size(f.stat().st_size)
            lines.append(f"  {f.name}  ({size})")

    total = len(folders) + len(files)
    if total > 80:
        lines.append(f"\n... and {total - 80} more entries.")
    lines.append(f"\n{len(folders)} directories, {len(files)} files total.")
    return "\n".join(lines)


# ─── tool: open_file ──────────────────────────────────────────────────────

@function_tool()
async def open_file(
    path: Annotated[str, Field(description="File path")],
    context: RunContext,
) -> str:
    """Open file with default app."""
    p = _safe_path(path)
    if not p.is_file():
        raise ToolError(f"That path is not a file, sir.")
    os.startfile(str(p))
    return f"Opening {p.name}, sir."


# ─── tool: read_file ──────────────────────────────────────────────────────

@function_tool()
async def read_file(
    path: Annotated[str, Field(description="File path")],
    context: RunContext,
) -> str:
    """Read text file contents."""
    p = _safe_path(path)
    if not p.is_file():
        raise ToolError(f"That path is not a file, sir.")

    size = p.stat().st_size
    if size > MAX_READ_SIZE:
        raise ToolError(
            f"The file is {_format_size(size)}, which exceeds my read limit "
            f"of {_format_size(MAX_READ_SIZE)}, sir."
        )

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="latin-1")
        except Exception:
            raise ToolError("Unable to read that file as text, sir. It may be a binary file.")

    if not text.strip():
        return f"The file {p.name} is empty, sir."

    lines = text.splitlines()
    if len(lines) > 200:
        text = "\n".join(lines[:200]) + f"\n\n... truncated ({len(lines) - 200} more lines)"
    return f"Contents of {p.name}, sir:\n\n{text}"


# ─── tool: search_files ──────────────────────────────────────────────────

@function_tool()
async def search_files(
    query: Annotated[str, Field(description="Search query")],
    context: RunContext,
    directory: Annotated[str | None, Field(description="Search directory (default: home)")] = None,
) -> str:
    """Search files by name."""
    root = Path(directory).expanduser().resolve() if directory else Path.home()
    if not root.is_dir():
        raise ToolError(f"Directory not found, sir: {root}")

    results = []
    q = query.lower()
    start = time.time()
    try:
        for entry in root.rglob("*"):
            if time.time() - start > 10:
                break
            if entry.is_file() and q in entry.name.lower():
                try:
                    rel = entry.relative_to(root)
                except ValueError:
                    rel = entry.name
                results.append(str(rel))
                if len(results) >= 30:
                    break
    except PermissionError:
        pass

    if not results:
        return f"No files matching '{query}' found in {root.name}, sir."

    lines = [f"Found {len(results)} result{'s' if len(results) > 1 else ''} for '{query}' in {root.name}:"]
    for r in results[:30]:
        lines.append(f"  {r}")
    return "\n".join(lines)


# ─── tool: file_info ──────────────────────────────────────────────────────

@function_tool()
async def file_info(
    path: Annotated[str, Field(description="File path or directory")],
    context: RunContext,
) -> str:
    """File/directory metadata."""
    p = _safe_path(path)
    stat = p.stat()
    kind = "directory" if p.is_dir() else "file"
    size = _format_size(stat.st_size) if p.is_file() else ""
    modified = _format_time(stat.st_mtime)
    created = _format_time(stat.st_ctime)

    parts = [f"{p.name} is a {kind}, sir."]
    if size:
        parts.append(f"Size: {size}.")
    parts.append(f"Last modified: {modified}.")
    parts.append(f"Created: {created}.")
    return " ".join(parts)


# ─── tool: create_file ────────────────────────────────────────────────────

@function_tool()
async def create_file(
    path: Annotated[str, Field(description="File path to create")],
    context: RunContext,
) -> str:
    """Create empty file."""
    p = Path(path).expanduser().resolve()
    if p.exists():
        raise ToolError(f"Already exists, sir: {p.name}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return f"Created {p.name}, sir."


# ─── tool: create_folder ──────────────────────────────────────────────────

@function_tool()
async def create_folder(
    path: Annotated[str, Field(description="Folder path to create")],
    context: RunContext,
) -> str:
    """Create folder."""
    p = Path(path).expanduser().resolve()
    if p.exists():
        raise ToolError(f"Already exists, sir: {p.name}")
    p.mkdir(parents=True)
    return f"Created folder {p.name}, sir."


# ─── tool: copy_item ─────────────────────────────────────────────────────

@function_tool()
async def copy_item(
    source: Annotated[str, Field(description="Source path")],
    destination: Annotated[str, Field(description="Destination path")],
    context: RunContext,
) -> str:
    """Copy file or folder."""
    src = _safe_path(source)
    dst = Path(destination).expanduser().resolve()

    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return f"Copied folder {src.name} to {dst.parent.name}, sir."
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return f"Copied {src.name} to {dst.parent.name}, sir."


# ─── tool: move_item ─────────────────────────────────────────────────────

@function_tool()
async def move_item(
    source: Annotated[str, Field(description="Source path")],
    destination: Annotated[str, Field(description="Destination path")],
    context: RunContext,
) -> str:
    """Move/rename file or folder."""
    src = _safe_path(source)
    dst = Path(destination).expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src.name} to {dst.parent.name}, sir."


# ─── tool: resolve_folder ─────────────────────────────────────────────────

@function_tool()
async def resolve_folder(
    name: Annotated[str, Field(
        description="Folder name (downloads, documents, etc)",
    )],
    context: RunContext,
) -> str:
    """Resolve folder name to path."""
    home = Path.home()
    known = {
        "home": home,
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "pictures": home / "Pictures",
        "photos": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
        "root": Path("C:\\"),
        "c drive": Path("C:\\"),
    }
    key = name.strip().lower().replace("'", "")
    p = known.get(key)
    if p is None:
        raise ToolError(f"I don't know where '{name}' is, sir. Try: downloads, documents, desktop, pictures, music, videos, home, or root.")
    if not p.exists():
        raise ToolError(f"The {name} folder does not exist at {p}, sir.")
    return str(p)


# ─── tool: find_recent ────────────────────────────────────────────────────

@function_tool()
async def find_recent(
    context: RunContext,
    directory: Annotated[str, Field(description="Directory to search")],
    pattern: Annotated[str | None, Field(description="Filename filter")] = None,
    count: Annotated[int, Field(description="File count", ge=1, le=20)] = 5,
) -> str:
    """Recent files in directory."""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"Directory not found, sir: {root}")

    try:
        candidates = []
        for entry in root.iterdir():
            if entry.is_file():
                if pattern is None or pattern.lower() in entry.name.lower():
                    candidates.append(entry)
    except PermissionError:
        raise ToolError(f"Permission denied reading {root.name}, sir.")

    candidates.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    top = candidates[:count]

    if not top:
        msg = f"No files found in {root.name}"
        if pattern:
            msg += f" matching '{pattern}'"
        return msg + ", sir."

    lines = [f"{'Most recent' if count == 1 else f'Top {len(top)} recent'} file{'s' if len(top) > 1 else ''} in {root.name}, sir:"] if len(top) > 1 else [f"Most recent file in {root.name}, sir:"]
    for f in top:
        mtime = _format_time(f.stat().st_mtime)
        size = _format_size(f.stat().st_size)
        lines.append(f"  {f.name}  ({size})  — modified {mtime}")
    return "\n".join(lines)


# ─── tool: delete_item ───────────────────────────────────────────────────

@function_tool()
async def delete_item(
    path: Annotated[str, Field(description="Path to delete")],
    context: RunContext,
) -> str:
    """Delete file or folder."""
    p = _safe_path(path)

    system_protected = [
        Path(os.environ.get("SystemRoot", "C:\\Windows")).resolve(),
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")).resolve(),
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")).resolve(),
        Path(os.environ.get("SystemDrive", "C:\\")).resolve(),
        Path.home().resolve(),
    ]
    for protected in system_protected:
        if p == protected or protected in p.parents:
            raise ToolError(f"I cannot delete files in {protected.name}, sir. That could damage the system.")

    if p.is_dir():
        shutil.rmtree(p)
        return f"Folder {p.name} has been permanently removed, sir."
    else:
        os.remove(p)
        return f"File {p.name} has been deleted, sir."


FILE_TOOLS = [
    list_directory,
    open_file,
    read_file,
    search_files,
    file_info,
    resolve_folder,
    find_recent,
    create_file,
    create_folder,
    copy_item,
    move_item,
    delete_item,
]
