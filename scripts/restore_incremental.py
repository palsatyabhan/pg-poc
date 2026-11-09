#!/usr/bin/env python3
"""Rebuild pg-target from the full backup plus one or more incremental backups."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BACKUP_DIR,
    assert_event_ids,
    assert_reference_rows,
    banner,
    log,
    print_summary,
    require_docker,
    restore_synthetic,
    show_rows,
)


def restore_and_check(
    combine_inputs: list[str],
    synthetic_name: str,
    expected_ids: tuple[int, ...],
    *,
    iteration: int,
    total: int,
) -> None:
    require_docker()
    restore_synthetic(combine_inputs, synthetic_name)
    banner(f"Checking pg-target after incremental restore {iteration} of {total}")
    log("Both servers should now contain every row written up to this incremental backup.")
    assert_event_ids("source", expected_ids)
    assert_event_ids("target", expected_ids)
    assert_reference_rows("source")
    assert_reference_rows("target")
    show_rows("source")
    show_rows("target")
    log(f"Target was replaced from the saved synthetic full ./backup/{synthetic_name}.")
    log("Files combined for that synthetic full:")
    for part in combine_inputs:
        log(f"  {BACKUP_DIR / part}")


def main() -> None:
    restore_and_check(
        ["full", "incremental"],
        "synthetic-1",
        (1, 2, 3, 4),
        iteration=1,
        total=1,
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
