# Configuration

PostgreSQL 17 incremental backup needs the same server settings whether the database runs in the containers from `docker-compose.yml` or as a normal install on the host. The source is the server that is backed up. The target is a separate data directory that is restored from those backup files.

`summarize_wal` must already be on before the full backup. An incremental backup can only be taken while the WAL summaries that describe changes since that full backup are still on disk.

## Source and target

| | Source | Target |
| --- | --- | --- |
| Role | Database that is backed up | New database restored from the backup files |
| Container | `pg-source` (`pg-incr-source`) | `pg-target` (`pg-incr-target`) |
| Host port | `5432` | `5433` |
| Port inside the container | `5432` | `5432` |
| Data directory | Docker volume `pg_source_data` | Docker volume `pg_target_data` |
| Backup files | Writes `./backup/full` and `./backup/incremental` | Reads the same `./backup` folder |
| Database, user, password | `appdb` / `postgres` / `postgres` | Same cluster, because restore copies the source data directory |

The Python scripts connect with:

```text
postgresql://postgres:postgres@127.0.0.1:5432/appdb
postgresql://postgres:postgres@127.0.0.1:5433/appdb
```

Those strings are in `scripts/common.py`. A host install that uses another port, user, or password has to match that file, or the checks will not connect.

## Settings required on the source

Put these in the server command or in `postgresql.conf`, then restart. `wal_level`, `summarize_wal`, `wal_summary_keep_time`, and `max_wal_senders` are not applied by a reload.

```text
listen_addresses = '*'
wal_level = replica
summarize_wal = on
wal_summary_keep_time = 7d
max_wal_senders = 10
```

| Setting | Required value | Why |
| --- | --- | --- |
| `wal_level` | `replica` or `logical` | `summarize_wal` cannot run while `wal_level` is `minimal`. `replica` is the PostgreSQL 17 default; the compose file sets it explicitly. |
| `summarize_wal` | `on` | Turns on the WAL summarizer. Incremental `pg_basebackup` reads those summaries. The default is `off`. |
| `wal_summary_keep_time` | Longer than the gap between the full backup and the incremental backup | The POC keeps summaries for 7 days. The server default is 10 days. Summaries older than this are deleted, and the next incremental backup then fails. |
| `max_wal_senders` | At least `1`, POC uses `10` | `pg_basebackup` uses a replication connection. The default is already `10`. |
| `listen_addresses` | `*` in the containers | The published host ports `5432` and `5433` have to reach Postgres. A host install that is only used from the same machine can use `localhost`. |

The source image is also created with checksums:

```text
POSTGRES_INITDB_ARGS=--data-checksums
```

Checksums are optional for incremental backup. They can only be chosen when the data directory is first created with `initdb`. A restore keeps them because they are part of the data files, not a startup flag.

Confirm the source before taking the full backup:

```sql
SELECT name, setting
FROM pg_settings
WHERE name IN ('wal_level', 'summarize_wal', 'wal_summary_keep_time', 'max_wal_senders');

SELECT * FROM pg_get_wal_summarizer_state();
```

`summarize_wal` must be `on`, and `pg_get_wal_summarizer_state()` must show a `summarizer_pid`.

## Authentication on the source

`pg_basebackup` opens a replication connection. A rule for `all` databases does not match that connection. After the container is up, `scripts/common.py` writes this `pg_hba.conf` and runs `SELECT pg_reload_conf()`:

```text
local   all           all                              trust
local   replication   all                              trust
host    all           all           127.0.0.1/32       trust
host    replication   all           127.0.0.1/32       trust
host    all           all           ::1/128            trust
host    replication   all           ::1/128            trust
host    all           all           0.0.0.0/0          trust
host    replication   all           0.0.0.0/0          trust
```

This is only for the local demo. `trust` accepts any connection that matches the address. Do not use the `0.0.0.0/0` lines on a shared network.

The backup command connects through the container's Unix socket as the `postgres` operating-system user:

