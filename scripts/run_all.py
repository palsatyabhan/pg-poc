#!/usr/bin/env python3
"""Run the full backup, then three incremental backup and restore rounds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from add_more_data import add_source_rows
from common import BACKUP_DIR, banner, compose, log, print_summary, require_docker
from full_backup import main as full_backup
from incremental_backup import take_incremental
from reset import main as reset
from restore_full import main as restore_full
from restore_incremental import restore_and_check
from start_and_seed import main as start_and_seed

# Taking a backup still chains manifests: full, then incremental, then incremental-2.
# Restore keeps a synthetic full so the next combine does not open the older directories.
INCREMENTALS = (
    {
        "name": "incremental",
        "parent": "full",
        "events": (
            (3, "incr1", "written for incremental 1"),
            (4, "incr1", "also written for incremental 1"),
        ),
        "source_ids": (1, 2, 3, 4),
        "target_ids_before": (1, 2),
        "combine": ["full", "incremental"],
        "synthetic": "synthetic-1",
    },
    {
        "name": "incremental-2",
        "parent": "incremental",
        "events": (
            (5, "incr2", "written for incremental 2"),
            (6, "incr2", "also written for incremental 2"),
        ),
        "source_ids": (1, 2, 3, 4, 5, 6),
        "target_ids_before": (1, 2, 3, 4),
        "combine": ["synthetic-1", "incremental-2"],
        "synthetic": "synthetic-2",
    },
    {
        "name": "incremental-3",
        "parent": "incremental-2",
        "events": (
            (7, "incr3", "written for incremental 3"),
            (8, "incr3", "also written for incremental 3"),
        ),
        "source_ids": (1, 2, 3, 4, 5, 6, 7, 8),
        "target_ids_before": (1, 2, 3, 4, 5, 6),
        "combine": ["synthetic-2", "incremental-3"],
        "synthetic": "synthetic-3",
    },
)


def main() -> None:
    require_docker()
    log("The log lines below follow each action. A verification summary is printed at the end.")
    completed = False
    try:
        reset()
        start_and_seed()
        full_backup()
        restore_full()
        total = len(INCREMENTALS)
        for number, spec in enumerate(INCREMENTALS, start=1):
            add_source_rows(
                spec["events"],
                iteration=number,
                total=total,
                source_ids=spec["source_ids"],
                target_ids=spec["target_ids_before"],
            )
            take_incremental(spec["name"], spec["parent"], iteration=number, total=total)
            restore_and_check(
                spec["combine"],
                spec["synthetic"],
                spec["source_ids"],
                iteration=number,
                total=total,
            )
        completed = True
    finally:
        print_summary()
        if completed:
            banner("POC finished")
            log("pg-source   postgresql://postgres:postgres@127.0.0.1:5432/appdb")
            log("pg-target   postgresql://postgres:postgres@127.0.0.1:5433/appdb")
            for name in (
                "full",
                "incremental",
                "synthetic-1",
                "incremental-2",
                "synthetic-2",
                "incremental-3",
                "synthetic-3",
            ):
                log(f"{name:15} {BACKUP_DIR / name}")
            compose("ps", check=False)
        else:
            log("Stopped because a step failed. The summary above shows the checks that ran.")


if __name__ == "__main__":
    main()
