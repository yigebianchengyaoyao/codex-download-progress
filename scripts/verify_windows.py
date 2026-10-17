"""Smoke-check the built executable using its authenticated local HTTP API."""

import json
import os
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
INFO = ROOT / f"build/frozen-info-{os.getpid()}.json"
process = subprocess.Popen(
    [
        str(ROOT / "dist/DownloadWorkbench-0.3.0.exe"),
        "--no-browser",
        "--info-file",
        str(INFO),
    ],
    creationflags=subprocess.CREATE_NO_WINDOW,
)
try:
    deadline = time.monotonic() + 30
    while not INFO.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Executable exited: {process.returncode}")
        time.sleep(0.1)
    info = json.loads(INFO.read_text(encoding="utf-8"))
    parsed = urlsplit(info["url"])
    origin = f"{parsed.scheme}://{parsed.netloc}"
    token = parsed.fragment.removeprefix("token=")

    def request(route, data=None):
        headers = {"X-Workbench-Token": token, "Content-Type": "application/json"}
        request = Request(
            origin + route,
            headers=headers,
            data=json.dumps(data).encode() if data is not None else None,
        )
        return urlopen(request, timeout=5)

    with request("/") as response:
        assert "下载工作台" in response.read().decode("utf-8")
    with request(
        "/api/demo", {"directory": str(ROOT / "build/frozen-downloads")}
    ) as response:
        batch_id = json.load(response)["batch_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with request("/api/state") as response:
            batch = next(
                batch
                for batch in json.load(response)["batches"]
                if batch["batch_id"] == batch_id
            )
        if batch["status"] == "finished":
            break
        time.sleep(0.1)
    assert batch["counts"]["completed"] == 3, batch["counts"]
    for job in batch["jobs"]:
        assert Path(job["path"]).stat().st_size == job["total_bytes"]
        if job["filename"].endswith(".zip"):
            with zipfile.ZipFile(job["path"]) as archive:
                assert archive.testzip() is None
    with request("/api/receipt?batch_id=" + batch_id + "&format=csv") as response:
        assert response.read().startswith(b"\xef\xbb\xbf")
    with request("/api/quit", {}) as response:
        assert json.load(response)["closing"]
    assert process.wait(timeout=10) == 0
    print(
        "Frozen executable: startup, assets, 3 demo files, valid ZIP, CSV, clean exit PASS"
    )
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