```text
pg_basebackup -h /var/run/postgresql -U postgres
```

The database role must be a superuser or have the `REPLICATION` attribute. The `postgres` superuser already qualifies.

`pg_hba.conf` lives in the data directory, so the copy taken by the full backup is the copy that the target starts with.

## Settings required on the target

The target needs a PostgreSQL 17 server and an empty data directory. It does not need `summarize_wal` just to restore. The compose file still starts it with:

```text
listen_addresses = '*'
wal_level = replica
summarize_wal = on
```

Those `-c` flags are process arguments. They are not stored in the backed-up `postgresql.conf`. After a restore, the target only keeps them because `docker-compose.yml` passes them again on the next start.

The restored data directory must end up like this before Postgres starts:

| Item | Value |
| --- | --- |
| Owner | `postgres:postgres` |
| Mode | `0700` on the data directory |
| `postmaster.pid` | Removed, if a copy is present |
| `standby.signal` | Removed. This POC starts a normal server, not a standby |
| Backup folder | Left untouched. Postgres rewrites a data directory when it starts, so the server must not be pointed at `./backup/full` |

`pg_combinebackup` refuses to write into a directory that already has files. The restore helper combines the backups into `/tmp/restored`, clears the target data directory, and copies the result in. The helper is the `pg-tools` service. It uses the `postgres:17` image, runs as root, and is not started by a normal `docker compose up`. It mounts the target data volume and `./backup`.

`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` on the target matter only when Docker initializes an empty data directory. After a restore, roles and the `appdb` database are whatever was in the source backup.

## Container layout

Both database containers use `postgres:17`, `shm_size: 256mb`, and the host folder `./backup` mounted at `/backup`.

`shm_size` is a Docker setting, not a Postgres parameter. A container's default `/dev/shm` is 64 MB, which is tight for Postgres. A normal host install uses the operating system's shared memory and does not need this value.

| Volume or folder | Source | Target |
| --- | --- | --- |
| `/var/lib/postgresql/data` | `pg_source_data` | `pg_target_data` |
| `/backup` | `./backup` on the host | The same `./backup` on the host |
| `./backup/full` | Full `pg_basebackup` output | Input to `pg_combinebackup` |
| `./backup/incremental` | Incremental `pg_basebackup` output | Second input to `pg_combinebackup`, after `full` |

The backup directory has to be empty before each `pg_basebackup`. The scripts delete and recreate `full` or `incremental` first. The operating-system user that runs `pg_basebackup` must be able to create files there. In the containers that user is `postgres` (uid 999), and the scripts create the directory mode `0777` so the write succeeds through the Docker mount.

## Host-installed Postgres

Use this when the source, the target, or both are installed on the machine instead of started by `docker-compose.yml`. The current Python scripts still call `docker compose` for backup and restore. The settings and commands below are what that host database itself needs. Point `PORTS` and the connection strings in `scripts/common.py` at the host instance if you want the row checks to use it.

Run two separate clusters if both are on one machine. They need different data directories and different ports.

```text
Source data directory:  /var/lib/postgresql/17/source
Source port:            5432
Target data directory:  /var/lib/postgresql/17/target
Target port:            5433
Backup directory:       /var/backups/postgres
```

Create the source with checksums if you want the same choice as the containers:

```bash
initdb --data-checksums --username=postgres --pwfile=<(printf '%s\n' 'postgres') -D /var/lib/postgresql/17/source
```

### Source `postgresql.conf`

```text
listen_addresses = 'localhost'
port = 5432
wal_level = replica
summarize_wal = on
wal_summary_keep_time = '7d'
max_wal_senders = 10
```

Restart after saving that file:

```bash
pg_ctl -D /var/lib/postgresql/17/source restart
```

### Source `pg_hba.conf` for a host

Prefer a local socket and peer authentication when `pg_basebackup` runs as the `postgres` operating-system user. Add a password rule only for the machine that will connect with psycopg.

