#!/bin/sh
# Wave V15 / 5-04 (#8 LOCKED) — the SCHEDULED daily backup, as a compose sidecar.
#
# WHY A SIDECAR: the finding (U1-F4) was "exactly one backup set exists and nothing schedules
# `make backup`".  The parent decision locks the compose-sidecar route: this script runs as the
# `backup` service (docker-compose.yml, image postgres:16-alpine — pg_dump's version must match
# the server it dumps) in a loop — one cycle immediately, then every BACKUP_INTERVAL_SECONDS
# (default 24h) — writing into the SAME ./backups/ directory `make backup` uses.  `make backup`
# is untouched and stays the on-demand path; this is the scheduler it never had.
#
# HOW IT READS THE STACK (no docker socket, no docker CLI inside the container):
#   * the database via pg_dump over the compose network (PGHOST=postgres);
#   * each DATA volume through a READ-ONLY mount of the named volume at $VOLUMES_ROOT/<name> —
#     a compose volume mount IS a directory in the container, so plain `tar` is the whole story.
#
# HONEST FAILURE MODES (mirrors the `make backup` guard, Makefile:82-84): a missing or EMPTY
# volume mount is REFUSED BY NAME — `docker run -v`/a mistyped mount would otherwise tar an empty
# volume into a green-looking, worthless archive.  A failed pg_dump writes NO dump (the temp
# `.part` is removed; the safe name is only ever created by a rename after success).  A failed
# cycle exits non-zero for `--once` and logs loudly in the loop (the previous cycle's artifacts
# stay valid; the next cycle retries).  SIGTERM/SIGINT end the loop cleanly.
#
# USAGE
#   backup.sh            # loop: run a cycle now, then every BACKUP_INTERVAL_SECONDS (compose entrypoint)
#   backup.sh --once     # exactly one cycle; exit code = cycle result (operator drill / tests)
#
# ENV (the compose service sets the first block; every value has a default)
#   POSTGRES_USER, POSTGRES_DB, PGHOST, PGPASSWORD   database connection
#   BACKUP_DIR=/backups                              artifact directory (compose: ./backups bind mount)
#   BACKUP_INTERVAL_SECONDS=86400                    loop cadence (24 h)
#   BACKUP_RETENTION_DAYS=14                         prune artifacts older than this
#   BACKUP_VOLUMES="backend_data chroma_data redis_data celery_beat_data"
#   VOLUMES_ROOT=/volumes                            where the read-only volume mounts live

log() {
    printf '[backup] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

POSTGRES_USER=${POSTGRES_USER:-polsia}
POSTGRES_DB=${POSTGRES_DB:-polsia}
PGHOST=${PGHOST:-postgres}
BACKUP_DIR=${BACKUP_DIR:-/backups}
BACKUP_INTERVAL_SECONDS=${BACKUP_INTERVAL_SECONDS:-86400}
BACKUP_RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
BACKUP_VOLUMES=${BACKUP_VOLUMES:-"backend_data chroma_data redis_data celery_beat_data"}
VOLUMES_ROOT=${VOLUMES_ROOT:-/volumes}

backup_database() {
    stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
    target="$BACKUP_DIR/polsia-$stamp.sql"
    part="$target.part"
    log "pg_dump $POSTGRES_DB@$PGHOST -> $target"
    if pg_dump -h "$PGHOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$part"; then
        mv "$part" "$target"
        log "database dump written: $target ($(wc -c < "$target") bytes)"
        return 0
    else
        code=$?
        rm -f "$part"
        log "ERROR: pg_dump failed (exit $code) for $POSTGRES_DB@$PGHOST — no dump written this cycle"
        return 1
    fi
}

backup_volume() {
    name=$1
    src="$VOLUMES_ROOT/$name"
    if [ ! -d "$src" ]; then
        log "ERROR: volume mount $src is MISSING — refusing to archive $name (a missing mount would 'back up' nothing)"
        return 1
    fi
    if [ -z "$(ls -A "$src" 2>/dev/null)" ]; then
        log "ERROR: volume mount $src is EMPTY — refusing (an empty tar is a green-looking, worthless backup; is the volume name right?)"
        return 1
    fi
    target="$BACKUP_DIR/$name.tgz"
    part="$target.part"
    # `tar -f` treats an argument containing a colon as a remote "host:path" spec on GNU tar, so
    # the archive is created under its BARE name from inside the backup dir (colon-proof on any
    # tar; busybox has no such quirk) and `-C` (which has no remote check) points at the mount.
    # The safe name only ever appears via the rename-after-success below.
    if (cd "$BACKUP_DIR" && tar czf "$name.tgz.part" -C "$src" .); then
        mv "$part" "$target"
        log "volume archive written: $target ($(wc -c < "$target") bytes)"
        return 0
    else
        code=$?
        rm -f "$part"
        log "ERROR: tar failed for $name (exit $code) — archive not written"
        return 1
    fi
}

prune_old_backups() {
    log "pruning artifacts older than ${BACKUP_RETENTION_DAYS} day(s) in $BACKUP_DIR"
    found=$(find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'polsia-*.sql' -o -name '*.tgz' \) -mtime "+$BACKUP_RETENTION_DAYS" -print)
    if [ -z "$found" ]; then
        log "nothing older than the retention window — nothing pruned"
        return 0
    fi
    # shellcheck disable=SC2086 -- newline-separated paths from find, split on purpose
    for candidate in $found; do
        rm -f "$candidate"
        log "pruned: $candidate"
    done
    return 0
}

run_cycle() {
    log "cycle start (dir=$BACKUP_DIR interval=${BACKUP_INTERVAL_SECONDS}s retention=${BACKUP_RETENTION_DAYS}d volumes='$BACKUP_VOLUMES')"
    if ! mkdir -p "$BACKUP_DIR"; then
        log "ERROR: cannot create/use backup directory $BACKUP_DIR — cycle refused"
        return 1
    fi
    status=0
    backup_database || status=1
    for name in $BACKUP_VOLUMES; do
        backup_volume "$name" || status=1
    done
    prune_old_backups || status=1
    if [ "$status" -eq 0 ]; then
        log "cycle complete: database dump + volumes archived, retention applied"
    else
        log "cycle complete WITH ERRORS (see above) — artifacts written this cycle remain valid; retrying next interval"
    fi
    return "$status"
}

case "${1:-}" in
    --once)
        run_cycle
        exit $?
        ;;
    "")
        ;;
    *)
        log "ERROR: unknown argument '$1' — usage: backup.sh [--once]"
        exit 2
        ;;
esac

# SIGTERM (docker stop) and SIGINT end the loop cleanly instead of waiting out the sleep.
trap 'log "received termination signal — exiting cleanly"; exit 0' INT TERM

log "backup sidecar: running a cycle now, then every ${BACKUP_INTERVAL_SECONDS}s (retention ${BACKUP_RETENTION_DAYS}d, dir $BACKUP_DIR)"
while :; do
    run_cycle || true
    sleep "$BACKUP_INTERVAL_SECONDS" &
    sleeper=$!
    wait "$sleeper" || true
done
