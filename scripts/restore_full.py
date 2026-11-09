#!/usr/bin/env python3
"""Restore ./backup/full into a new pg-target instance and check the rows."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    assert_phases,
    assert_reference_rows,
    banner,
    log,
    print_summary,
    require_docker,
    restore_into_target,
    show_rows,
)


def main() -> None:
    require_docker()
    restore_into_target("full")
    banner("Checking the new instance against the full backup")
    log("Both servers should have the two original rows and none of the later rows.")
    assert_phases("source", 2, 0)
    assert_phases("target", 2, 0)
    assert_reference_rows("source")
    assert_reference_rows("target")
    show_rows("target")
    log("pg-target is a new server on localhost:5433. It has only the rows from the full backup.")


if __name__ == "__main__":
    try:
        main()
    finally:
        print_summary()
