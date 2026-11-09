#!/usr/bin/env python3
"""Insert the rows that belong to the next incremental backup."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    assert_event_ids,
    banner,
    checkpoint_and_switch_wal,
    connect,
    log,
    print_summary,
    require_docker,
    show_rows,
)

# Used when this file is run by itself after the full restore.
FIRST_EVENTS = (
    (3, "incr1", "written for incremental 1"),
    (4, "incr1", "also written for incremental 1"),
)


def add_source_rows(
    events: tuple[tuple[int, str, str], ...],
    *,
    iteration: int,
    total: int,
    source_ids: tuple[int, ...],
    target_ids: tuple[int, ...],
) -> None:
    require_docker()
    banner(f"Adding rows for incremental {iteration} of {total} on pg-source only")
    written = ", ".join(f"{event_id} ({phase})" for event_id, phase, _note in events)
    log(f"Inserting demo_events {written}. pg-target is not updated.")
    with connect("source", autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO demo_events (id, phase, note) VALUES (%s, %s, %s)",
                events,
            )

    checkpoint_and_switch_wal()
    log("Source should include the new rows. Target should still be missing them.")
    assert_event_ids("source", source_ids)
    assert_event_ids("target", target_ids)
    show_rows("source")
    show_rows("target")


def main() -> None:
    add_source_rows(
        FIRST_EVENTS,
        iteration=1,
        total=1,
        source_ids=(1, 2, 3, 4),
        target_ids=(1, 2),
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
