"""Build the optional standalone Windows executable with PyInstaller 6.22.3."""

import os
from pathlib import Path

import PyInstaller.__main__


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/download-progress"
os.chdir(ROOT)
PyInstaller.__main__.run(
    [
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name",
        "DownloadWorkbench-0.3.0",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build/pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--paths",
        str(PLUGIN / "scripts"),
        "--add-data",
        str(PLUGIN / "assets") + os.pathsep + "assets",
        str(PLUGIN / "scripts/workbench.py"),
    ]
)
