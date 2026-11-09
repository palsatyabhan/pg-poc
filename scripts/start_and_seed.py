#!/usr/bin/env python3
"""Start pg-source and load the rows that belong in the full backup."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BACKUP_DIR,
    REFERENCE_ROWS,
    assert_phases,
    assert_reference_rows,
    banner,
    compose,
    connect,
    ensure_replication_hba,
    log,
    print_summary,
    print_table,
    record,
    require_docker,
    show_rows,
    wait_for,
)

FULL_EVENTS = (
    (1, "full", "written before the full backup"),
    (2, "full", "also written before the full backup"),
)


def main() -> None:
    require_docker()
    banner("Starting pg-source and loading the initial data")
    log("These rows are the data that the full backup must contain.")
    log(f"Host backup folder is {BACKUP_DIR}. It is mounted into both Postgres containers.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    compose("up", "-d", "pg-source")
    wait_for("source")
    ensure_replication_hba()

    with connect("source", autocommit=False) as conn:
        log("Server settings used by incremental backup:")
        print_table(
            conn,
            """
            SELECT name, setting
            FROM pg_settings
            WHERE name = ANY(%s)
            ORDER BY name
            """,
            (["summarize_wal", "wal_level"],),
        )
        (summarize_wal,) = conn.execute("SHOW summarize_wal").fetchone()
        record("summarize_wal", f"setting is {summarize_wal}", summarize_wal == "on")
        if summarize_wal != "on":
            raise SystemExit("summarize_wal must be on before the full backup, or incremental backup will fail.")
        log(f"Inserting {REFERENCE_ROWS} reference rows. These stay unchanged so the next backup can be incremental.")
        log("Inserting demo_events 1 and 2 with phase 'full'.")

        conn.execute(
            """
            CREATE TABLE reference_blob (
                id integer PRIMARY KEY,
                payload text NOT NULL
            )
            """
        )
        # Left unchanged after the full backup, so the later incremental backup
        # stores changed blocks instead of another copy of this table.
        conn.execute(
            """
            INSERT INTO reference_blob (id, payload)
            SELECT g, repeat(md5(g::text), 8)
            FROM generate_series(1, %s) AS g
            """,
            (REFERENCE_ROWS,),
        )
        conn.execute(
            """
            CREATE TABLE demo_events (
                id integer PRIMARY KEY,
                phase text NOT NULL,
                note text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO demo_events (id, phase, note) VALUES (%s, %s, %s)",
                FULL_EVENTS,
            )

    log("Seed committed. Checking the source rows.")
    assert_phases("source", 2, 0)
    assert_reference_rows("source")
    show_rows("source")
    log("Source is ready on localhost:5432, database appdb, user postgres, password postgres.")


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
