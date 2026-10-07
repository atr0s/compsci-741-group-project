"""Build the two App Service zip packages from src/.

    package.py OUT_DIR

mcp.zip   the catalog MCP server, rooted so that the startup command
          `python catalog/zava_catalog.py` resolves and the
          server finds customer_sales/ as its sibling.
web.zip   the chat frontend, mirroring src/ (python/web_app/ + shared/static/)
          because web_app.py resolves its static directory relative to itself.

Each carries its own requirements.txt at the root, which is what Oryx installs.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

EXCLUDE_DIRS = {"__pycache__", ".venv", "venv", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".env", ".DS_Store"}


def files_under(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_dir() or any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        yield path


def build(out: Path, entries: list[tuple[Path, str]]) -> int:
    """entries: (source file or directory, path inside the zip)."""
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for source, target in entries:
            if source.is_file():
                zf.write(source, target)
                count += 1
                continue
            for path in files_under(source):
                zf.write(path, f"{target}/{path.relative_to(source).as_posix()}")
                count += 1
    return count


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    mcp_root = SRC / "python" / "mcp_server"
    mcp = build(out_dir / "mcp.zip", [
        (mcp_root / "requirements.txt", "requirements.txt"),
        (mcp_root / "catalog", "catalog"),
        (mcp_root / "customer_sales", "customer_sales"),
    ])

    web_root = SRC / "python" / "web_app"
    web_entries = [(web_root / "requirements.txt", "requirements.txt")]
    web_entries += [(p, f"python/web_app/{p.name}") for p in sorted(web_root.glob("*.py"))]
    web_entries += [(SRC / "shared" / "static", "shared/static")]
    web = build(out_dir / "web.zip", web_entries)

    print(f"mcp.zip: {mcp} files\nweb.zip: {web} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
