#!/usr/bin/env python3
"""UTF-8 JSON-RPC/MCP transport for the local download manager."""

from __future__ import annotations

import json
import sys

from downloader import DownloadManager
from batches import BatchQueue
from workbench_http import WorkbenchServer


PROTOCOL_VERSION = "2025-06-18"
VERSION = "0.3.0"
manager = DownloadManager()
batches = BatchQueue(manager)
panel = None


def tool(name, description, properties, required, read_only=False):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "additionalProperties": True},
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": False,
            "idempotentHint": read_only,
            "openWorldHint": name in ("start_download", "start_download_batch"),
        },
    }


TASK_ID = {
    "download_id": {"type": "string", "description": "ID returned by start_download."}
}
TOOLS = [
    tool(
        "start_download",
        "Start an HTTP/HTTPS download. Supply an absolute destination file or directory; "
        "the MCP process cannot infer the user's workspace. Strong ETags validate resume. "
        "At most four concurrent transfers. Existing final files are never overwritten.",
        {
            "url": {"type": "string"},
            "destination": {
                "type": "string",
                "description": "Absolute output file path.",
            },
            "directory": {
                "type": "string",
                "description": "Absolute output directory.",
            },
            "filename": {
                "type": "string",
                "description": "Optional filename with directory.",
            },
            "sha256": {
                "type": "string",
                "pattern": "^[a-fA-F0-9]{64}$",
                "description": "Optional expected SHA-256; mismatch prevents publication.",
            },
        },
        ["url"],
    ),
    tool(
        "get_download_progress",
        "Get progress. Optionally wait up to ten seconds while running.",
        {**TASK_ID, "wait_seconds": {"type": "number", "minimum": 0, "maximum": 10}},
        ["download_id"],
        True,
    ),
    tool(
        "cancel_download",
        "Request cancellation, retaining the partial file. Cancellation is "
        "confirmed when status becomes cancelled; a blocked read can take up to 30 seconds.",
        TASK_ID,
        ["download_id"],
    ),
    tool(
        "list_downloads",
        "List up to 100 tasks from this MCP session. IDs do not survive restart.",
        {},
        [],
        True,
    ),
]

BATCH_ID = {"batch_id": {"type": "string"}}
TOOLS.extend(
    [
        tool(
            "start_download_batch",
            "Queue 1-50 HTTP/HTTPS file URLs with deduplication and optional category folders. Existing files are skipped without content verification.",
            {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
                "directory": {
                    "type": "string",
                    "description": "Absolute output directory.",
                },
                "title": {"type": "string"},
                "organize": {"type": "boolean"},
            },
            ["urls", "directory"],
        ),
        tool(
            "get_batch_progress",
            "Get batch progress and separate success, failure, skipped and cancelled counts.",
            BATCH_ID,
            ["batch_id"],
            True,
        ),
        tool(
            "cancel_download_batch",
            "Cancel queued and running jobs in a batch, retaining partial files.",
            BATCH_ID,
            ["batch_id"],
        ),
        tool(
            "get_batch_receipt",
            "Get a receipt without URLs or absolute paths. Filenames remain private data.",
            BATCH_ID,
            ["batch_id"],
            True,
        ),
        tool(
            "open_download_workbench",
            "Start an authenticated local visual workbench. Return its private URL for the user to open; do not share this URL publicly.",
            {
                "directory": {
                    "type": "string",
                    "description": "Optional absolute default download folder.",
                }
            },
            [],
        ),
    ]
)


def text_result(value):
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
    }


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def handle(request):
    global panel
    method = request["method"]
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError(-32602, "params must be an object")
    if method == "initialize":
        # Negotiate a version actually implemented, rather than echo any client value.
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "download-progress", "version": VERSION},
        }
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method != "tools/call":
        raise ProtocolError(-32601, f"Method not found: {method}")
    name = params.get("name")
    descriptor = next((item for item in TOOLS if item["name"] == name), None)
    if descriptor is None:
        raise ProtocolError(-32602, "Unknown tool")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        raise ProtocolError(-32602, "arguments must be an object")
    schema = descriptor["inputSchema"]
    if set(args) - set(schema["properties"]) or set(schema["required"]) - set(args):
        raise ProtocolError(
            -32602, "Missing required arguments or unknown argument names"
        )
    try:
        if name == "start_download":
            value = manager.start(args)
        elif name == "get_download_progress":
            value = manager.get(args["download_id"], args.get("wait_seconds", 0))
        elif name == "cancel_download":
            value = manager.cancel(args["download_id"])
        elif name == "list_downloads":
            value = manager.list()
        elif name == "start_download_batch":
            value = batches.start(**args)
        elif name == "get_batch_progress":
            value = batches.get(args["batch_id"])
        elif name == "cancel_download_batch":
            value = batches.cancel(args["batch_id"])
        elif name == "get_batch_receipt":
            value = batches.receipt(args["batch_id"])
        else:
            if panel is None or panel.closed.is_set():
                panel = WorkbenchServer(manager, batches, args.get("directory"))
                value = panel.start()
            else:
                value = {
                    "url": panel.url,
                    "directory": panel.directory,
                    "version": VERSION,
                }
        return text_result(value)
    except Exception as exc:
        return {"isError": True, **text_result({"error": str(exc)})}


def process_line(line):
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Invalid JSON"},
        }
    valid = (
        isinstance(request, dict)
        and request.get("jsonrpc") == "2.0"
        and isinstance(request.get("method"), str)
        and (
            "id" not in request
            or request["id"] is None
            or (
                isinstance(request["id"], (str, int))
                and not isinstance(request["id"], bool)
            )
        )
    )
    if not valid:
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid request"},
        }
    if "id" not in request:
        # Notification-shaped tool calls must never start an unobservable download.
        if request["method"].startswith("notifications/"):
            handle(request)
        return None
    try:
        result = handle(request)
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}
    except ProtocolError as exc:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": exc.code, "message": exc.message},
        }


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        for line in sys.stdin:
            try:
                response = process_line(line)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(
                    f"MCP transport error: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        if panel is not None:
            panel.close()
        batches.close()
        manager.close()


if __name__ == "__main__":
    main()
