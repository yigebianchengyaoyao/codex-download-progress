#!/usr/bin/env python3
"""Small stdlib-only MCP server for resumable downloads and progress polling."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


PROTOCOL_VERSION = "2025-06-18"
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def _safe_filename(name: str) -> str:
    name = Path(name).name.strip().strip(".") or "download"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:240] or "download"


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1024
    return f"{value} B"


def _snapshot(task: dict) -> dict:
    now = time.monotonic()
    elapsed = max(now - task["started_at"], 0.001)
    downloaded = task["downloaded"]
    speed = int(downloaded / elapsed)
    total = task.get("total")
    remaining = max(total - downloaded, 0) if total is not None else None
    eta = int(remaining / speed) if remaining is not None and speed > 0 else None
    return {
        "download_id": task["id"],
        "status": task["status"],
        "url": task["url"],
        "path": str(task["path"]),
        "downloaded_bytes": downloaded,
        "downloaded": _format_bytes(downloaded),
        "total_bytes": total,
        "total": _format_bytes(total) if total is not None else None,
        "percent": round(downloaded * 100 / total, 1) if total else None,
        "speed_bytes_per_second": speed,
        "speed": f"{_format_bytes(speed)}/s",
        "eta_seconds": eta,
        "error": task.get("error"),
    }


def _run_download(task: dict) -> None:
    path: Path = task["path"]
    part = Path(str(path) + ".part")
    metadata = Path(str(part) + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"Destination already exists: {path}")

        offset = 0
        if part.exists():
            if not metadata.is_file():
                raise IOError(f"Partial file has no resume metadata; move or remove it first: {part}")
            saved_url = json.loads(metadata.read_text(encoding="utf-8")).get("url")
            if saved_url != task["url"]:
                raise IOError(f"Partial file belongs to a different URL: {part}")
            offset = part.stat().st_size
        else:
            metadata.write_text(json.dumps({"url": task["url"]}, ensure_ascii=False), encoding="utf-8")
        headers = {"User-Agent": "Codex-Download-Progress/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = Request(task["url"], headers=headers)

        try:
            response = urlopen(request, timeout=30)
        except HTTPError as exc:
            if exc.code == 416 and offset:
                range_header = exc.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes \*/(\d+)", range_header)
                if match and int(match.group(1)) == offset:
                    os.replace(part, path)
                    task["downloaded"] = offset
                    task["total"] = offset
                    task["status"] = "completed"
                    return
            raise

        with response:
            status = getattr(response, "status", response.getcode())
            if offset and status == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                if not match or int(match.group(1)) != offset:
                    raise IOError("Server returned an invalid Content-Range for the partial file")
                else:
                    mode = "ab"
            else:
                offset = 0
                mode = "wb"

            length = response.headers.get("Content-Length")
            total = offset + int(length) if length and length.isdigit() else None
            if total is None:
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes \d+-\d+/(\d+)", content_range)
                total = int(match.group(1)) if match else None

            task["downloaded"] = offset
            task["total"] = total
            with part.open(mode) as output:
                while True:
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    output.write(block)
                    task["downloaded"] += len(block)

        if task.get("total") is not None and task["downloaded"] != task["total"]:
            raise IOError(f"Server ended the response at {task['downloaded']} of {task['total']} bytes")
        if path.exists():
            raise FileExistsError(f"Destination appeared during download: {path}")
        os.replace(part, path)
        metadata.unlink(missing_ok=True)
        task["status"] = "completed"
    except (OSError, URLError, ValueError) as exc:
        task["status"] = "failed"
        task["error"] = str(exc)


def _text_result(value: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
    }


def _start_download(args: dict) -> dict:
    url = str(args.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url must be an absolute HTTP or HTTPS URL")

    destination = args.get("destination")
    if destination:
        requested_path = Path(str(destination)).expanduser()
        if requested_path.exists() and requested_path.is_dir():
            name = _safe_filename(str(args.get("filename") or _filename_from_url(url)))
            path = requested_path / name
        elif str(destination).endswith(("/", "\\")):
            name = _safe_filename(str(args.get("filename") or _filename_from_url(url)))
            path = requested_path / name
        else:
            path = requested_path
    else:
        name = _safe_filename(str(args.get("filename") or _filename_from_url(url)))
        path = Path.cwd() / "downloads" / name
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"Destination already exists: {path}")
    part = Path(str(path) + ".part")
    if part.exists() and not part.is_file():
        raise ValueError(f"Partial download path is not a file: {part}")

    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "url": url,
        "path": path,
        "status": "running",
        "downloaded": 0,
        "total": None,
        "started_at": time.monotonic(),
        "error": None,
    }
    with _tasks_lock:
        _tasks[task_id] = task
    threading.Thread(target=_run_download, args=(task,), daemon=True).start()
    return _snapshot(task)


def _filename_from_url(url: str) -> str:
    return _safe_filename(unquote(Path(urlparse(url).path).name) or "download")


def _get_progress(args: dict) -> dict:
    task_id = str(args.get("download_id", ""))
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        raise ValueError(f"Unknown download_id: {task_id}")
    return _snapshot(task)


TOOLS = [
    {
        "name": "start_download",
        "description": "Start downloading a URL to a local path. Returns immediately with a download ID; call get_download_progress to monitor bytes, percentage, speed, ETA, and completion. Only HTTP and HTTPS URLs are supported. Existing files are not overwritten. Partial .part files are resumed when the server supports Range requests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute HTTP or HTTPS file URL."},
                "destination": {"type": "string", "description": "Optional output file path or existing directory. Defaults to ./downloads/<filename> under the current workspace."},
                "filename": {"type": "string", "description": "Optional filename, used when destination is a directory."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_download_progress",
        "description": "Get the latest status of a download started with start_download. Poll about every two seconds while status is running. Status is running, completed, or failed.",
        "inputSchema": {
            "type": "object",
            "properties": {"download_id": {"type": "string", "description": "ID returned by start_download."}},
            "required": ["download_id"],
            "additionalProperties": False,
        },
    },
]


def _handle(request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        return {"protocolVersion": requested, "capabilities": {"tools": {}}, "serverInfo": {"name": "download-progress", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "start_download":
            return _text_result(_start_download(args))
        if name == "get_download_progress":
            return _text_result(_get_progress(args))
        raise ValueError(f"Unknown tool: {name}")
    raise LookupError(f"Method not found: {method}")


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                continue
            request_id = request.get("id")
            try:
                result = _handle(request)
                if request_id is not None:
                    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
                    print(json.dumps(response, ensure_ascii=False), flush=True)
            except LookupError as exc:
                if request_id is not None:
                    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": str(exc)}}, ensure_ascii=False), flush=True)
            except Exception as exc:
                if request_id is not None:
                    response = {"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, **_text_result({"error": str(exc)})}}
                    print(json.dumps(response, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(f"Invalid MCP input: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
