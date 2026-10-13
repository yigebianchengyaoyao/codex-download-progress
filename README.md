# Download Progress for Codex

A local Codex plugin for downloading HTTP/HTTPS files with visible progress updates. Start a download, then check its live status in bytes, percentage, speed, ETA, and final path.

## What it does

- Starts a URL download and returns immediately with a download ID.
- Reports progress through Codex tool results so the assistant can post concise updates in the conversation.
- Resumes an existing `.part` file for the same URL when the server supports HTTP Range requests; a small sidecar prevents resuming the wrong transfer.
- Saves into `./downloads/` under the current workspace by default, or to a user-specified file or directory.
- Refuses to overwrite an existing destination.
- Uses only Python's standard library.

The plugin tracks only downloads started through its own `start_download` tool. It cannot observe downloads performed by other tools, package managers, or the operating system. A host must expose the plugin's local MCP tools; public plugin directory submission for an arbitrary local-file downloader may require a hosted MCP service and review.

## Install in Codex

This repository includes a Codex marketplace at `.agents/plugins/marketplace.json`.

1. Add the GitHub marketplace with `codex plugin marketplace add yigebianchengyaoyao/codex-download-progress` (or add the cloned repository path).
2. Install it with `codex plugin add download-progress@download-progress-community`.
3. Start a new Codex conversation if the tools are not yet available.

Repository: <https://github.com/yigebianchengyaoyao/codex-download-progress>

The local MCP server starts with Python 3.10 or newer. If your system uses `py` instead of `python`, change the `command` in `plugins/download-progress/.mcp.json` to `py` and add `-3` before the script path.

## Use it

Ask Codex to download a URL and provide an optional destination. The assistant starts the transfer, checks progress about every two seconds, and reports the final file path when the tool marks it complete.

## Progress fields

`get_download_progress` returns the task status, bytes downloaded and total, percentage when the server reports a size, average speed, ETA when available, destination, and any failure details.

## Project status

See [`project_memory.json`](project_memory.json) for the current requirements, feature list, known limitations, and project status.

## License

MIT. See [`LICENSE`](LICENSE).
