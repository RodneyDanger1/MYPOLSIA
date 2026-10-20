"""V27 / L2 — run-workspace retention: the daily beat task, the owner route, the activity row.

RED-first pin. At the V26 settle commit `d6bd944` there is no `cleanup_run_workspaces` task, no
beat entry, no route and no panel; the V26 registry function
(`app.agents.tool_registry.cleanup_expired_run_workspaces`, policy keep-newest-20 / max-age-72 h)
has NO caller anywhere in the tree.  Every test below therefore fails on the base tree for a real
missing surface (a missing attribute / a 404 / a KeyError on the beat dict), never on a syntax
error of the test itself.

What is pinned, in the brief's order
------------------------------------
1. the task is registered on the app under its FULL name and appears EXACTLY ONCE in the beat
   schedule, daily, with an `expires` below the next day's tick (backlog rule);
2. the task body calls the V26 registry function with the NAMED defaults (20 / 72.0) — the policy
   is not re-declared here, it is imported;
3. the activity row carries counts + run NAMES only — a sentinel planted INSIDE a run directory
   is asserted absent from the row (no file contents, no file paths);
4. the route's dry run reports the same counts as the applied pass and deletes NOTHING;
5. `{"apply": true}` deletes only expired run directories; recent, foreign-named, non-directory
   and outside-`runs/` entries survive;
6. the preview and the delete agree on identical trees (the anti-drift leg: one policy, two
   read paths);
7. a non-object / wrongly-typed / unknown-key body is a NAMED `E_VALIDATION`, never FastAPI's
   uncoded 422;
8. a non-object body, a bad `apply`, a bad `workspace_root` and a non-JSON body all refuse
   WITHOUT deleting anything;
9. `GET` serves the last recorded result, or an honest `{data: null}` when nothing ran yet.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from celery_app.tasks import maintenance

TASK_NAME = "celery_app.tasks.maintenance.cleanup_run_workspaces"
BEAT_KEY = "run-workspace-cleanup"
ROUTE = "/api/v1/ops/run-workspace-cleanup"
ACTION = "run_workspace_cleanup"
KEEP_LATEST = 20
MAX_AGE_HOURS = 72.0

#: Written INSIDE a run directory that the applied pass deletes.  If any byte of it reaches the
#: activity row, the row is carrying file contents — the brief's absolute.
SENTINEL = "V27-SENTINEL-CONTENT-MUST-NEVER-ENTER-ACTIVITY"


# ── fixtures / helpers ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_redis_publish(monkeypatch):
    """TEST DOUBLE (labelled): the activity publisher is a no-op AsyncMock.

    `log_activity` publishes after the flush; without this the first publish pays the refused
    Redis socket retry ladder in unit tests that are not under tests/unit/celery/ (where the
    suite's own quiet_redis fixture lives).  The DB row — what these tests assert — is real.
    """
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)


def _seed_tree(root: Path, *, recent: int = 20, old: int = 4) -> tuple[list[str], list[str]]:
    """A `<root>/runs/` tree: `recent` fresh run dirs, `old` expired ones, plus hostile entries.

    Mtimes are explicit (`os.utime`), so the age leg is deterministic against the wall clock.
    Returns `(recent_names, old_names)`.
    """
    runs = root / "runs"
    runs.mkdir(parents=True)
    now = time.time()

    recent_names: list[str] = []
    for index in range(recent):
        name = f"run-recent-{index:02d}"
        (runs / name).mkdir()
        os.utime(runs / name, (now - (index + 1) * 3600, now - (index + 1) * 3600))
        recent_names.append(name)

    old_names: list[str] = []
    for index in range(old):
        name = f"run-old-{index:02d}"
        target = runs / name
        target.mkdir()
        age_h = 100.0 + index * 10.0
        os.utime(target, (now - age_h * 3600, now - age_h * 3600))
        (target / "output.txt").write_text(f"{SENTINEL} {index}", encoding="utf-8")
        old_names.append(name)

    # A plain file and a foreign-named directory under runs/ are NEVER candidates.
    (runs / "stray.txt").write_text("not a run", encoding="utf-8")
    (runs / "not a run").mkdir()
    # A sibling of runs/ is outside the operation entirely.
    (root / "keepme.txt").write_text("outside runs", encoding="utf-8")
    return recent_names, old_names


def _all_names(root: Path) -> set[str]:
    return {p.name for p in (root / "runs").iterdir()}


# ── 1. registration + beat entry ──────────────────────────────────────────────────────────────


def test_the_task_is_registered_under_its_full_name() -> None:
    from celery_app.worker import app

    assert TASK_NAME in app.tasks, "the maintenance task must be registered on the worker app"
    assert maintenance.cleanup_run_workspaces.name == TASK_NAME


def test_the_beat_entry_appears_exactly_once_and_is_daily() -> None:
    from celery_app.beat_schedule import beat_schedule

    entry = beat_schedule[BEAT_KEY]
    assert entry["task"] == TASK_NAME
    schedule = entry["schedule"]
    assert schedule.hour == {4}, f"daily at 04:15 UTC, got hour={schedule.hour!r}"
    assert schedule.minute == {15}
    assert entry["options"]["queue"] == "maintenance"
    assert entry["options"]["expires"] == 6 * 3600, (
        "expires must stay below the next day's tick so a backlog cannot replay an old sweep"
    )
    occurrences = [key for key, value in beat_schedule.items() if value.get("task") == TASK_NAME]
    assert occurrences == [BEAT_KEY], f"scheduled {len(occurrences)} times: {occurrences}"


def test_the_beat_entry_is_loaded_by_the_real_app_conf() -> None:
    from celery_app.worker import app

    effective = dict(app.conf.beat_schedule or {})
    assert BEAT_KEY in effective, "declared-but-not-loaded is the D-15 defect class"
    assert effective[BEAT_KEY]["task"] == TASK_NAME


# ── 2. the task body: named defaults + the honest activity row ────────────────────────────────


def test_the_task_body_calls_the_registry_with_the_documented_defaults(queue_db, monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    _seed_tree(root)
    monkeypatch.setattr("app.config.settings.code_workspace_dir", str(root))

    seen: list[dict] = []

    def spy(**kwargs):
        seen.append(kwargs)
        return {
            "runs_dir": str(root / "runs"),
            "removed": ["run-old-00"],
            "count": 1,
            "kept": 23,
            "skipped": [],
            "policy": {"keep_latest": KEEP_LATEST, "max_age_hours": MAX_AGE_HOURS},
        }

    monkeypatch.setattr(maintenance, "cleanup_expired_run_workspaces", spy)

    result = queue_db.run(maintenance._cleanup_run_workspaces())

    assert seen == [
        {"workspace_root": None, "keep_latest": KEEP_LATEST, "max_age_hours": MAX_AGE_HOURS}
    ], "the task must call the V26 function with the NAMED policy, not its own numbers"
    assert result["applied"] is True
    assert result["count"] == 1
    assert result["status"] == "completed"

    row = queue_db.query("SELECT action, summary, detail FROM activity_log ORDER BY id DESC LIMIT 1")[0]
    assert row["action"] == ACTION
    detail = json.loads(row["detail"])
    assert detail["count"] == 1
    assert detail["removed"] == ["run-old-00"]
    assert detail["applied"] is True
    assert detail["trigger"] == "beat"


def test_the_task_records_counts_and_names_but_never_file_contents(queue_db, monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    monkeypatch.setattr("app.config.settings.code_workspace_dir", str(root))

    result = queue_db.run(maintenance._cleanup_run_workspaces())

    assert result["applied"] is True
    assert sorted(result["removed"]) == sorted(old), "only the expired run dirs are removed"
    assert result["count"] == len(old)
    assert result["kept"] == len(recent)
    for name in old:
        assert not (root / "runs" / name).exists(), f"{name} should be gone"
    for name in recent:
        assert (root / "runs" / name).is_dir(), f"{name} is inside the newest 20 and must survive"
    assert (root / "keepme.txt").is_file(), "a sibling of runs/ is never touched"

    row = queue_db.query("SELECT action, summary, detail FROM activity_log ORDER BY id DESC LIMIT 1")[0]
    detail = json.loads(row["detail"])
    serialized = json.dumps(detail) + row["summary"]

    assert SENTINEL not in serialized, "the activity row must never carry file contents"
    assert "output.txt" not in serialized, "the activity row must never carry file paths"
    assert detail["count"] == len(old)
    assert sorted(detail["removed"]) == sorted(old)
    assert detail["policy"] == {"keep_latest": KEEP_LATEST, "max_age_hours": MAX_AGE_HOURS}
    assert {"name": "stray.txt", "reason": "not_a_directory"} in detail["skipped"]
    assert {"name": "not a run", "reason": "invalid_name"} in detail["skipped"]


# ── 3. preview vs delete on the same policy ───────────────────────────────────────────────────


def test_dry_run_reports_counts_and_deletes_nothing(tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    before = _all_names(root)

    outcome = maintenance.run_workspace_cleanup(apply=False, workspace_root=root)

    assert outcome["applied"] is False
    assert outcome["count"] == len(old)
    assert outcome["kept"] == len(recent)
    assert sorted(outcome["removed"]) == sorted(old)
    assert outcome["policy"] == {"keep_latest": KEEP_LATEST, "max_age_hours": MAX_AGE_HOURS}
    assert Path(outcome["runs_dir"]).resolve() == (root / "runs").resolve()
    assert _all_names(root) == before, "a dry run deletes NOTHING"


def test_apply_deletes_only_expired_run_dirs(tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)

    outcome = maintenance.run_workspace_cleanup(apply=True, workspace_root=root)

    assert outcome["applied"] is True
    assert sorted(outcome["removed"]) == sorted(old)
    assert outcome["count"] == len(old)
    for name in old:
        assert not (root / "runs" / name).exists()
    for name in recent:
        assert (root / "runs" / name).is_dir()
    assert (root / "runs" / "stray.txt").is_file()
    assert (root / "runs" / "not a run").is_dir()
    assert (root / "keepme.txt").is_file()
    assert (root / "runs").is_dir(), "the shared runs/ directory itself is never removed"


def test_the_preview_and_the_delete_agree_on_identical_trees(tmp_path):
    """The anti-drift leg: one policy, read two ways.

    Two trees are seeded identically (same names, same mtimes).  The preview on A must name
    exactly the directories the applied pass on B removes — otherwise the panel's "would remove"
    count is a second, drifting policy.
    """
    now = time.time()
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    for tree in (tree_a, tree_b):
        _seed_tree(tree)
        for entry in (tree / "runs").iterdir():
            os.utime(entry, (now - 100 * 3600, now - 100 * 3600))

    planned = maintenance.run_workspace_cleanup(apply=False, workspace_root=tree_a)
    applied = maintenance.run_workspace_cleanup(apply=True, workspace_root=tree_b)

    assert sorted(planned["removed"]) == sorted(applied["removed"])
    assert planned["count"] == applied["count"]
    assert planned["kept"] == applied["kept"]
    assert planned["skipped"] == applied["skipped"]


# ── 4. the owner route ───────────────────────────────────────────────────────────────────────


async def test_route_dry_run_reports_counts_and_deletes_nothing(api_client, auth_headers, tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    before = _all_names(root)

    resp = await api_client.post(ROUTE, json={"workspace_root": str(root)}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"runs_dir", "count", "kept", "removed", "skipped", "policy", "applied"}
    assert body["applied"] is False
    assert body["count"] == len(old)
    assert body["kept"] == len(recent)
    assert sorted(body["removed"]) == sorted(old)
    assert body["policy"] == {"keep_latest": KEEP_LATEST, "max_age_hours": MAX_AGE_HOURS}
    assert Path(body["runs_dir"]).resolve() == (root / "runs").resolve()
    assert _all_names(root) == before, "the route's dry run must delete nothing"


async def test_route_apply_deletes_only_the_expired_dirs(api_client, auth_headers, tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)

    resp = await api_client.post(
        ROUTE, json={"apply": True, "workspace_root": str(root)}, headers=auth_headers
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert sorted(body["removed"]) == sorted(old)
    for name in old:
        assert not (root / "runs" / name).exists()
    for name in recent:
        assert (root / "runs" / name).is_dir()
    assert (root / "keepme.txt").is_file()


@pytest.mark.parametrize(
    "payload",
    [
        [],  # a JSON array is not a body object
        "apply",  # a JSON string
        7,  # a JSON number
        True,  # a JSON boolean
        {"apply": "yes"},  # a bool is not coerced from a string
        {"apply": 1},  # ... nor from an int
        {"workspace_root": 5},  # the seam is a path string or absent
        {"workspace_root": ""},
        {"apply": False, "surprise": True},  # unknown keys are refused, not ignored
    ],
)
async def test_route_refuses_a_bad_body_by_name(api_client, auth_headers, tmp_path, payload):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    before = _all_names(root)

    resp = await api_client.post(
        ROUTE, json=payload, headers=auth_headers
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert _all_names(root) == before, "a refused body deletes nothing"


async def test_route_refuses_a_non_json_body_by_name(api_client, auth_headers, tmp_path):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    before = _all_names(root)

    resp = await api_client.post(
        ROUTE,
        content=b"{not json",
        headers={**auth_headers, "Content-Type": "application/json"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "E_VALIDATION"
    assert _all_names(root) == before


async def test_the_body_is_optional_and_defaults_to_a_dry_run(api_client, auth_headers, tmp_path):
    """No body at all is still a dry run — the safe default must not need a payload to be safe."""
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)
    import app.config as config_module

    original = config_module.settings.code_workspace_dir
    config_module.settings.code_workspace_dir = str(root)
    try:
        resp = await api_client.post(ROUTE, headers=auth_headers)
    finally:
        config_module.settings.code_workspace_dir = original

    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False
    assert resp.json()["count"] == len(old)
    assert (root / "runs" / old[0]).is_dir()


async def test_route_requires_the_owner_key(api_client):
    resp = await api_client.post(ROUTE, json={})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


async def test_route_records_the_result_and_the_last_read_serves_it(
    api_client, auth_headers, tmp_path
):
    root = tmp_path / "workspace"
    recent, old = _seed_tree(root)

    first = await api_client.post(
        ROUTE, json={"workspace_root": str(root)}, headers=auth_headers
    )
    assert first.status_code == 200, first.text

    last = await api_client.get(ROUTE, headers=auth_headers)
    assert last.status_code == 200, last.text
    body = last.json()
    assert body["data"] is not None
    assert body["data"]["trigger"] == "owner"
    assert body["data"]["result"]["count"] == len(old)
    assert body["data"]["result"]["applied"] is False
    assert isinstance(body["data"]["activity_id"], int)
    assert body["data"]["created_at"]

    applied = await api_client.post(
        ROUTE, json={"apply": True, "workspace_root": str(root)}, headers=auth_headers
    )
    assert applied.status_code == 200, applied.text
    last = await api_client.get(ROUTE, headers=auth_headers)
    assert last.json()["data"]["result"]["applied"] is True, "the newest row wins"


async def test_the_last_read_is_an_honest_null_before_any_cleanup(api_client, auth_headers):
    resp = await api_client.get(ROUTE, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"data": None}
