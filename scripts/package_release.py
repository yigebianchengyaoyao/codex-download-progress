"""Create source-plugin and convenience-delivery archives without local secrets."""

import hashlib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
DIST.mkdir(exist_ok=True)
PLUGIN = ROOT / "plugins/download-progress"
with zipfile.ZipFile(
    DIST / "download-progress-0.3.0.zip", "w", zipfile.ZIP_DEFLATED
) as archive:
    for path in PLUGIN.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            archive.write(
                path, "download-progress/" + path.relative_to(PLUGIN).as_posix()
            )
    archive.write(ROOT / "LICENSE", "LICENSE")
    archive.write(ROOT / "README.md", "README.md")

executable = DIST / "DownloadWorkbench-0.3.0.exe"
if executable.exists():
    with zipfile.ZipFile(
        DIST / "download-workbench-delivery-0.3.0.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        archive.write(executable, executable.name)
        archive.write(ROOT / "LICENSE", "LICENSE")
        for path in (ROOT / "delivery").glob("*.md"):
            archive.write(path, path.name)

artifacts = sorted(
    path
    for path in DIST.iterdir()
    if "0.3.0" in path.name and path.suffix in (".zip", ".exe")
)
(DIST / "SHA256SUMS-0.3.0.txt").write_text(
    "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in artifacts
    ),
    encoding="utf-8",
    newline="\n",
)
print("\n".join(f"{path.name}: {path.stat().st_size} bytes" for path in artifacts))
