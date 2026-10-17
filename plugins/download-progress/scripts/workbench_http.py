"""Authenticated loopback UI. No cloud service, account, or external assets."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from demo_files import make_demo_files


VERSION = "0.3.0"
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets"


def default_directory():
    root = (
        Path("E:/下载")
        if os.name == "nt" and Path("E:/").exists()
        else Path.home() / "Downloads"
    )
    return str(root / "下载工作台")


class WorkbenchServer:
    def __init__(self, manager, batches, directory=None, port=0, standalone=False):
        if directory is not None and (
            not isinstance(directory, str) or not Path(directory).is_absolute()
        ):
            raise ValueError("工作目录必须是绝对路径")
        self.manager, self.batches = manager, batches
        self.directory = directory or default_directory()
        self.standalone = standalone
        self.token = secrets.token_urlsafe(32)
        self.demo_key = secrets.token_urlsafe(24)
        self.demo_files = None
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Do not put bearer tokens, URLs or local paths in server logs.

            def send(
                self,
                code,
                payload,
                content_type="application/json; charset=utf-8",
                filename=None,
            ):
                body = (
                    json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    if isinstance(payload, (dict, list))
                    else payload
                )
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                )
                if filename:
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{filename}"'
                    )
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def allowed(self, api=False):
                if self.headers.get("Host") != f"127.0.0.1:{owner.port}":
                    self.send(403, {"error": "拒绝非回环主机请求"})
                    return False
                origin = self.headers.get("Origin")
                if origin and origin != owner.origin:
                    self.send(403, {"error": "拒绝跨站请求"})
                    return False
                if api and not secrets.compare_digest(
                    self.headers.get("X-Workbench-Token", ""), owner.token
                ):
                    self.send(403, {"error": "连接凭据失效，请从程序重新打开工作台"})
                    return False
                return True

            def do_GET(self):
                if not self.allowed():
                    return
                parsed = urlsplit(self.path)
                static = {
                    "/": ("workbench.html", "text/html; charset=utf-8"),
                    "/workbench.css": ("workbench.css", "text/css; charset=utf-8"),
                    "/workbench.js": (
                        "workbench.js",
                        "application/javascript; charset=utf-8",
                    ),
                }
                if parsed.path in static:
                    name, mime = static[parsed.path]
                    self.send(200, (ASSETS / name).read_bytes(), mime)
                    return
                if parsed.path.startswith(f"/demo/{owner.demo_key}/"):
                    self.serve_demo(unquote(parsed.path.rsplit("/", 1)[-1]))
                    return
                if not self.allowed(api=True):
                    return
                try:
                    if parsed.path == "/api/state":
                        self.send(
                            200,
                            {
                                "version": VERSION,
                                "directory": owner.directory,
                                "standalone": owner.standalone,
                                "batches": owner.batches.list(),
                                "downloads": owner.manager.list()["downloads"],
                            },
                        )
                    elif parsed.path == "/api/receipt":
                        params = parse_qs(parsed.query)
                        batch_id = params.get("batch_id", [""])[0]
                        if params.get("format", ["json"])[0] == "csv":
                            self.send(
                                200,
                                owner.batches.csv(batch_id),
                                "text/csv; charset=utf-8",
                                f"receipt-{batch_id}.csv",
                            )
                        else:
                            self.send(200, owner.batches.receipt(batch_id))
                    else:
                        self.send(404, {"error": "页面不存在"})
                except (ValueError, OSError) as exc:
                    self.send(400, {"error": str(exc)})

            def serve_demo(self, filename):
                if owner.demo_files is None:
                    self.send(404, {"error": "请先创建演示任务"})
                    return
                body = owner.demo_files.get(filename)
                if body is None:
                    self.send(404, {"error": "演示文件不存在"})
                    return
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                offset = 0
                requested = self.headers.get("Range", "")
                if (
                    requested.startswith("bytes=")
                    and self.headers.get("If-Range") == etag
                ):
                    try:
                        offset = int(requested[6:].split("-")[0])
                    except ValueError:
                        offset = 0
                if not 0 <= offset < len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206 if offset else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body) - offset))
                self.send_header("ETag", etag)
                if offset:
                    self.send_header(
                        "Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}"
                    )
                self.end_headers()
                try:
                    for start in range(offset, len(body), 32768):
                        self.wfile.write(body[start : start + 32768])
                        self.wfile.flush()
                        time.sleep(0.08)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_POST(self):
                if not self.allowed(api=True):
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 262144 or not self.headers.get(
                        "Content-Type", ""
                    ).startswith("application/json"):
                        raise ValueError("请提交不超过 256 KiB 的 JSON 请求")
                    args = json.loads(self.rfile.read(length))
                    if not isinstance(args, dict):
                        raise ValueError("请求内容必须是对象")
                    route = urlsplit(self.path).path
                    if route == "/api/batch":
                        result = owner.batches.start(
                            args.get("urls"),
                            args.get("directory"),
                            args.get("title", "我的下载清单"),
                            args.get("organize", True),
                        )
                    elif route == "/api/demo":
                        directory = args.get("directory", owner.directory)
                        if (
                            not isinstance(directory, str)
                            or not Path(directory).is_absolute()
                        ):
                            raise ValueError("演示目录必须是绝对路径")
                        owner.demo_files = make_demo_files()
                        folder = Path(directory) / (
                            "演示-"
                            + datetime.now().strftime("%Y%m%d-%H%M%S")
                            + "-"
                            + secrets.token_hex(2)
                        )
                        urls = [
                            f"{owner.origin}/demo/{owner.demo_key}/{quote(name)}"
                            for name in owner.demo_files
                        ]
                        result = owner.batches.start(
                            urls, str(folder), "本机演示 · 自生成文件", True, True
                        )
                    elif route == "/api/cancel-batch":
                        result = owner.batches.cancel(args.get("batch_id"))
                    elif route == "/api/cancel-task":
                        result = owner.manager.cancel(args.get("download_id"))
                    elif route == "/api/open-folder":
                        path = Path(
                            owner.batches.get(args.get("batch_id"))["directory"]
                        )
                        if not path.is_dir():
                            raise ValueError("文件夹尚未建立，请等待下载开始")
                        if os.name == "nt":
                            os.startfile(str(path))
                        else:
                            subprocess.Popen(
                                [
                                    "open" if sys.platform == "darwin" else "xdg-open",
                                    str(path),
                                ]
                            )
                        result = {"opened": True}
                    elif route == "/api/quit":
                        result = {"closing": True}
                        self.send(200, result)
                        threading.Thread(target=owner.close, daemon=True).start()
                        return
                    else:
                        self.send(404, {"error": "操作不存在"})
                        return
                    self.send(200, result)
                except (ValueError, TypeError, OSError) as exc:
                    self.send(400, {"error": str(exc)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.url = self.origin + "/#token=" + self.token
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return {"url": self.url, "directory": self.directory, "version": VERSION}

    def close(self):
        with self.close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)
