"""Regression coverage for transfer correctness and MCP boundary handling."""

import hashlib
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/download-progress/scripts"
sys.path.insert(0, str(SCRIPTS))
import downloader
import server

DATA = bytes(range(256)) * 8192
CHANGED = b"new content" * 10000
LARGE = bytes(range(256)) * 131072
REQUESTS = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.path.split("?")[0]
        REQUESTS.append((route, dict(self.headers)))
        body = (
            CHANGED
            if route in ("/changed", "/changed-range")
            else LARGE if route == "/large" else DATA
        )
        if route == "/empty":
            body = b""
        offset = 0
        status = 200
        if self.headers.get("Range") and route in (
            "/range",
            "/bad-range",
            "/416",
            "/changed-range",
        ):
            offset = int(self.headers["Range"].split("=")[1].split("-")[0])
            status = 416 if route == "/416" else 206
        self.send_response(status)
        self.send_header(
            "ETag", '"new"' if route in ("/changed", "/changed-range") else '"version1"'
        )
        if status == 416:
            self.send_header("Content-Range", f"bytes */{len(body)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if status == 206:
            beginning = offset + 1 if route == "/bad-range" else offset
            self.send_header(
                "Content-Range", f"bytes {beginning}-{len(body)-1}/{len(body)}"
            )
        if route != "/unknown":
            self.send_header("Content-Length", str(len(body) - offset))
        self.end_headers()
        if route == "/truncated":
            body = body[:12345]
        try:
            if route == "/slow":
                for start in range(0, len(body), 16384):
                    self.wfile.write(body[start : start + 16384])
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.wfile.write(body[offset:])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.http_thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parent
        )
        self.directory = Path(self.temporary.name)
        self.manager = downloader.DownloadManager()
        REQUESTS.clear()

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def start(self, route="/normal", filename="result.bin", **extra):
        return self.manager.start(
            {
                "url": self.base_url + route,
                "destination": str(self.directory / filename),
                **extra,
            }
        )

    def finish(self, task):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.manager.get(task["download_id"], 0.02)
            if result["status"] != "running":
                self.manager.tasks[task["download_id"]]["worker"].join(1)
                return self.manager.get(task["download_id"])
        self.fail("Task did not reach a terminal state")

    def seed(self, route, body, etag='"version1"'):
        path = self.directory / "result.bin"
        Path(str(path) + ".part").write_bytes(body)
        metadata = {
            "url_hash": hashlib.sha256((self.base_url + route).encode()).hexdigest(),
            "etag": etag,
        }
        Path(str(path) + ".part.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )

    def test_utf8_download_and_expected_checksum(self):
        result = self.finish(
            self.start(filename="中文结果.bin", sha256=hashlib.sha256(DATA).hexdigest())
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["checksum_verified"])
        self.assertEqual(Path(result["path"]).read_bytes(), DATA)

    def test_checksum_mismatch_never_publishes(self):
        result = self.finish(self.start(sha256="0" * 64))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(Path(result["path"]).exists())
        self.assertTrue(Path(result["partial_path"]).exists())

    def test_unknown_length_download(self):
        result = self.finish(self.start("/unknown"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Path(result["path"]).read_bytes(), DATA)

    def test_empty_file_completes_at_100_percent(self):
        result = self.finish(self.start("/empty"))
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["total_bytes"], 0)

    def test_validated_resume_and_actual_received_bytes(self):
        self.seed("/range", DATA[:100000])
        result = self.finish(self.start("/range"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["resumed_bytes"], 100000)
        self.assertEqual(result["received_bytes"], len(DATA) - 100000)
        self.assertEqual(Path(result["path"]).read_bytes(), DATA)
        self.assertEqual(REQUESTS[0][1]["If-Range"], '"version1"')

    def test_changed_file_restarts_instead_of_splicing(self):
        self.seed("/changed", DATA[:100000])
        result = self.finish(self.start("/changed"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["resumed_bytes"], 0)
        self.assertEqual(Path(result["path"]).read_bytes(), CHANGED)

    def test_server_ignoring_if_range_cannot_splice_changed_file(self):
        self.seed("/changed-range", DATA[:100000])
        result = self.finish(self.start("/changed-range"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(Path(result["path"]).exists())

    def test_missing_validator_restarts(self):
        self.seed("/range", DATA[:100000], None)
        result = self.finish(self.start("/range"))
        self.assertEqual(result["resumed_bytes"], 0)
        self.assertNotIn("Range", REQUESTS[0][1])
        self.assertEqual(Path(result["path"]).read_bytes(), DATA)

    def test_wrong_content_range_fails(self):
        self.seed("/bad-range", DATA[:100000])
        result = self.finish(self.start("/bad-range"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(Path(result["path"]).exists())

    def test_416_does_not_claim_completion_from_length_alone(self):
        self.seed("/416", b"x" * len(DATA))
        result = self.finish(self.start("/416"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["received_bytes"], len(DATA))
        self.assertEqual(Path(result["path"]).read_bytes(), DATA)

    def test_truncated_response_is_terminal_failure(self):
        result = self.finish(self.start("/truncated"))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(Path(result["path"]).exists())

    def test_unexpected_exception_is_terminal_failure(self):
        with patch.object(
            downloader, "urlopen", side_effect=RuntimeError("network fixture error")
        ):
            result = self.finish(self.start())
        self.assertEqual(result["status"], "failed")

    def test_file_appearing_at_commit_is_not_overwritten(self):
        original = downloader.publish_file

        def racing_publish(part, destination):
            destination.write_bytes(b"belongs to another program")
            original(part, destination)

        with patch.object(downloader, "publish_file", side_effect=racing_publish):
            result = self.finish(self.start())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            Path(result["path"]).read_bytes(), b"belongs to another program"
        )

    def test_duplicate_destination_rejected(self):
        self.start("/slow")
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.start("/slow")

    def test_separate_manager_cannot_write_same_destination(self):
        first = self.start("/slow")
        deadline = time.monotonic() + 3
        while (
            self.manager.get(first["download_id"])["received_bytes"] == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        other = downloader.DownloadManager()
        try:
            task = other.start(
                {
                    "url": self.base_url + "/slow",
                    "destination": str(self.directory / "result.bin"),
                }
            )
            task_record = other.tasks[task["download_id"]]
            task_record["worker"].join(3)
            self.assertEqual(other.get(task["download_id"])["status"], "failed")
        finally:
            other.close()

    def test_separate_process_holds_destination_lock(self):
        path = self.directory / "result.bin"
        code = (
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "from downloader import destination_lock\n"
            f"with destination_lock(Path({str(path)!r})):\n"
            "    print('locked', flush=True)\n    sys.stdin.readline()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            result = self.finish(self.start())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(path.exists())
        finally:
            process.communicate("\n", timeout=5)

    def test_concurrent_transfer_limit(self):
        for index in range(4):
            self.start("/slow", f"result{index}.bin")
        with self.assertRaisesRegex(ValueError, "At most 4"):
            self.start("/slow", "fifth.bin")

    def test_cancel_retains_partial_and_stops(self):
        task = self.start("/slow")
        deadline = time.monotonic() + 3
        while (
            self.manager.get(task["download_id"])["received_bytes"] == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        requested = self.manager.cancel(task["download_id"])
        self.assertTrue(requested["cancel_requested"])
        result = self.finish(task)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(Path(result["path"]).exists())
        self.assertTrue(Path(result["partial_path"]).exists())

    def test_terminal_speed_does_not_drift(self):
        task = self.start()
        first = self.finish(task)
        time.sleep(0.03)
        second = self.manager.get(task["download_id"])
        self.assertEqual(
            first["speed_bytes_per_second"], second["speed_bytes_per_second"]
        )

    def test_invalid_arguments_and_relative_destination(self):
        for args in (
            {"url": False, "directory": str(self.directory)},
            {"url": self.base_url, "destination": "relative.bin"},
            {"url": self.base_url},
            {"url": "ftp://example.com/a", "directory": str(self.directory)},
            {"url": self.base_url, "directory": str(self.directory), "sha256": "bad"},
        ):
            with self.assertRaises(ValueError):
                self.manager.start(args)

    def test_query_secret_not_returned_or_persisted(self):
        task = self.start("/slow?token=private-value")
        deadline = time.monotonic() + 3
        while (
            self.manager.get(task["download_id"])["received_bytes"] == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        result = self.manager.get(task["download_id"])
        self.assertNotIn("private-value", json.dumps(result))
        metadata = Path(result["partial_path"] + ".json").read_text(encoding="utf-8")
        self.assertNotIn("private-value", metadata)
        self.manager.cancel(task["download_id"])
        self.finish(task)

    def test_large_32_mib_transfer(self):
        result = self.finish(
            self.start("/large", sha256=hashlib.sha256(LARGE).hexdigest())
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["downloaded_bytes"], len(LARGE))
        self.assertEqual(Path(result["path"]).read_bytes(), LARGE)

    def test_actual_download_over_stdio_transport(self):
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        def call(name, args):
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            self.assertNotIn("error", response)
            self.assertFalse(response["result"].get("isError"), response)
            return response["result"]["structuredContent"]

        try:
            task = call(
                "start_download",
                {
                    "url": self.base_url + "/normal",
                    "directory": str(self.directory),
                    "filename": "工具输出.bin",
                },
            )
            for attempt in range(50):
                result = call(
                    "get_download_progress",
                    {"download_id": task["download_id"], "wait_seconds": 0.05},
                )
                if result["status"] != "running":
                    break
            self.assertEqual(result["status"], "completed")
            self.assertEqual(Path(result["path"]).read_bytes(), DATA)
            self.assertEqual(len(call("list_downloads", {})["downloads"]), 1)
            self.assertEqual(
                call("cancel_download", {"download_id": task["download_id"]})["status"],
                "completed",
            )
        finally:
            process.communicate(timeout=5)


class ProtocolTests(unittest.TestCase):
    def test_parse_and_invalid_request_errors(self):
        self.assertEqual(server.process_line("{")["error"]["code"], -32700)
        self.assertEqual(server.process_line("[]")["error"]["code"], -32600)
        self.assertEqual(
            server.process_line(
                json.dumps({"jsonrpc": "2.0", "id": True, "method": "ping"})
            )["error"]["code"],
            -32600,
        )

    def test_version_negotiated_not_reflected(self):
        response = server.process_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "made-up"},
                }
            )
        )
        self.assertEqual(response["result"]["protocolVersion"], server.PROTOCOL_VERSION)

    def test_missing_and_unknown_tool_arguments(self):
        for args in ({}, {"url": "https://example.com", "unknown": True}):
            response = server.process_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "start_download", "arguments": args},
                    }
                )
            )
            self.assertEqual(response["error"]["code"], -32602)

    def test_notification_cannot_start_download(self):
        with patch.object(server.manager, "start") as start:
            response = server.process_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {
                            "name": "start_download",
                            "arguments": {"url": "https://example.com"},
                        },
                    }
                )
            )
            self.assertIsNone(response)
            start.assert_not_called()

    def test_real_stdio_handshake_and_tool_listing(self):
        lines = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "中文客户端"}},
                },
                ensure_ascii=False,
            ),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "server.py")],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.2.0")
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)


if __name__ == "__main__":
    unittest.main()
