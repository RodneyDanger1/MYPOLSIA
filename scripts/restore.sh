#!/bin/sh
# Wave V17 / lane L5 — the RESTORE DRILL: proves a backup can actually be restored.
#
# WHY THIS EXISTS: `make restore` was instructions-as-a-target — a backup that has never been
# restored is an unproven backup.  This script restores FOR REAL into a THROWAWAY container and
# verifies the result against a manifest it derives from the dump itself.
#
# WHAT IT DOES (never the live stack)
#   1. validate the artifacts in --src: find the newest `polsia-*.sql`, require every canonical
#      volume archive (`*.tgz`), require each one to be a readable gzip tar, require the dump to
#      carry a pg_dump header — otherwise exit 2 naming exactly what is wrong;
#   2. `docker run -d --name polsia-restore-drill-pg` from `postgres:16-alpine` — NO published
#      ports, NO compose project, NO live volume, NO live network;
#   3. wait for `pg_isready`, then `createdb`;
#   4. load the dump — plain-SQL artifacts (what `make backup` and `scripts/backup.sh` write) go
#      through `psql -v ON_ERROR_STOP=1`; custom-format artifacts (PGDMP magic) go through
#      `pg_restore --no-owner --no-privileges`;
#   5. verify: public table count + per-table row counts for a fixed core set, compared against a
#      manifest parsed out of the dump's own COPY blocks; then extract every volume archive into
#      a temp dir and prove each listed member materialised;
#   6. ALWAYS tear down (`docker rm -f`) via a trap — success, failure, Ctrl-C or SIGTERM.
#
# USAGE
#   scripts/restore.sh --src backups            # execute the drill; exit 0 PASS / 1 FAIL / 2 refusal
#   scripts/restore.sh --src backups --dry-run  # print the plan and the exact commands; touches nothing
#   scripts/restore.sh --src backups --keep     # skip teardown (debugging; prints the manual rm)
#
# ENV (every value has a safe default)
#   RESTORE_PG_CONTAINER=polsia-restore-drill-pg   throwaway container name (must not already exist)
#   RESTORE_PG_IMAGE=postgres:16-alpine            image (pg_dump version must match the server)
#   RESTORE_PG_PASSWORD=polsia_drill_pw            password INSIDE the throwaway container only
#   POSTGRES_USER=polsia                           role created in the throwaway container
#   RESTORE_VOLUMES="backend_data chroma_data celery_beat_data redis_data"
#   RESTORE_CORE_TABLES="activity_log agent_runs tasks memory_entries task_schedules chat_messages company_config"
#
# This script has no destructive path against a real stack: it only creates and removes its own
# container, and it refuses to run if that container name already exists.
set -u

log() { printf '[restore] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
refuse() {
    printf '[restore] REFUSED: %s\n' "$*"
    exit 2
}
usage() {
    printf 'usage: restore.sh --src <backup-dir> [--dry-run] [--keep]\n'
    printf '  <backup-dir> must hold a polsia-*.sql dump and the volume *.tgz archives\n'
    printf '  written by `make backup` or `scripts/backup.sh`.\n'
}

SRC=""
DRY_RUN=0
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --src)
            [ $# -ge 2 ] || refuse "--src needs a directory argument"
            SRC=$2
            shift 2
            ;;
        --src=*)
            SRC=${1#--src=}
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --keep)
            KEEP=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf '[restore] REFUSED: unknown argument "%s"\n' "$1"
            usage
            exit 2
            ;;
    esac
done

NAME=${RESTORE_PG_CONTAINER:-polsia-restore-drill-pg}
IMAGE=${RESTORE_PG_IMAGE:-postgres:16-alpine}
PGPASSWORD_DRILL=${RESTORE_PG_PASSWORD:-polsia_drill_pw}
PGUSER=${POSTGRES_USER:-polsia}
VOLUMES=${RESTORE_VOLUMES:-"backend_data chroma_data celery_beat_data redis_data"}
CORE_TABLES=${RESTORE_CORE_TABLES:-"activity_log agent_runs tasks memory_entries task_schedules chat_messages company_config"}

# ── 1. argument + artifact validation (no docker yet: dry run must not touch it) ──────────────
[ -n "$SRC" ] || refuse "--src is required — point it at a backup dir, e.g. --src backups"

