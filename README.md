# PostgreSQL 17 incremental backup POC

This POC shows what PostgreSQL 17 incremental backup does, and what it does not do.

One container, `pg-source`, is the database that is backed up. A second container, `pg-target`, is restored from files written to `./backup` on your machine. Both containers mount that same folder. The full backup is taken once. Three later backups are incremental. Each restore builds a new data directory for the target. An incremental file is never applied by itself onto the running target.

Server settings for the containers and for a host install are in [Config.md](Config.md).

## How the POC works

`python scripts/run_all.py` deletes this project's containers and volumes, then does the following.

1. Start `pg-source` on port `5432` with `summarize_wal=on`. psycopg loads two `demo_events` rows (`full`) and a 40,000-row table that is not changed again.
2. `pg_basebackup` writes a full copy to `./backup/full`.
3. `pg-target` on port `5433` is replaced with a copy of that full backup. It has rows 1–2.
4. Three rounds follow. Each round adds two rows on the source only, takes one incremental backup, saves a synthetic full backup, and replaces the target with a copy of that synthetic full.

```text
pg-source (localhost:5432)
  full backup -----------------> ./backup/full
  incremental 1, parent = full -> ./backup/incremental
  incremental 2, parent = incremental -> ./backup/incremental-2
  incremental 3, parent = incremental-2 -> ./backup/incremental-3

restore copies written for pg-target (localhost:5433)
  synthetic-1 = full + incremental
  synthetic-2 = synthetic-1 + incremental-2
  synthetic-3 = synthetic-2 + incremental-3
```

Taking a backup and restoring it are different chains.

The backup chain records what changed on the source. Incremental 2 is based on the manifest of incremental 1. Incremental 3 is based on incremental 2. Each `backup_label` stays on timeline 1 and its start LSN moves forward (`0/3000028`, then `0/6000028`, then `0/9000028`, then `0/C000028` in the last run).

The restore chain avoids opening the oldest directories twice. `pg_combinebackup` writes `synthetic-1` from `full` and `incremental`, and that directory is kept. The next combine reads only `synthetic-1` and `incremental-2`. The round after that reads only `synthetic-2` and `incremental-3`. Postgres is not started on a synthetic directory. A copy of it replaces the target data directory, and the target is started from that copy.

Queries and row checks use psycopg. `pg_basebackup` and `pg_combinebackup` run inside the containers. The Python driver cannot take or combine a file-system backup.

After the source is up, Python writes `pg_hba.conf` and reloads it so `pg_basebackup` can open a replication connection. A shell script mounted from the Mac cannot be used for that: Docker Desktop refuses to execute it, and the container then exits during first-time setup.

## What each step proves

| Step | What happens | What you should see |
| --- | --- | --- |
| 1 | Source starts and is seeded | Rows 1–2, phase `full`, plus 40,000 reference rows |
| 2 | Full backup to `./backup/full` | `backup_manifest`, `backup_label`, about 58 MB |
| 3 | Target is replaced from `full` | Both servers have ids 1–2 |
| 4 | Two `incr1` rows are inserted on the source only | Source ids 1–4. Target still ids 1–2 |
| 5 | Incremental backup against `backup/full` | `./backup/incremental` contains `INCREMENTAL.*` files and `INCREMENTAL FROM LSN` |
| 6 | `synthetic-1` is saved from `full` + `incremental`. Target is replaced from `synthetic-1` | Both servers have ids 1–4. Timeline id stays 1 |
| 7 | Two `incr2` rows are inserted on the source only | Source ids 1–6. Target still ids 1–4 |
| 8 | Incremental backup against `backup/incremental` | `./backup/incremental-2`, parent LSN is incremental 1 |
| 9 | `synthetic-2` is saved from `synthetic-1` + `incremental-2` only | `full` and `incremental` are not opened. Both servers have ids 1–6 |
| 10 | Two `incr3` rows are inserted on the source only | Source ids 1–8. Target still ids 1–6 |
| 11 | Incremental backup against `backup/incremental-2` | `./backup/incremental-3` |
| 12 | `synthetic-3` is saved from `synthetic-2` + `incremental-3` only | Both servers have ids 1–8 and 40,000 reference rows |

The script prints a timestamped log for every action and a pass/fail summary at the end. The last successful run reported 47 passed, 0 failed.

## Constraints

These have to be true or the backup and restore commands fail.

