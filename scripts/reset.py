#!/usr/bin/env python3
"""Remove this project's containers, volumes, and host backup files."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import BACKUP_DIR, banner, compose, log, print_summary, require_docker


def main() -> None:
    require_docker()
    banner("Resetting containers, volumes, and host backup files")
    compose("down", "-v", "--remove-orphans")
    if BACKUP_DIR.exists():
        for child in BACKUP_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Reset complete. Host backup folder: {BACKUP_DIR}")


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