```text
local   all           postgres                             peer
local   replication   postgres                             peer
host    all           postgres        127.0.0.1/32         scram-sha-256
host    all           postgres        ::1/128              scram-sha-256
```

Reload after editing `pg_hba.conf`:

```bash
pg_ctl -D /var/lib/postgresql/17/source reload
```

Create the application database once:

```sql
CREATE DATABASE appdb;
```

### Backup directories on the host

Keep backups outside the data directory.

```bash
mkdir -p /var/backups/postgres/full /var/backups/postgres/incremental
chown -R postgres:postgres /var/backups/postgres
chmod 700 /var/backups/postgres
```

`full` and `incremental` must be empty before each backup. `pg_basebackup` will not reuse a directory that already contains files.

Full backup, run as `postgres`:

```bash
pg_basebackup \
  -D /var/backups/postgres/full \
  -h /var/run/postgresql \
  -U postgres \
  --checkpoint=fast \
  --wal-method=stream \
  --progress \
  --label=full
```

Incremental backup, after more data has been written. This uses the full backup's manifest and does not take another full backup:

```bash
pg_basebackup \
  -D /var/backups/postgres/incremental \
  -h /var/run/postgresql \
  -U postgres \
  --checkpoint=fast \
  --wal-method=stream \
  --progress \
  --label=incremental \
  --incremental=/var/backups/postgres/full/backup_manifest
```

`backup_label` for the second run must contain `INCREMENTAL FROM LSN`. The directory should contain `INCREMENTAL.*` files.

### Target on the host

Initialize a second cluster on port `5433`, or reuse an existing target data directory. Stop it before replacing its files.

```text
listen_addresses = 'localhost'
port = 5433
```

```bash
pg_ctl -D /var/lib/postgresql/17/target stop
rm -rf /tmp/pg-restored
pg_combinebackup -o /tmp/pg-restored \
  /var/backups/postgres/full \
  /var/backups/postgres/incremental
find /var/lib/postgresql/17/target -mindepth 1 -delete
cp -a /tmp/pg-restored/. /var/lib/postgresql/17/target/
rm -f /var/lib/postgresql/17/target/postmaster.pid /var/lib/postgresql/17/target/standby.signal
chown -R postgres:postgres /var/lib/postgresql/17/target
chmod 700 /var/lib/postgresql/17/target
pg_ctl -D /var/lib/postgresql/17/target start
```

For a restore of the full backup only, pass `/var/backups/postgres/full` to `pg_combinebackup` and omit the incremental directory. The order is always the full backup first, then each incremental backup from oldest to newest.

Set `port = 5433` again after restore if the copied source configuration says `port = 5432` and both clusters run on the same machine. Command-line `-c listen_addresses` and `-c summarize_wal` from Docker are not in a host `postgresql.conf` unless you add them there.

### One side in Docker and one side on the host

The backup folder has to be a real directory that both sides can read.

| Source | Target | What has to match |
| --- | --- | --- |
| Container | Host | Mount `./backup` into the source container. Run `pg_combinebackup` on the host against `./backup/full` and `./backup/incremental`. |
| Host | Container | Take the host backups in a directory that is mounted into `pg-target` and `pg-tools` at `/backup`. |
| Host | Host | Use `/var/backups/postgres` directly. No Docker mount is involved. |

`pg_basebackup` and `pg_combinebackup` must both be PostgreSQL 17, the same major version as the source server.

## What the backup carries

| Carried in the backup files | Set again on the next start |
| --- | --- |
| Databases, roles, and table data | Docker `shm_size` |
| `pg_hba.conf` as it was when the backup was taken | `postgres -c` flags such as `summarize_wal` and `listen_addresses` |
| Data checksums, if `initdb` enabled them | Host port mapping (`5432` and `5433`) |
| `postgresql.conf` stored in the data directory | `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` after the first restore |

The target container publishes `5433` so it can run beside the source. Inside the container the server still listens on `5432`.