if [ ! -d "$SRC" ]; then
    refuse "backup directory \"$SRC\" does not exist — run 'make backup' first, or point --src at a real backup dir"
fi

dump_count=$(ls -1 "$SRC"/polsia-*.sql 2>/dev/null | wc -l | tr -d ' ')
if [ "$dump_count" -eq 0 ]; then
    refuse "no polsia-*.sql database dump in \"$SRC\" — run 'make backup' first, or point --src at a real backup dir"
fi
# Newest dump wins when a dir holds several (backup.sh leaves one per cycle).
dump=$(ls -1t "$SRC"/polsia-*.sql 2>/dev/null | head -n 1)
if [ "$dump_count" -gt 1 ]; then
    log "note: $dump_count dumps in $SRC — using the newest: $dump"
fi
if [ ! -s "$dump" ]; then
    refuse "database dump \"$dump\" is EMPTY (0 bytes) — a restore of nothing is not a drill"
fi

format=""
if head -c 5 "$dump" | grep -q 'PGDMP'; then
    format=custom
elif head -n 3 "$dump" | grep -q 'PostgreSQL database dump'; then
    format=plain
else
    refuse "database dump \"$dump\" carries no pg_dump header (neither PGDMP magic nor a plain-SQL banner) — refusing to guess"
fi

