---
name: download-progress
description: Download a URL through the Download Progress tools and keep the user informed with current progress, speed, and completion path. Use whenever the user asks Codex to download a file.
---

# Download with progress

Use this workflow when the user asks to download a file from a URL. This plugin only tracks downloads started through its own tools; it cannot observe downloads started by other tools, package managers, or the operating system.

1. Call `start_download` with the URL and, if the user specified one, the destination path. If no destination is specified, save into a `downloads` folder under the current workspace.
2. Report that the download has started, including the destination and download ID.
3. While it is running, call `get_download_progress` about every 2 seconds. Keep the user informed in the conversation with concise updates that include percentage when known, downloaded/total bytes, speed, and ETA when available. For unknown-size responses, report downloaded bytes and speed without inventing a percentage.
4. Stop polling when the task is complete or failed. Report the final path and size, or the failure and whether a partial file can be resumed.
5. Never claim completion until the tool reports `completed`.

The downloader resumes a retained `.part` file when the server supports HTTP range requests. A completed file is never overwritten unless the user explicitly asks for that behavior; the tool currently refuses existing destination files.
