#!/usr/bin/env python3
"""Take one incremental backup based on an earlier backup manifest."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BACKUP_DIR,
    banner,
    checkpoint_and_switch_wal,
    compose,
    directory_size,
    log,
    prepare_backup_dir,
    print_summary,
    record,
    require_docker,
)


def _label_value(label_text: str, prefix: str) -> str | None:
    for line in label_text.splitlines():
        if line.startswith(prefix):
            return line.split()[-1] if prefix.endswith("LSN:") else line.split()[3]
    return None


def take_incremental(name: str, parent: str, *, iteration: int, total: int) -> None:
    require_docker()
    parent_manifest = BACKUP_DIR / parent / "backup_manifest"
    parent_label = BACKUP_DIR / parent / "backup_label"
    if not parent_manifest.is_file():
        raise SystemExit(
            f"Missing backup/{parent}/backup_manifest. Incremental {iteration} is based on that backup."
        )

    banner(f"Taking INCREMENTAL backup {iteration} of {total} (no full backup)")
    log(f"Output directory: ./backup/{name}")
    log(f"Parent manifest: /backup/{parent}/backup_manifest")
    log("pg_basebackup --incremental stores changed blocks in INCREMENTAL.* files.")

    for attempt in range(1, 16):
        prepare_backup_dir(name)
        log(f"Incremental backup attempt {attempt}/15.")
        result = compose(
            "exec",
            "-T",
            "-u",
            "postgres",
            "pg-source",
            "pg_basebackup",
            "-D",
            f"/backup/{name}",
            "-h",
            "/var/run/postgresql",
            "-U",
            "postgres",
            "--checkpoint=fast",
            "--wal-method=stream",
            "--progress",
            f"--label={name}",
            f"--incremental=/backup/{parent}/backup_manifest",
            check=False,
        )
        if result.returncode == 0:
            break
        if attempt == 15:
            raise SystemExit(f"Incremental backup {name} failed after 15 attempts.")
        log("WAL summaries are not ready yet. Checkpointing and retrying.")
        checkpoint_and_switch_wal()
        time.sleep(2)
    else:
        raise SystemExit(f"Incremental backup {name} failed.")

    backup_dir = BACKUP_DIR / name
    label_text = (backup_dir / "backup_label").read_text()
    incremental_files = sorted(backup_dir.rglob("INCREMENTAL.*"))
    parent_size = directory_size(BACKUP_DIR / parent)
    incremental_size = directory_size(backup_dir)
    from_lsn = _label_value(label_text, "INCREMENTAL FROM LSN:")
    parent_start = _label_value(parent_label.read_text(), "START WAL LOCATION:")
    log(f"Parent backup size:      {parent_size} ({parent})")
    log(f"Incremental backup size: {incremental_size}")
    log(f"INCREMENTAL.* files: {len(incremental_files)}")
    log(f"INCREMENTAL FROM LSN {from_lsn}; parent start LSN {parent_start}")

    chained = (
        "INCREMENTAL FROM LSN" in label_text
        and len(incremental_files) > 0
        and from_lsn is not None
        and from_lsn == parent_start
    )
    record(
        f"incremental {iteration} backup",
        f"{incremental_size}, {len(incremental_files)} INCREMENTAL.* files, parent {parent} is {parent_size}",
        chained,
    )
    if not chained:
        raise SystemExit(f"{name} is not an incremental backup based on {parent}.")

    log("Incremental backup_label:")
    print(label_text, flush=True)
    log("Sample incremental files:")
    for path in incremental_files[:8]:
        log(f"  {path.relative_to(backup_dir)}")

    log(f"Running pg_verifybackup on ./backup/{name}.")
    compose("exec", "-T", "-u", "postgres", "pg-source", "pg_verifybackup", f"/backup/{name}")
    record(f"pg_verifybackup incremental {iteration}", f"manifest checksums match ./backup/{name}", True)
    record(
        f"incremental {iteration} parent",
        f"based on {parent} at LSN {parent_start}, not a new full backup",
        True,
    )


def main() -> None:
    take_incremental("incremental", "full", iteration=1, total=1)


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
