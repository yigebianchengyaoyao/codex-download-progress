---
name: download-progress
description: Download HTTP/HTTPS files or batches to an explicit local folder, show progress in a local workbench, cancel tasks, export receipts, and optionally verify SHA-256 for individual files. Use for downloads and follow-ups on plugin-managed tasks.
---

# Download with progress

Use this workflow when the user asks to download a file from a URL. This plugin only tracks downloads started through its own tools; it cannot observe downloads started by other tools, package managers, or the operating system.

1. Resolve the user's destination to an absolute file path (`destination`) or folder (`directory`). If none is specified, resolve `downloads` under the actual user workspace and pass that absolute directory. The MCP process working directory is the plugin installation folder, not the user's workspace. If workspace location is unavailable, ask for the download folder.
2. Call `start_download` with the URL and exactly one of those path arguments. Pass an expected `sha256` when the user or a trusted release source provides it. A computed checksum alone does not verify the publisher's file.
3. Report that the download has started, including its destination. While running, call `get_download_progress` with `wait_seconds: 2` to avoid busy polling. Post concise conversation updates at useful intervals or meaningful percentage changes; do not repeat unchanged status every two seconds. Show bytes and speed without inventing a percentage or ETA for unknown-size responses. These are tool-driven updates, not a guaranteed autonomous progress bar.
4. When the user asks to stop, call `cancel_download`. Report cancellation as requested until status becomes `cancelled`; a blocked network read can take up to 30 seconds to return. Use `list_downloads` to find a task ID lost from conversation context.
5. Stop polling on `completed`, `failed`, or `cancelled`. Report the final file and any checksum verification, or the failure and retained partial file. Never claim completion before the tool does. Resubmit the same URL and path to recover an interrupted task after a server restart; task IDs themselves are session-local.

Resume requires a matching URL fingerprint, a strong ETag, and a valid range response. Without a strong validator the transfer restarts safely. Existing final files are always refused. Do not delete unknown partial files or metadata to bypass a failure. Up to four transfers may run together; progress reads and task cancellation do not require repeated download approval.

## Workbench and batches

- Call `open_download_workbench` to show the visual queue. Optionally pass an absolute default directory. Return the private local URL as a clickable link for the user; never post it publicly. Progress refreshes directly in the browser, without repeated model calls. Closing its tab does not stop the service.
- For 1-50 file URLs, call `start_download_batch` with `urls`, an absolute `directory`, and optional `title`/`organize`. Duplicate URLs are removed; identical basenames are numbered. Existing final files are skipped without verifying their content.
- Use `get_batch_progress` and `cancel_download_batch` with the returned `batch_id`. A finished batch may include failures, cancellations or skips; report these separately from successful downloads.
- `get_batch_receipt` omits URLs, local paths and the user title, but includes filenames. Ask the user to check before sharing receipts. The UI's PNG share image omits filenames too.
- The UI demo creates actual local text, CSV and ZIP files and always labels its results as a demo. Never imply these are customer results or a network speed benchmark.
- Batch history is session-local, bounded to 20 batches (10 unfinished). A restart clears the queue but retains files. Batch input currently has no per-file expected checksum; use `start_download` when a trusted expected SHA-256 is required.
