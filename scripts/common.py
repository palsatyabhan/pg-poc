"""Shared helpers for the PostgreSQL incremental-backup POC.

SQL goes through psycopg. pg_basebackup and pg_combinebackup stay container
commands because PostgreSQL does not expose them in the Python driver.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

try:
    import psycopg
except ImportError as exc:  # pragma: no cover - guidance for a missing venv
    raise SystemExit(
        "psycopg is not installed. From the project root, run:\n"
        "  python3 -m venv .venv\n"
        "  source .venv/bin/activate\n"
        "  python -m pip install -r requirements.txt"
    ) from exc

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backup"
REFERENCE_ROWS = 40_000

PORTS = {"source": 5432, "target": 5433}
SERVICES = {"source": "pg-source", "target": "pg-target"}

# Local demo only. pg_basebackup uses a replication connection, which does not
# match the "all" database rules created by the Postgres image.
HBA_CONF = """\
local   all           all                              trust
local   replication   all                              trust
host    all           all           127.0.0.1/32       trust
host    replication   all           127.0.0.1/32       trust
host    all           all           ::1/128            trust
host    replication   all           ::1/128            trust
host    all           all           0.0.0.0/0          trust
host    replication   all           0.0.0.0/0          trust
"""

# Runs inside the pg-tools container. The output directory must not already
# contain files, and a Docker volume mount always exists, so combine to /tmp
# and then copy into the target data directory.
_RESTORE_SHELL = """
set -euo pipefail
rm -rf /tmp/restored
pg_combinebackup -o /tmp/restored {chain}
find /var/lib/postgresql/data -mindepth 1 -delete
cp -a /tmp/restored/. /var/lib/postgresql/data/
rm -f /var/lib/postgresql/data/postmaster.pid /var/lib/postgresql/data/standby.signal
chown -R postgres:postgres /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data
echo "Target data directory now contains the combined backup."
"""

# Writes a synthetic full backup under /backup, then copies that directory
# into the target data directory. The saved synthetic backup is never started,
# so the next round can use it without reading the older backups again.
_SYNTHETIC_SHELL = """
set -euo pipefail
rm -rf /backup/{output} /tmp/restored
pg_combinebackup -o /backup/{output} {inputs}
find /var/lib/postgresql/data -mindepth 1 -delete
cp -a /backup/{output}/. /var/lib/postgresql/data/
rm -f /var/lib/postgresql/data/postmaster.pid /var/lib/postgresql/data/standby.signal
chown -R postgres:postgres /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data
echo "Synthetic full /backup/{output} copied into the target data directory."
"""


# Filled by record() as each script checks a result. print_summary() shows them together.
_checks: list[tuple[str, str, bool]] = []


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def banner(title: str) -> None:
    print(flush=True)
    print("=" * 64, flush=True)
    print(title, flush=True)
    print("=" * 64, flush=True)


def record(name: str, detail: str, passed: bool) -> None:
    _checks.append((name, detail, passed))
    mark = "PASS" if passed else "FAIL"
    log(f"{mark}  {name}: {detail}")


def print_summary() -> None:
    banner("Verification summary")
    if not _checks:
        log("No verification checks were recorded.")
    else:
        width = max(len(name) for name, _, _ in _checks)
        for name, detail, passed in _checks:
            mark = "PASS" if passed else "FAIL"
            print(f"  {mark}  {name.ljust(width)}  {detail}", flush=True)
        passed = sum(1 for _, _, ok in _checks if ok)
        failed = len(_checks) - passed
        print(flush=True)
        log(f"Checks: {len(_checks)} total, {passed} passed, {failed} failed")

    banner("Database contents")
    for database in ("source", "target"):
        try:
            show_rows(database)
        except psycopg.Error as exc:
            log(f"pg-{database} is not available: {exc.__class__.__name__}: {exc}")

    print(flush=True)
    backup_dirs = []
    if BACKUP_DIR.is_dir():
        backup_dirs = sorted(
            path for path in BACKUP_DIR.iterdir() if path.is_dir() and any(path.iterdir())
        )
    if not backup_dirs:
        log("backups: not present")
    for path in backup_dirs:
        log(f"{path.name} backup: {directory_size(path)} at {path}")


def dsn(database: str) -> str:
    port = PORTS[database]
    return f"postgresql://postgres:postgres@127.0.0.1:{port}/appdb"


def connect(database: str, *, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(dsn(database), autocommit=autocommit, connect_timeout=10)


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("Docker is required. Install Docker Desktop and start it, then run this again.")
    result = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit("Docker Compose v2 is required (the 'docker compose' command).")


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    log("docker compose " + " ".join(args))
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=capture,
    )


def ensure_replication_hba() -> None:
    """Write pg_hba.conf from Python and reload it.

    A shell file mounted into docker-entrypoint-initdb.d cannot be executed
    on Docker Desktop: the file mount is noexec, so the kernel reports
    "/bin/bash: bad interpreter: Permission denied" and the container exits.
    """
    log("Writing pg_hba.conf so pg_basebackup can use a local replication connection.")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "-u",
            "postgres",
            "pg-source",
            "tee",
            "/var/lib/postgresql/data/pg_hba.conf",
        ],
        cwd=ROOT,
        input=HBA_CONF,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "could not write pg_hba.conf"
        record("pg_hba.conf", detail, False)
        raise SystemExit(detail)
    log("Wrote pg_hba.conf. Reloading server configuration.")
    with connect("source") as conn:
        conn.execute("SELECT pg_reload_conf()")
    record("pg_hba.conf", "local replication is allowed for pg_basebackup", True)


def wait_for(database: str, timeout: int = 120) -> None:
    """Poll with psycopg until the published port accepts a query."""
    label = f"pg-{database}"
    log(f"Waiting for {label} to accept connections on {dsn(database)}")
    deadline = time.time() + timeout
    last_error: Exception | None = None
    last_logged = 0.0
    while time.time() < deadline:
        try:
            with connect(database) as conn:
                conn.execute("SELECT 1")
            log(f"{label} is ready.")
            return
        except psycopg.Error as exc:
            last_error = exc
            now = time.time()
            if now - last_logged >= 5:
                reason = str(exc).strip().splitlines()[0]
                log(f"{label} not ready yet: {reason}")
                last_logged = now
            time.sleep(1)
    log(f"{label} did not become ready within {timeout} seconds. Recent container logs:")
    compose("logs", "--tail", "80", SERVICES[database], check=False)
    raise SystemExit(f"{label} is not accepting connections: {last_error}")


def print_table(conn: psycopg.Connection, query: str, params: tuple = ()) -> None:
    cursor = conn.execute(query, params)
    columns = [column.name for column in cursor.description or ()]
    rows = cursor.fetchall()
    rendered = [["" if value is None else str(value) for value in row] for row in rows]
    widths = [len(name) for name in columns]
    for row in rendered:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print(" | ".join(name.ljust(width) for name, width in zip(columns, widths)), flush=True)
    print("-+-".join("-" * width for width in widths), flush=True)
    for row in rendered:
        print(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)), flush=True)
    noun = "row" if len(rows) == 1 else "rows"
    print(f"({len(rows)} {noun})", flush=True)


def show_rows(database: str) -> None:
    print(f"\n--- pg-{database} ---")
    with connect(database) as conn:
        print_table(
            conn,
            """
            SELECT id, phase, note
            FROM demo_events
            ORDER BY id
            """,
        )
        print()
        print_table(
            conn,
            """
            SELECT phase, count(*) AS rows
            FROM demo_events
            GROUP BY phase
            ORDER BY phase
            """,
        )
        print()
        print_table(conn, "SELECT count(*) AS reference_rows FROM reference_blob")


def event_phases(conn: psycopg.Connection) -> tuple[int, int]:
    full, incremental = conn.execute(
        """
        SELECT
            coalesce(sum((phase = 'full')::int), 0)::int,
            coalesce(sum((phase = 'incremental')::int), 0)::int
        FROM demo_events
        """
    ).fetchone()
    return int(full), int(incremental)


def assert_phases(database: str, full: int, incremental: int) -> None:
    with connect(database) as conn:
        actual = event_phases(conn)
    detail = (
        f"expected full={full}, incremental={incremental}; "
        f"found full={actual[0]}, incremental={actual[1]}"
    )
    record(f"pg-{database} demo_events", detail, actual == (full, incremental))
    if actual != (full, incremental):
        show_rows(database)
        raise SystemExit(f"pg-{database}: {detail}")


def assert_event_ids(database: str, expected: tuple[int, ...]) -> None:
    with connect(database) as conn:
        rows = conn.execute("SELECT id FROM demo_events ORDER BY id").fetchall()
    actual = tuple(int(row[0]) for row in rows)
    detail = f"expected ids {list(expected)}; found {list(actual)}"
    record(f"pg-{database} demo_events", detail, actual == expected)
    if actual != expected:
        show_rows(database)
        raise SystemExit(f"pg-{database}: {detail}")


def assert_reference_rows(database: str) -> None:
    with connect(database) as conn:
        (actual,) = conn.execute("SELECT count(*) FROM reference_blob").fetchone()
    actual = int(actual)
    detail = f"expected {REFERENCE_ROWS}; found {actual}"
    record(f"pg-{database} reference_blob", detail, actual == REFERENCE_ROWS)
    if actual != REFERENCE_ROWS:
        raise SystemExit(f"pg-{database}: reference_blob {detail}")


def checkpoint_and_switch_wal() -> None:
    """CHECKPOINT and pg_switch_wal() cannot run inside a transaction block."""
    with connect("source", autocommit=True) as conn:
        log("Checkpointing pg-source so the new rows are on disk.")
        conn.execute("CHECKPOINT")
        (lsn,) = conn.execute("SELECT pg_switch_wal()").fetchone()
        log(f"Switched WAL at {lsn}")
        log("WAL summarizer state:")
        print_table(conn, "SELECT * FROM pg_get_wal_summarizer_state()")


def prepare_backup_dir(name: str) -> Path:
    log(f"Clear host folder {BACKUP_DIR / name} before writing the backup.")
    compose("exec", "-T", "-u", "root", "pg-source", "rm", "-rf", f"/backup/{name}")
    compose("exec", "-T", "-u", "root", "pg-source", "mkdir", "-p", "-m", "777", f"/backup/{name}")
    return BACKUP_DIR / name


def directory_size(path: Path) -> str:
    total = sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    units = ("B", "KB", "MB", "GB")
    size = float(total)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{total} B"


def log_backup_position(name: str) -> None:
    """Show the WAL timeline and LSN stored in a backup label."""
    label_path = BACKUP_DIR / name / "backup_label"
    if not label_path.is_file():
        log(f"{name} has no backup_label.")
        return
    log(f"Backup position of {name}:")
    for line in label_path.read_text().splitlines():
        if line.startswith(
            ("START WAL LOCATION:", "START TIMELINE:", "INCREMENTAL FROM LSN:", "INCREMENTAL FROM TLI:", "LABEL:")
        ):
            log(f"  {line}")


def log_server_timeline(database: str) -> None:
    """Show the timeline the running server is on after recovery and promotion."""
    with connect(database) as conn:
        timeline_id, checkpoint_lsn = conn.execute(
            "SELECT timeline_id, checkpoint_lsn FROM pg_control_checkpoint()"
        ).fetchone()
    detail = f"timeline_id={timeline_id} checkpoint_lsn={checkpoint_lsn}"
    log(f"pg-{database} control file: {detail}")
    record(f"pg-{database} timeline", detail, True)


def restore_synthetic(inputs: list[str], output_name: str) -> None:
    """Combine inputs into one saved full backup, then replace pg-target with a copy of it."""
    missing = [part for part in inputs if not (BACKUP_DIR / part / "backup_manifest").is_file()]
    if missing:
        raise SystemExit("Missing backup manifest for: " + ", ".join(missing))
    if inputs[0] != "full" and not inputs[0].startswith("synthetic-"):
        raise SystemExit("A synthetic full must start from backup/full or an earlier synthetic full.")

    banner("Building synthetic full " + output_name + " from " + " + ".join(inputs))
    if len(inputs) == 2 and inputs[0].startswith("synthetic-"):
        log(
            f"pg_combinebackup reads only {inputs[0]} and {inputs[1]}. "
            f"The older backups are already inside {inputs[0]} and are not opened."
        )
    else:
        log(f"This is the first combined copy. Later rounds reuse {output_name} instead of reading these inputs again.")
    log(f"Saving the combined result at ./backup/{output_name}. Postgres is not started on that directory.")
    log("A separate copy of it replaces the target data directory.")
    compose("stop", "pg-target", check=False, capture=True)
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "bash",
        "pg-tools",
        "-c",
        _SYNTHETIC_SHELL.format(
            output=output_name,
            inputs=" ".join(f"/backup/{part}" for part in inputs),
        ),
    )
    log_backup_position(output_name)
    record(
        f"{output_name} inputs",
        "combined from " + " + ".join(inputs),
        (BACKUP_DIR / output_name / "backup_manifest").is_file(),
    )
    log("Starting pg-target on the copied data directory.")
    compose("up", "-d", "pg-target")
    wait_for("target")
    log("Source timeline stays on the original backup history. The target promotes when this restored copy starts.")
    log_server_timeline("source")
    log_server_timeline("target")


def restore_chain(parts: list[str]) -> None:
    """Rebuild pg-target from a full backup followed by incremental backups, oldest first."""
    missing = [part for part in parts if not (BACKUP_DIR / part / "backup_manifest").is_file()]
    if missing:
        raise SystemExit("Missing backup manifest for: " + ", ".join(missing))
    if parts[0] != "full":
        raise SystemExit("The first backup in a restore chain must be the full backup.")
    chain = " ".join(f"/backup/{part}" for part in parts)
    banner("Restoring " + " + ".join(parts) + " into pg-target")
    log(f"pg_combinebackup will read {chain} from the shared host folder and copy it into pg-target.")
    log("Stopping pg-target so its data directory can be replaced.")
    compose("stop", "pg-target", check=False, capture=True)
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "bash",
        "pg-tools",
        "-c",
        _RESTORE_SHELL.format(chain=chain),
    )
    log("Starting pg-target on the restored data directory.")
    compose("up", "-d", "pg-target")
    wait_for("target")


def restore_into_target(mode: str) -> None:
    if mode == "full":
        restore_chain(["full"])
    elif mode == "incremental":
        restore_chain(["full", "incremental"])
    else:
        raise SystemExit(f"Unknown restore mode: {mode}")
