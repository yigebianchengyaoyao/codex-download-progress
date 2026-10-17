"""Small bounded batch queue on top of the validated download manager."""

import csv
import io
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from downloader import MAX_ACTIVE, safe_filename


CATEGORIES = {
    "图片": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"},
    "视频": {".mp4", ".mov", ".mkv", ".webm"},
    "音频": {".mp3", ".wav", ".flac", ".m4a"},
    "文档": {".pdf", ".txt", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".md"},
    "模型": {".pt", ".pth", ".onnx", ".gguf", ".safetensors", ".ckpt"},
    "压缩包": {".zip", ".7z", ".rar", ".gz", ".tar"},
}
TERMINAL = {"completed", "failed", "cancelled", "skipped"}


def category(filename):
    extension = Path(filename).suffix.lower()
    return next(
        (name for name, suffixes in CATEGORIES.items() if extension in suffixes), "其他"
    )


class BatchQueue:
    def __init__(self, manager):
        self.manager = manager
        self.batches = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def start(self, urls, directory, title="我的下载清单", organize=True, demo=False):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 50:
            raise ValueError("每批请提供 1～50 个链接")
        if (
            not isinstance(directory, str)
            or not Path(directory).expanduser().is_absolute()
        ):
            raise ValueError("请选择绝对路径，例如 E:\\下载\\我的资料")
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise ValueError("清单名称请填写 1～80 个字")
        if not isinstance(organize, bool) or not isinstance(demo, bool):
            raise ValueError("分类与演示标记必须为布尔值")
        root = Path(directory).expanduser().resolve()
        seen, names, jobs = set(), set(), []
        for value in urls:
            if not isinstance(value, str) or len(value) > 8192:
                raise ValueError("每个链接必须是文本，长度不超过 8192")
            value = value.strip()
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("仅支持不含账号密码的 HTTP/HTTPS 文件直链")
            parsed.port
            if value in seen:
                continue
            seen.add(value)
            original = safe_filename(
                unquote(parsed.path.rsplit("/", 1)[-1]) or "download"
            )
            filename = original
            suffix = 2
            while filename.casefold() in names:
                filename = f"{Path(original).stem} ({suffix}){Path(original).suffix}"
                suffix += 1
            names.add(filename.casefold())
            group = category(filename)
            path = root / group / filename if organize else root / filename
            jobs.append(
                {
                    "url": value,
                    "filename": filename,
                    "category": group,
                    "path": str(path),
                    "status": "queued",
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                    "percent": None,
                    "error": None,
                }
            )
        with self.lock:
            if (
                sum(batch["status"] == "running" for batch in self.batches.values())
                >= 10
            ):
                raise ValueError("最多同时保留 10 个未结束的批次")
            while len(self.batches) >= 20:
                finished = next(
                    (
                        key
                        for key, item in self.batches.items()
                        if item["status"] != "running"
                    ),
                    None,
                )
                if finished is None:
                    raise ValueError("请等待当前清单完成")
                del self.batches[finished]
            batch = {
                "batch_id": uuid.uuid4().hex[:12],
                "title": title.strip(),
                "directory": str(root),
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "status": "running",
                "demo": demo,
                "duplicate_count": len(urls) - len(jobs),
                "jobs": jobs,
                "cancel_requested": False,
            }
            self.batches[batch["batch_id"]] = batch
            return self._snapshot(batch)

    def _snapshot(self, batch):
        jobs = [
            {key: value for key, value in job.items() if key not in ("url", "_task")}
            for job in batch["jobs"]
        ]
        counts = {
            state: sum(job["status"] == state for job in jobs)
            for state in (
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
                "skipped",
            )
        }
        return {key: value for key, value in batch.items() if key != "jobs"} | {
            "jobs": jobs,
            "counts": counts,
            "total": len(jobs),
            "completed_bytes": sum(
                job["downloaded_bytes"] for job in jobs if job["status"] == "completed"
            ),
        }

    def get(self, batch_id):
        with self.lock:
            if not isinstance(batch_id, str) or batch_id not in self.batches:
                raise ValueError("找不到这份清单；批次历史仅保留在本次服务会话")
            return self._snapshot(self.batches[batch_id])

    def list(self):
        with self.lock:
            return [self._snapshot(batch) for batch in self.batches.values()]

    def cancel(self, batch_id):
        with self.lock:
            self.get(batch_id)
            batch = self.batches[batch_id]
            batch["cancel_requested"] = True
            for job in batch["jobs"]:
                if job["status"] == "queued":
                    job["status"] = "cancelled"
                elif job["status"] == "running":
                    self.manager.cancel(job["download_id"])
            self._refresh(batch)
            return self._snapshot(batch)

    def _refresh(self, batch):
        for job in batch["jobs"]:
            if job["status"] == "running":
                snapshot = self.manager.snapshot(job["_task"])
                job.update(
                    {key: value for key, value in snapshot.items() if key != "source"}
                )
        if all(job["status"] in TERMINAL for job in batch["jobs"]):
            batch["status"] = "finished"

    def _loop(self):
        while not self.stop.wait(0.15):
            with self.lock:
                for batch in self.batches.values():
                    self._refresh(batch)
                    if batch["cancel_requested"]:
                        continue
                    for job in batch["jobs"]:
                        if job["status"] != "queued":
                            continue
                        with self.manager.lock:
                            active = sum(
                                task["status"] == "running"
                                for task in self.manager.tasks.values()
                            )
                            if active >= MAX_ACTIVE:
                                break
                            try:
                                result = self.manager.start(
                                    {"url": job["url"], "destination": job["path"]}
                                )
                                job.update(result)
                                # Retain the task object even if session history later evicts its ID.
                                job["_task"] = self.manager.tasks[result["download_id"]]
                            except FileExistsError:
                                job.update(
                                    status="skipped",
                                    error="同名文件已存在，已跳过；未校验已有文件内容",
                                )
                            except Exception as exc:
                                job.update(status="failed", error=str(exc))

    def receipt(self, batch_id):
        batch = self.get(batch_id)
        # Receipts intentionally omit title, directory, URLs and absolute paths.
        return {
            "batch_id": batch["batch_id"],
            "created_at": batch["created_at"],
            "demo": batch["demo"],
            "status": batch["status"],
            "total": batch["total"],
            "counts": batch["counts"],
            "duplicate_count": batch["duplicate_count"],
            "completed_bytes": batch["completed_bytes"],
            "files": [
                {
                    "filename": job["filename"],
                    "category": job["category"],
                    "status": job["status"],
                    "bytes": job["downloaded_bytes"],
                    "sha256": job.get("sha256"),
                }
                for job in batch["jobs"]
            ],
        }

    def csv(self, batch_id):
        receipt = self.receipt(batch_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["文件名", "分类", "状态", "字节", "SHA-256", "演示任务"])
        for item in receipt["files"]:
            # Neutralize spreadsheet formulas in user-controlled filenames.
            name = item["filename"]
            if name.lstrip().startswith(("=", "+", "-", "@")):
                name = "'" + name
            writer.writerow(
                [
                    name,
                    item["category"],
                    item["status"],
                    item["bytes"],
                    item["sha256"] or "",
                    receipt["demo"],
                ]
            )
        return output.getvalue().encode("utf-8-sig")

    def close(self):
        self.stop.set()
        self.worker.join(2)
        with self.lock:
            for batch in self.batches.values():
                if batch["status"] == "running":
                    self.cancel(batch["batch_id"])
