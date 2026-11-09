#!/usr/bin/env python3
"""Take a full pg_basebackup into ./backup/full on the host."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import banner, compose, directory_size, log, prepare_backup_dir, print_summary, record, require_docker


def main() -> None:
    require_docker()
    banner("Taking a FULL backup onto the host folder ./backup/full")
    log("pg_basebackup copies the whole data directory. This is the only full backup in the POC.")
    backup_dir = prepare_backup_dir("full")
    compose(
        "exec",
        "-T",
        "-u",
        "postgres",
        "pg-source",
        "pg_basebackup",
        "-D",
        "/backup/full",
        "-h",
        "/var/run/postgresql",
        "-U",
        "postgres",
        "--checkpoint=fast",
        "--wal-method=stream",
        "--progress",
        "--label=full",
    )

    manifest = backup_dir / "backup_manifest"
    label = backup_dir / "backup_label"
    if not manifest.is_file() or not label.is_file():
        record("full backup files", f"missing manifest or label in {backup_dir}", False)
        raise SystemExit(f"Full backup did not land in {backup_dir}")

    label_text = label.read_text()
    size = directory_size(backup_dir)
    log(f"Full backup size: {size}")
    log("Full backup files on the host:")
    files = sorted(path.relative_to(backup_dir) for path in backup_dir.rglob("*") if path.is_file())
    for relative in files[:20]:
        log(f"  {relative}")
    if len(files) > 20:
        log(f"  ... {len(files) - 20} more files")
    log("backup_label:")
    print(label_text, flush=True)
    record("full backup files", f"{size} written to {backup_dir}", True)
    record("full backup label", "label is a full streamed backup", "LABEL: full" in label_text and "INCREMENTAL FROM LSN" not in label_text)

    log("Running pg_verifybackup on ./backup/full.")
    compose("exec", "-T", "-u", "postgres", "pg-source", "pg_verifybackup", "/backup/full")
    record("pg_verifybackup full", "manifest checksums match the full backup", True)


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