tgz_count=$(ls -1 "$SRC"/*.tgz 2>/dev/null | wc -l | tr -d ' ')
if [ "$tgz_count" -eq 0 ]; then
    refuse "no volume archives (*.tgz) in \"$SRC\" — the drill restores the database AND its data volumes"
fi

missing=""
for vol in $VOLUMES; do
    [ -f "$SRC/$vol.tgz" ] || missing="$missing $vol.tgz"
done
if [ -n "$missing" ]; then
    refuse "volume archive(s) missing from \"$SRC\":$missing — an incomplete backup cannot pass a full restore drill"
fi

for vol in $VOLUMES; do
    # MSYS tar reads `C:/...` as a remote "host:path" spec (same quirk backup.sh documents), so
    # list from INSIDE the backup dir under the bare name.
    if ! ( cd "$SRC" && tar tzf "$vol.tgz" ) >/dev/null 2>&1; then
        refuse "volume archive \"$SRC/$vol.tgz\" is not a readable gzip tar — corrupt artifact, refusing"
    fi
done

log "artifacts OK: dump=$dump (format=$format, $(wc -c < "$dump") bytes), volumes=$VOLUMES"

# ── 2. plan ──────────────────────────────────────────────────────────────────────────────────
print_plan() {
    log "PLAN — restore drill against a THROWAWAY container, never the live compose project:"
    printf '  container : %s  (image %s, NO published ports, NO compose project, NO live volume)\n' "$NAME" "$IMAGE"
    printf '  artifacts : %s\n' "$dump"
    printf '  volumes   : %s\n' "$VOLUMES"
    printf '  commands  : docker run -d --name %s -e POSTGRES_PASSWORD=*** -e POSTGRES_USER=%s -e POSTGRES_DB=postgres %s\n' "$NAME" "$PGUSER" "$IMAGE"
    printf '              docker exec %s pg_isready -U %s -d postgres\n' "$NAME" "$PGUSER"
    printf '              docker exec %s createdb -U %s polsia\n' "$NAME" "$PGUSER"
    if [ "$format" = custom ]; then
        printf '              docker exec -i %s pg_restore -U %s -d polsia --no-owner --no-privileges < %s\n' "$NAME" "$PGUSER" "$dump"
    else
        printf '              docker exec -i %s psql -U %s -d polsia -v ON_ERROR_STOP=1 < %s\n' "$NAME" "$PGUSER" "$dump"
    fi
    printf '              docker exec %s psql -U %s -d polsia -tAc "select count(*) from public.<table>"  (per core table)\n' "$NAME" "$PGUSER"
    printf '              tar xzf <volume>.tgz into a temp dir; every listed member must materialise\n'
    printf '              docker rm -f %s   (ALWAYS, via trap — success, failure, Ctrl-C, SIGTERM)\n' "$NAME"
    printf '  guard     : refuses to run if a container named %s already exists\n' "$NAME"
}

if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY RUN — nothing is created, docker is not invoked."
    print_plan
    log "DRY RUN complete: plan only, no container, no docker call."
    exit 0
fi

print_plan

# ── 3. docker presence + name guard (before anything is created) ────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    refuse "docker is not available on PATH — the restore drill needs it; nothing was created"
fi
existing=$(docker ps -a --format '{{.Names}}' 2>/dev/null || true)
if printf '%s\n' "$existing" | grep -qx "$NAME"; then
    refuse "a container named \"$NAME\" already exists — refusing to reuse/replace it; remove it first (docker rm -f $NAME) or set RESTORE_PG_CONTAINER"
fi

# ── 4. temp dir + teardown trap ─────────────────────────────────────────────────────────────
WORK=$(mktemp -d "${TMPDIR:-/tmp}/polsia-restore-drill.XXXXXX") || refuse "cannot create a temp work dir"
STARTED=0
cleanup() {
    if [ "$STARTED" -eq 1 ]; then
        if [ "$KEEP" -eq 1 ]; then
            log "TEARDOWN SKIPPED (--keep): remove it yourself with: docker rm -f $NAME"
        else
            log "teardown: docker rm -f $NAME"
            docker rm -f "$NAME" >/dev/null 2>&1 || log "WARN: docker rm -f $NAME failed — check 'docker ps -a'"
            STARTED=0
        fi
    fi
    [ -n "${WORK:-}" ] && rm -rf "$WORK" 2>/dev/null
}
trap cleanup EXIT
trap 'log "interrupted — tearing down"; exit 130' INT TERM HUP

FAILS=0
fail() {
    log "FAIL: $*"
    FAILS=$((FAILS + 1))
}

# ── 5. start the throwaway database ─────────────────────────────────────────────────────────
log "command: docker run -d --name $NAME -e POSTGRES_PASSWORD=*** -e POSTGRES_USER=$PGUSER -e POSTGRES_DB=postgres $IMAGE"
if ! docker run -d --name "$NAME" \
        -e POSTGRES_PASSWORD="$PGPASSWORD_DRILL" \
        -e POSTGRES_USER="$PGUSER" \
        -e POSTGRES_DB=postgres \
        "$IMAGE" >"$WORK/run.log" 2>&1; then
    log "ERROR: docker run failed:"
    sed -n '1,20p' "$WORK/run.log" 2>/dev/null
    log "RESULT: FAIL (could not start the throwaway container)"
    exit 1
fi
STARTED=1
log "container $NAME started (no published ports)"

ready=0
i=0
while [ "$i" -lt 60 ]; do
    if docker exec "$NAME" pg_isready -U "$PGUSER" -d postgres >/dev/null 2>&1; then
        ready=1
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    log "ERROR: postgres did not become ready within 60s"
    docker logs --tail 20 "$NAME" 2>&1 | sed -n '1,20p'
    log "RESULT: FAIL (container never accepted connections)"
    exit 1
fi
log "postgres ready"

log "command: docker exec $NAME createdb -U $PGUSER polsia"
if ! docker exec "$NAME" createdb -U "$PGUSER" polsia >"$WORK/createdb.log" 2>&1; then
    log "ERROR: createdb failed:"
    sed -n '1,20p' "$WORK/createdb.log"
    log "RESULT: FAIL (createdb)"
    exit 1
fi

# ── 6. manifest from the dump itself ────────────────────────────────────────────────────────
# Plain-SQL dumps carry one COPY block per table; a row is one line (pg_dump escapes embedded
# newlines), so the block's line count IS the row count.  Custom dumps cannot be read this way,
# so the manifest for those is empty and verification falls back to "the restore succeeded".
manifest="$WORK/manifest.tsv"
: > "$manifest"
dump_tables=0
if [ "$format" = plain ]; then
    dump_tables=$(grep -c '^CREATE TABLE public\.' "$dump" 2>/dev/null || true)
    dump_tables=${dump_tables:-0}
    for tbl in $CORE_TABLES; do
        rows=$(awk -v t="public.$tbl" '
            { sub(/\r$/, "") }
            index($0, "COPY " t " ") == 1 { inb = 1; next }
            inb && $0 == "\\." { inb = 0; exit }
            inb { n++ }
            END { print n + 0 }
        ' "$dump")
        printf '%s\t%s\n' "$tbl" "$rows" >> "$manifest"
    done
    log "manifest from dump: $dump_tables public tables; core rows: $(tr '\n' ' ' < "$manifest")"
fi

# ── 7. load ─────────────────────────────────────────────────────────────────────────────────
if [ "$format" = custom ]; then
    log "command: docker exec -i $NAME pg_restore -U $PGUSER -d polsia --no-owner --no-privileges < $dump"
    if ! docker exec -i "$NAME" pg_restore -U "$PGUSER" -d polsia --no-owner --no-privileges < "$dump" >"$WORK/load.log" 2>&1; then
        log "ERROR: pg_restore failed (last lines):"
        tail -n 15 "$WORK/load.log"
        log "RESULT: FAIL (dump did not restore)"
        exit 1
    fi
else
    log "command: docker exec -i $NAME psql -U $PGUSER -d polsia -v ON_ERROR_STOP=1 < $dump"
    if ! docker exec -i "$NAME" psql -U "$PGUSER" -d polsia -v ON_ERROR_STOP=1 -q < "$dump" >"$WORK/load.log" 2>&1; then
        log "ERROR: psql load failed (last lines):"
        tail -n 15 "$WORK/load.log"
        log "RESULT: FAIL (dump did not restore)"
        exit 1
    fi
fi
log "dump restored into $NAME/polsia"

q() { docker exec "$NAME" psql -U "$PGUSER" -d polsia -t -A -q -c "$1" 2>/dev/null | tr -d '\r' | head -n 1; }

# ── 8. verification: table count + row counts ───────────────────────────────────────────────
if [ "$format" = plain ]; then
    actual_tables=$(q "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'")
    actual_tables=${actual_tables:-0}
    if [ "$actual_tables" = "$dump_tables" ]; then
        log "PASS tables: public base tables restored = $actual_tables (manifest $dump_tables)"
    else
        fail "tables: restored $actual_tables, manifest says $dump_tables"
    fi

    while IFS="$(printf '\t')" read -r tbl expected; do
        [ -n "$tbl" ] || continue
        actual=$(q "select count(*) from public.$tbl")
        actual=${actual:-0}
        if [ "$actual" = "$expected" ]; then
            log "PASS rows/$tbl: $actual"
        else
            fail "rows/$tbl: restored $actual, manifest says $expected"
        fi
    done < "$manifest"
else
    log "note: custom-format dump — row-count manifest is not derivable from the archive; table presence only"
    actual_tables=$(q "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'")
    [ -n "$actual_tables" ] && log "INFO tables restored = $actual_tables"
fi

# ── 9. verification: every volume member must materialise ──────────────────────────────────
for vol in $VOLUMES; do
    archive="$SRC/$vol.tgz"
    dest="$WORK/vol/$vol"
    mkdir -p "$dest" || { fail "volume/$vol: cannot create extraction dir"; continue; }
    if ! ( cd "$SRC" && tar tzf "$vol.tgz" ) > "$WORK/$vol.list" 2>/dev/null; then
        fail "volume/$vol: archive listing failed"
        continue
    fi
    members=$(wc -l < "$WORK/$vol.list" | tr -d ' ')
    if ! ( cd "$SRC" && tar xzf "$vol.tgz" -C "$dest" ) >"$WORK/$vol.extract.log" 2>&1; then
        fail "volume/$vol: extraction failed ($(tail -n 3 "$WORK/$vol.extract.log" | tr '\n' ' '))"
        continue
    fi
    present=0
    files=0
    while IFS= read -r member; do
        [ -n "$member" ] || continue
        if [ -e "$dest/$member" ] || [ -L "$dest/$member" ]; then
            present=$((present + 1))
        fi
        case "$member" in
            */|.) : ;;
            *) files=$((files + 1)) ;;
        esac
    done < "$WORK/$vol.list"
    if [ "$present" -eq "$members" ]; then
        log "PASS volume/$vol: $present/$members members materialised ($files file entries)"
    else
        fail "volume/$vol: only $present/$members members materialised"
    fi
done

# ── 10. verdict ─────────────────────────────────────────────────────────────────────────────
if [ "$FAILS" -eq 0 ]; then
    log "RESULT: PASS — the backup restores into a clean database and every volume archive materialises"
    exit 0
fi
log "RESULT: FAIL — $FAILS verification check(s) failed (see FAIL lines above)"
exit 1
