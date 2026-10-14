"""Bounded local downloads with validated resume and cooperative cancellation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


MAX_ACTIVE = 4
MAX_HISTORY = 100
SOCKET_TIMEOUT = 30


class Cancelled(Exception):
    pass


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    name = name[:180] or "download"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.I):
        name = "_" + name
    return name


@contextmanager
def destination_lock(path: Path):
    # Keep the lock inode in place: deleting it permits separate processes to
    # acquire different locks for the same destination. OS ownership ends on exit.
    with Path(str(path) + ".part.lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_file(part: Path, destination: Path) -> None:
    # os.replace overwrites a file created after the initial existence check.
    if os.name == "nt":
        os.rename(part, destination)
    else:
        os.link(part, destination)
        try:
            part.unlink()
        except OSError:
            pass  # The published file is valid even if partial-file cleanup fails.


class DownloadManager:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.lock = threading.RLock()

    def _update(self, task: dict, **values) -> None:
        with self.lock:
            task.update(values)

    def snapshot(self, task: dict) -> dict:
        with self.lock:
            copied = dict(task)
        total = copied["total"]
        downloaded = copied["downloaded"]
        end = copied.get("ended_at") or time.monotonic()
        elapsed = max(end - copied.get("transfer_started", end), 0.001)
        speed = int(copied["received"] / elapsed)
        finished = copied["status"] != "running"
        return {
            "download_id": copied["id"],
            "status": copied["status"],
            "phase": copied["phase"],
            "source": copied["source"],
            "path": str(copied["path"]),
            "downloaded_bytes": downloaded,
            "downloaded": format_bytes(downloaded),
            "total_bytes": total,
            "total": format_bytes(total) if total is not None else None,
            "percent": (
                100.0
                if copied["status"] == "completed"
                else round(downloaded * 100 / total, 1) if total else None
            ),
            "resumed_bytes": copied["resumed"],
            "received_bytes": copied["received"],
            "speed_bytes_per_second": speed,
            "speed": f"{format_bytes(speed)}/s",
            "eta_seconds": (
                int(max(total - downloaded, 0) / speed)
                if not finished and total is not None and speed
                else None
            ),
            "cancel_requested": copied["cancel"].is_set(),
            "error": copied.get("error"),
            "sha256": copied.get("sha256"),
            "checksum_verified": copied.get("checksum_verified", False),
            "partial_path": str(copied["path"]) + ".part",
        }

    def start(self, args: dict) -> dict:
        url = args.get("url")
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        url = url.strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Use an absolute HTTP/HTTPS URL without embedded credentials"
            )
        parsed.port  # Validate the port before starting a worker.
        destination, directory = args.get("destination"), args.get("directory")
        if bool(destination) == bool(directory):
            raise ValueError(
                "Supply exactly one absolute destination file path or directory; workspace cannot be inferred"
            )
        selected = destination or directory
        if (
            not isinstance(selected, str)
            or not Path(selected).expanduser().is_absolute()
        ):
            raise ValueError("The destination or directory must be an absolute path")
        if directory:
            filename = args.get(
                "filename", unquote(parsed.path.rsplit("/", 1)[-1]) or "download"
            )
            if not isinstance(filename, str):
                raise ValueError("filename must be a string")
            path = Path(directory).expanduser() / safe_filename(filename)
        else:
            if "filename" in args:
                raise ValueError("filename is only valid with directory")
            path = Path(destination).expanduser()
        path = path.resolve()
        if path.exists():
            raise FileExistsError(f"Destination already exists: {path}")
        expected = args.get("sha256")
        if expected is not None and (
            not isinstance(expected, str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", expected)
        ):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        with self.lock:
            active = [
                task for task in self.tasks.values() if task["status"] == "running"
            ]
            if any(task["path"] == path for task in active):
                raise ValueError("A download already owns this destination")
            if len(active) >= MAX_ACTIVE:
                raise ValueError(f"At most {MAX_ACTIVE} downloads can run at once")
            while len(self.tasks) >= MAX_HISTORY:
                oldest = next(
                    key
                    for key, task in self.tasks.items()
                    if task["status"] != "running"
                )
                del self.tasks[oldest]
            task = {
                "id": uuid.uuid4().hex[:12],
                "url": url,
                "path": path,
                "source": urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "")
                ),
                "expected": expected.lower() if expected else None,
                "status": "running",
                "phase": "connecting",
                "cancel": threading.Event(),
                "downloaded": 0,
                "received": 0,
                "resumed": 0,
                "total": None,
            }
            self.tasks[task["id"]] = task
            worker = threading.Thread(target=self._run, args=(task,), daemon=True)
            task["worker"] = worker
            try:
                worker.start()
            except Exception:
                del self.tasks[task["id"]]
                raise
        return self.snapshot(task)

    def get(self, task_id: str, wait_seconds: float = 0) -> dict:
        if not isinstance(task_id, str):
            raise ValueError("download_id must be a string")
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= 10
        ):
            raise ValueError("wait_seconds must be between 0 and 10")
        with self.lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise ValueError(
                "Unknown download_id; task history is session-local and bounded to 100 records"
            )
        deadline = time.monotonic() + wait_seconds
        while task["status"] == "running" and time.monotonic() < deadline:
            time.sleep(min(0.1, max(deadline - time.monotonic(), 0)))
        return self.snapshot(task)

    def cancel(self, task_id: str) -> dict:
        self.get(task_id)
        with self.lock:
            task = self.tasks[task_id]
            if task["status"] == "running":
                task["cancel"].set()
                task["phase"] = "cancelling"
        return self.snapshot(task)

    def list(self) -> dict:
        with self.lock:
            return {"downloads": [self.snapshot(task) for task in self.tasks.values()]}

    def close(self) -> None:
        with self.lock:
            tasks = list(self.tasks.values())
            for task in tasks:
                if task["status"] == "running":
                    task["cancel"].set()
        deadline = time.monotonic() + SOCKET_TIMEOUT + 2
        for task in tasks:
            task["worker"].join(max(deadline - time.monotonic(), 0))

    @staticmethod
    def _check_cancel(task: dict) -> None:
        if task["cancel"].is_set():
            raise Cancelled("Download cancelled; partial file retained")

    def _run(self, task: dict) -> None:
        try:
            task["path"].parent.mkdir(parents=True, exist_ok=True)
            with destination_lock(task["path"]):
                self._transfer(task)
        except Exception as exc:
            if task["status"] != "completed":
                self._update(
                    task,
                    status="cancelled" if isinstance(exc, Cancelled) else "failed",
                    phase="cancelled" if isinstance(exc, Cancelled) else "failed",
                    error=str(exc),
                )
        finally:
            self._update(task, ended_at=time.monotonic())

    def _transfer(self, task: dict) -> None:
        path = task["path"]
        part = Path(str(path) + ".part")
        metadata = Path(str(part) + ".json")
        if path.exists():
            raise FileExistsError(f"Destination already exists: {path}")
        self._check_cancel(task)
        url_hash = hashlib.sha256(task["url"].encode("utf-8")).hexdigest()
        saved = {}
        offset = 0
        if part.exists():
            saved = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("Invalid partial-file metadata")
            # Read legacy metadata but restart transfers lacking a strong ETag.
            if saved.get("url_hash") != url_hash and saved.get("url") != task["url"]:
                raise ValueError("Partial file belongs to another URL")
            etag = saved.get("etag", "")
            if isinstance(etag, str) and etag.startswith('"') and etag.endswith('"'):
                offset = part.stat().st_size
        headers = {
            "User-Agent": "Download-Progress/0.2.0",
            "Accept-Encoding": "identity",
        }
        if offset:
            headers.update({"Range": f"bytes={offset}-", "If-Range": saved["etag"]})
        try:
            response = urlopen(
                Request(task["url"], headers=headers), timeout=SOCKET_TIMEOUT
            )
        except HTTPError as exc:
            if exc.code != 416 or not offset:
                raise
            # Matching length is not proof of matching content. Fetch afresh.
            exc.close()
            offset = 0
            headers.pop("Range", None)
            headers.pop("If-Range", None)
            response = urlopen(
                Request(task["url"], headers=headers), timeout=SOCKET_TIMEOUT
            )
        with response:
            self._check_cancel(task)
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise ValueError(
                    "Server ignored identity encoding; refusing unsafe range accounting"
                )
            length = response.headers.get("Content-Length")
            if length is not None and not re.fullmatch(r"\d+", length):
                raise ValueError("Invalid Content-Length")
            total = int(length) if length is not None else None
            if response.status == 206:
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+)",
                    response.headers.get("Content-Range", ""),
                )
                if (
                    not offset
                    or not match
                    or int(match[1]) != offset
                    or int(match[2]) != int(match[3]) - 1
                ):
                    raise ValueError(
                        "Invalid Content-Range for the requested remainder"
                    )
                if response.headers.get("ETag") != saved.get("etag"):
                    raise ValueError(
                        "File identity changed during resume; partial file retained"
                    )
                if total is not None and total != int(match[2]) - offset + 1:
                    raise ValueError("Content-Length conflicts with Content-Range")
                total = int(match[3])
            elif response.status == 200:
                offset = 0
            else:
                raise ValueError(f"Unexpected download status: {response.status}")
            digest = hashlib.sha256()
            if offset:
                with part.open("rb") as retained:
                    for block in iter(lambda: retained.read(256 * 1024), b""):
                        self._check_cancel(task)
                        digest.update(block)
            self._update(
                task,
                downloaded=offset,
                resumed=offset,
                total=total,
                transfer_started=time.monotonic(),
                phase="downloading",
            )
            with part.open("ab" if offset else "wb") as output:
                saved = {"url_hash": url_hash, "etag": response.headers.get("ETag")}
                temporary = Path(str(metadata) + ".tmp")
                temporary.write_text(json.dumps(saved), encoding="utf-8")
                os.replace(temporary, metadata)
                while True:
                    self._check_cancel(task)
                    block = response.read1(64 * 1024)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    with self.lock:
                        task["downloaded"] += len(block)
                        task["received"] += len(block)
                    if total is not None and task["downloaded"] > total:
                        raise IOError("Received more bytes than the declared file size")
                output.flush()
                os.fsync(output.fileno())
        self._check_cancel(task)
        if total is not None and task["downloaded"] != total:
            raise IOError(f"Truncated response: {task['downloaded']} of {total} bytes")
        actual = digest.hexdigest()
        self._update(task, phase="verifying", sha256=actual)
        if task["expected"] and actual != task["expected"]:
            raise ValueError("SHA-256 mismatch; final file was not published")
        with self.lock:
            self._check_cancel(task)
            publish_file(part, path)
            task.update(
                status="completed",
                phase="completed",
                total=task["downloaded"],
                checksum_verified=bool(task["expected"]),
            )
        try:
            metadata.unlink(missing_ok=True)
        except OSError:
            pass  # A completed file remains completed even if sidecar cleanup fails.
