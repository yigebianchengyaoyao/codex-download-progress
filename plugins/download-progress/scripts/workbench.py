"""Standalone launcher, also used as the Windows executable entry point."""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from batches import BatchQueue
from downloader import DownloadManager
from workbench_http import WorkbenchServer


def main():
    parser = argparse.ArgumentParser(description="本机下载工作台")
    parser.add_argument("--directory")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--info-file", help="Optional local startup diagnostics file")
    args = parser.parse_args()
    manager = DownloadManager()
    batches = BatchQueue(manager)
    panel = None
    try:
        panel = WorkbenchServer(
            manager, batches, args.directory, args.port, standalone=True
        )
        info = panel.start()
        if args.info_file:
            Path(args.info_file).write_text(json.dumps(info), encoding="utf-8")
        if sys.stdout:
            print(json.dumps(info), flush=True)
        if not args.no_browser:
            webbrowser.open(panel.url)
        panel.closed.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if panel:
            panel.close()
        batches.close()
        manager.close()


if __name__ == "__main__":
    main()
