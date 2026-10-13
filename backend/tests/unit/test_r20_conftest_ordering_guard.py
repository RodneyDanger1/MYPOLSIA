"""R20 item 3 — pins the ordering guard's wiring (mechanism: tests/unit/conftest.py docstring).

The behavioral proof is the mixed FILE-argument battery itself (R20-minors receipt:
raw/repro_mixed_order_before.txt = 34 passed + 8 fixture errors; raw/repro_mixed_order_after.txt
= green) — re-running a nested pytest inside the suite would double the gate's cost for the
same signal.  What lives here are the cheap invariants that keep a future edit from silently
un-doing the guard:

* the parent conftest must re-expose the SAME fixture objects as the celery conftest;
* the celery-scope predicate must match only tests/unit/celery/;
* both `quiet_redis` copies must stay autouse, and the guarded parent copy must patch the
  publish seam only for celery paths.
"""
from __future__ import annotations

from types import SimpleNamespace

from tests.unit import conftest as unit_conftest
from tests.unit.celery import conftest as celery_conftest

SHARED_FIXTURE_NAMES = (
    "queue_db",
    "install_run_spy",
    "execute_body",
    "eager_delivery",
    "service",
    "worker_fn",
    "steal_claim",
)


def test_parent_conftest_reexposes_the_same_fixture_objects():
    """Re-export, not copy: if the celery fixture is renamed/replaced, this fails loudly."""
    for name in SHARED_FIXTURE_NAMES:
        assert getattr(unit_conftest, name) is getattr(celery_conftest, name), name


def test_celery_scope_predicate_matches_only_the_celery_dir():
    assert unit_conftest._is_under_celery_dir(unit_conftest.CELERY_DIR / "test_agent_tasks.py") is True
    assert unit_conftest._is_under_celery_dir(unit_conftest.CELERY_DIR.parent / "test_policy_firewall.py") is False
    assert unit_conftest._is_under_celery_dir(unit_conftest.CELERY_DIR.parent.parent / "conftest.py") is False


def test_both_quiet_redis_copies_are_autouse():
    for module in (unit_conftest, celery_conftest):
        marker = module.quiet_redis._fixture_function_marker
        assert marker.autouse is True, module.__name__


def test_guarded_quiet_redis_patches_only_for_celery_paths():
    """Behavioral check of the guard: inert outside, real mock inside — no fixture machinery."""
    from _pytest.monkeypatch import MonkeyPatch

    import app.core.events as events_module

    fn = unit_conftest.quiet_redis._fixture_function
    mp = MonkeyPatch()
    try:
        outside = SimpleNamespace(path=unit_conftest.CELERY_DIR.parent / "test_other.py")
        assert fn(outside, mp) is None

        celery_request = SimpleNamespace(path=unit_conftest.CELERY_DIR / "test_x.py")
        mock = fn(celery_request, mp)
        assert mock is not None and mock.publish is not None
        assert events_module.get_redis() is mock  # the publish seam is mocked for celery paths
    finally:
        mp.undo()
