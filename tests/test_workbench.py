"""Batch execution and authenticated local workbench contracts."""

import csv
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "plugins/download-progress/scripts")
)
from batches import BatchQueue
from demo_files import make_demo_files
from downloader import DownloadManager
from workbench_http import WorkbenchServer
import server
import test_downloads
from test_downloads import DATA


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_downloads.DownloadTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        test_downloads.DownloadTests.tearDownClass()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.directory = Path(self.temporary.name)
        self.manager = DownloadManager()
        self.queue = BatchQueue(self.manager)
        self.panel = WorkbenchServer(self.manager, self.queue, str(self.directory))
        self.panel.start()

    def tearDown(self):
        self.panel.close()
        self.queue.close()
        self.manager.close()
        self.temporary.cleanup()

    def request(self, path, data=None, token=True, headers=None):
        request_headers = {"X-Workbench-Token": self.panel.token} if token else {}
        request_headers.update(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(
            self.panel.origin + path,
            headers=request_headers,
            data=json.dumps(data).encode() if data is not None else None,
        )
        return urlopen(request, timeout=5)

    def finish(self, batch):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.queue.get(batch["batch_id"])
            if result["status"] == "finished":
                return result
            time.sleep(0.03)
        self.fail("Batch failed to finish")

    def test_batch_deduplicates_and_categorizes_actual_files(self):
        base = test_downloads.DownloadTests.base_url
        batch = self.queue.start(
            [base + "/guide.pdf", base + "/guide.pdf", base + "/image.png"],
            str(self.directory),
        )
        result = self.finish(batch)
        self.assertEqual(result["counts"]["completed"], 2)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual((self.directory / "文档/guide.pdf").read_bytes(), DATA)
        self.assertEqual((self.directory / "图片/image.png").read_bytes(), DATA)

    def test_queue_runs_more_than_four_without_exceeding_limit(self):
        batch = self.queue.start(
            [
                test_downloads.DownloadTests.base_url + f"/normal?file={index}"
                for index in range(12)
            ],
            str(self.directory),
            organize=False,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.manager.lock:
                self.assertLessEqual(
                    sum(
                        task["status"] == "running"
                        for task in self.manager.tasks.values()
                    ),
                    4,
                )
            if self.queue.get(batch["batch_id"])["status"] == "finished":
                break
            time.sleep(0.01)
        result = self.finish(batch)
        self.assertEqual(result["counts"]["completed"], 12)
        self.assertEqual(len({job["filename"] for job in result["jobs"]}), 12)

    def test_existing_file_skipped_without_false_verification(self):
        (self.directory / "normal").write_bytes(b"old")
        batch = self.queue.start(
            [test_downloads.DownloadTests.base_url + "/normal"],
            str(self.directory),
            organize=False,
        )
        result = self.finish(batch)
        self.assertEqual(result["counts"]["skipped"], 1)
        self.assertIn("未校验", result["jobs"][0]["error"])
        self.assertEqual((self.directory / "normal").read_bytes(), b"old")

    def test_cancel_queued_and_active(self):
        batch = self.queue.start(
            [
                test_downloads.DownloadTests.base_url + f"/slow?file={index}"
                for index in range(10)
            ],
            str(self.directory),
        )
        time.sleep(0.25)
        self.queue.cancel(batch["batch_id"])
        result = self.finish(batch)
        self.assertEqual(result["counts"]["cancelled"], 10)

    def test_reject_invalid_inputs(self):
        for urls, directory in [
            ([], str(self.directory)),
            (["file:///etc/passwd"], str(self.directory)),
            (["https://user:pass@example.com/x"], str(self.directory)),
            (["https://example.com/a"], "relative"),
            (["https://example.com/a"] * 51, str(self.directory)),
        ]:
            with self.assertRaises(ValueError):
                self.queue.start(urls, directory)

    def test_receipt_omits_private_urls_paths_title_and_escapes_csv(self):
        batch = self.queue.start(
            [test_downloads.DownloadTests.base_url + "/%3Dformula.csv?token=private"],
            str(self.directory),
            "PRIVATE TITLE",
        )
        self.finish(batch)
        receipt = json.dumps(self.queue.receipt(batch["batch_id"]))
        for private in ["token", "PRIVATE TITLE", str(self.directory), "127.0.0.1"]:
            self.assertNotIn(private, receipt)
        rows = list(
            csv.reader(
                io.StringIO(self.queue.csv(batch["batch_id"]).decode("utf-8-sig"))
            )
        )
        self.assertTrue(rows[1][0].startswith("'="))

    def test_local_api_requires_token_and_rejects_cross_origin_host(self):
        for token, headers in [
            (False, {}),
            (True, {"Origin": "https://evil.example"}),
            (True, {"Host": "evil.example"}),
        ]:
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/state", token=token, headers=headers)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        with self.request("/api/state") as response:
            self.assertEqual(json.load(response)["version"], "0.3.0")

    def test_static_assets_and_no_path_traversal(self):
        for route in ["/", "/workbench.js", "/workbench.css"]:
            with self.request(route, token=False) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(
                    "frame-ancestors 'none'",
                    response.headers["Content-Security-Policy"],
                )
        with self.assertRaises(HTTPError) as caught:
            self.request("/../scripts/server.py")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_http_demo_downloads_own_files_and_marks_receipt(self):
        with self.request("/api/demo", {}) as response:
            batch = json.load(response)
        result = self.finish(batch)
        self.assertEqual(result["counts"]["completed"], 3)
        self.assertTrue(self.queue.receipt(batch["batch_id"])["demo"])
        generated = self.panel.demo_files
        for job in result["jobs"]:
            self.assertEqual(Path(job["path"]).read_bytes(), generated[job["filename"]])

    def test_http_batch_cannot_forge_demo_marker(self):
        with self.request(
            "/api/batch",
            {
                "urls": [test_downloads.DownloadTests.base_url + "/normal"],
                "directory": str(self.directory),
                "demo": True,
            },
        ) as response:
            self.assertFalse(json.load(response)["demo"])

    def test_mcp_batch_tools_execute_download_and_return_receipt(self):
        original_manager, original_batches = server.manager, server.batches
        server.manager, server.batches = self.manager, self.queue
        try:

            def call(name, arguments):
                result = server.handle(
                    {
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    }
                )
                self.assertFalse(result.get("isError", False), result)
                return result["structuredContent"]

            batch = call(
                "start_download_batch",
                {
                    "urls": [test_downloads.DownloadTests.base_url + "/normal"],
                    "directory": str(self.directory),
                },
            )
            self.finish(batch)
            self.assertEqual(
                call("get_batch_progress", {"batch_id": batch["batch_id"]})["counts"][
                    "completed"
                ],
                1,
            )
            self.assertEqual(
                call("get_batch_receipt", {"batch_id": batch["batch_id"]})["files"][0][
                    "bytes"
                ],
                len(DATA),
            )
        finally:
            server.manager, server.batches = original_manager, original_batches


if __name__ == "__main__":
    unittest.main()