- The server must be PostgreSQL 17 or newer. Incremental `pg_basebackup` and `pg_combinebackup` do not exist before 17. `pg_basebackup` and `pg_combinebackup` must be the same major version as the source.
- `summarize_wal` must be `on` before the full backup, and it must stay on until the last incremental backup. The default is `off`.
- `wal_level` must be `replica` or `logical`. `summarize_wal` does not run when `wal_level` is `minimal`.
- `max_wal_senders` must allow the `pg_basebackup` connection. The POC sets `10`.
- WAL summaries must still be on disk for the whole range from the parent backup's start LSN to the new backup. `wal_summary_keep_time` is `7d` here. A summary that has already been deleted makes the next incremental backup fail.
- `pg_hba.conf` needs a `replication` rule. A rule for all databases does not match `pg_basebackup`. The role must be a superuser or have `REPLICATION`. The POC uses the `postgres` superuser and `trust` authentication.
- Backups are plain directories, not tar. `pg_basebackup` refuses to write into a directory that already has files. Each backup directory is emptied first.
- An incremental backup must name the `backup_manifest` of its parent, and the parent's start LSN must be the incremental's `INCREMENTAL FROM LSN`. The parents in this POC are `full`, then `incremental`, then `incremental-2`.
- The target must be stopped before its data directory is replaced. The replacement directory must be owned by `postgres` and be mode `0700`. `postmaster.pid` and `standby.signal` are removed so the copy starts as its own server.
- Ports `5432` and `5433` must be free. The scripts connect as `postgres` / `postgres` to database `appdb` on `127.0.0.1`.
- Docker is required for `run_all.py`. The image is `postgres:17`, and each database container has `shm_size: 256mb` because a container's default shared memory is too small.

## PostgreSQL limitations

These limits belong to PostgreSQL. The POC follows them. They are not choices of the demo scripts.

- An incremental backup is not a database. PostgreSQL stores changed blocks in `INCREMENTAL.*` files and records the parent in `backup_manifest`. The server cannot start from those files, and it has no command that copies only those blocks into a data directory that is already running.
- Restore of an incremental backup always requires a full backup image. `pg_combinebackup` is the only restore tool. Its first input must be a full backup, or a synthetic full produced by an earlier `pg_combinebackup`. The incremental supplies the changed blocks. The tool writes a new complete data directory. There is no mode that applies only the delta.
- A differential backup has the same restore rule. PostgreSQL has no differential command. Pointing `--incremental` at the original full backup makes that backup contain every change since the full backup, which is the usual differential shape. Restore is still `pg_combinebackup` of that full backup plus the differential. Intermediate backups can be skipped. The full backup cannot.
- A data directory that Postgres has started is no longer a backup. Startup removes `backup_label`, writes WAL, and checkpoints. `pg_combinebackup` will not treat that directory as the parent of the next incremental. The parent has to be backup files that have not been started.
- Restore work follows the size of the database. The output is always a full data directory, so the write is the size of the database at that LSN. Reading a shorter chain, synthetic full plus the newest incremental, removes repeated reads of the oldest files. It does not make the output smaller.
- Incremental backup stays on the source timeline. Each backup moves the WAL LSN forward on timeline 1. It does not open timeline 2 or 3. A new timeline is a property of a server that is promoted during recovery. It is not something an incremental backup file creates.
- The only PostgreSQL mechanism that applies changes without rebuilding the data directory is WAL replay. The restored server has to stay in recovery, or be a streaming standby, and then it applies WAL records. That is a different feature from `pg_basebackup --incremental`. File-level incremental backup does not replay onto an already promoted server.
- Incremental backup also depends on WAL summaries. `summarize_wal` must have been on for the whole range since the parent backup, and `wal_level` cannot be `minimal`. If the summaries are gone, PostgreSQL refuses the incremental backup. `pg_basebackup` uses a replication connection, so `max_wal_senders` and a `replication` rule in `pg_hba.conf` are required as well.

## Why the POC is implemented this way

| What PostgreSQL requires | What the scripts do |
| --- | --- |
| One real full backup before any incremental backup | `./backup/full` is taken once and is never replaced by a second full backup |
| Each incremental backup names the previous backup manifest | Incremental 1 uses `full`, incremental 2 uses `incremental`, incremental 3 uses `incremental-2` |
| `pg_combinebackup` must start from a full image | Round 1 combines `full` + `incremental` and saves `synthetic-1` |
| A combined backup may stand in for the chain that produced it | Round 2 reads only `synthetic-1` + `incremental-2`. Round 3 reads only `synthetic-2` + `incremental-3` |
| The combine output is a full data directory, and a started directory cannot be reused as a backup | The synthetic directory is left untouched. A copy replaces `pg-target`, and the target is started from that copy |
| Changed blocks cannot be patched onto the live target | The target is stopped and its data directory is replaced on every restore |

## Requirements

- Docker Desktop, or another engine, with `docker compose`
- Python 3.9 or newer
- Free host ports `5432` and `5433`
- The `postgres:17` image, which the script pulls

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run the whole POC

```bash
python scripts/run_all.py
```

The run fails if a row check, backup label, or manifest check does not match.

## Run one step at a time

These commands follow the first full backup and the first incremental round. They do not run incremental 2 or incremental 3.

```bash
python scripts/reset.py
python scripts/start_and_seed.py
python scripts/full_backup.py
python scripts/restore_full.py
python scripts/add_more_data.py
python scripts/incremental_backup.py
python scripts/restore_incremental.py
```

Connect from the host:

```python
import psycopg
with psycopg.connect("postgresql://postgres:postgres@127.0.0.1:5433/appdb") as conn:
    print(conn.execute("SELECT id, phase, note FROM demo_events ORDER BY id").fetchall())
```

## Why the backup folder is not the target data directory

Postgres rewrites the data directory when it starts. The host folder has to stay an unmodified backup, because the next incremental manifest and the next `pg_combinebackup` read those files. The target container copies a synthetic full into its own volume and starts that copy.

## Clean up

```bash
python scripts/reset.py
```
