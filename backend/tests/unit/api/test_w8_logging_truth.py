"""WAVE 8 / logic-debugger — FAILURE MATRIX #3, GROUP E: LOGGING / DEBUGGING TRUTH.

Two rules, both of them "a record must say what happened":

  1. A REFUSAL THAT NAMES ITSELF IS OBSERVABLE.  Every layer that turns a failure into a wire
     refusal must also leave a server-side record naming the code — a caller who reports "it
     failed" and an operator who reads the logs must be able to meet.
  2. A FAILURE IS NEVER SWALLOWED.  No bare `except:`, no `except Exception: pass`, and no debug
     `print()` in the product tree — a silent catch converts a visible failure into a corrupt
     record (AGENTS.md §1).

Rows:
  E1  a route refusal logs WARNING with its code (the `PolsiaError` handler);
  E2  a PRE-route refusal logs with the correlation id (the middleware branch);
  E3  a run whose agent failed records the named code ON THE LEDGER (task.error_message /
      agent_runs.raw_log) and one `task_failed` activity row — the refusal is not only in a log
      line that rotates away;
  E4  a duplicate delivery that did nothing is observable at INFO, naming the skip code;
  E5  no silent exception handler in this seat's three product files (AST scan);
  E6  no `print()` anywhere in `app/` or `celery_app/` (AST scan);
  E7  a refused credential never appears in the response body OR in any captured log record;
  E8  a provider adapter's RETURNED refusal (not raised) still names code + message + fix — a
      dict-returning refusal is as observable as a raised one.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

from app.core.errors import E_DB, E_INTEGRATION, E_NOT_FOUND, PolsiaError

BACKEND_ROOT = Path(__file__).resolve().parents[3]
TERRITORY_FILES = (
    BACKEND_ROOT / "app" / "services" / "task_service.py",
    BACKEND_ROOT / "app" / "services" / "chat_service.py",
    BACKEND_ROOT / "celery_app" / "tasks" / "agent_tasks.py",
)


# ── E1 / E2: the wire refusals are logged ───────────────────────────────────────────────────
async def test_e01_a_route_refusal_logs_its_code(api_client, auth_headers, caplog):
    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = await api_client.get("/api/v1/tasks/999999", headers=auth_headers)
    assert resp.status_code == 404
    records = [
        record.getMessage() for record in caplog.records if record.name.startswith("app.main")
    ]
    assert any(E_NOT_FOUND in line and "/api/v1/tasks/999999" in line for line in records), (
        f"the refusal must be observable on the server side, with the code: {records}"
    )


async def test_e02_a_pre_route_refusal_logs_its_code_and_answers_with_an_id(
    api_client, auth_headers, caplog, monkeypatch
):
    from app.core.database import get_db
    from app.main import app as application

    def _boom():  # noqa: ANN202 - a FastAPI dependency
        raise PolsiaError(E_DB, "planted: the database is unreachable")

    monkeypatch.setitem(application.dependency_overrides, get_db, _boom)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = await api_client.get("/api/v1/tasks", headers=auth_headers)
    assert resp.status_code == 503
    assert resp.headers.get("x-request-id"), "the caller is given a correlation id"
    records = [record.getMessage() for record in caplog.records]
    assert any(E_DB in line for line in records), (
        f"the refusal must be observable server-side with its code: {records}"
    )


async def test_e02b_the_refusals_log_line_carries_the_same_request_id(
    api_client, auth_headers, caplog, monkeypatch
):
    """W8-LD-05, CLOSED in wave 9 (the strict xfail was removed when this row went green).

    The defect: the correlation id is minted in the `correlation_and_timing` middleware, but every
    route/dependency `PolsiaError` is answered by the exception handler INSIDE that middleware — so
    the log line an operator got for a refusal carried NO request id, and the middleware branch
    that would have added it was unreachable for those refusals.  A client quoting its
    `X-Request-ID` and the server log could not be matched, which is the one thing the id is for.

    The fix passes `request_id_from_scope(request.scope)` (the memoized, same-on-every-path rule)
    into every refusal log line.  Both wire classes are driven here: the handler's own route
    refusal (a failing `get_db` dependency) and the framework's 404 route miss.
    """
    from app.core.database import get_db
    from app.main import app as application

    def _boom():  # noqa: ANN202 - a FastAPI dependency
        raise PolsiaError(E_DB, "planted: the database is unreachable")

    monkeypatch.setitem(application.dependency_overrides, get_db, _boom)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = await api_client.get("/api/v1/tasks", headers=auth_headers)
    request_id = resp.headers["x-request-id"]
    records = [record.getMessage() for record in caplog.records]
    assert any(E_DB in line and request_id in line for line in records), (
        f"the operator cannot tie this refusal to the caller's id: {records}"
    )

    # the SAME id rule on the framework-refusal path (a different handler, one id)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        miss = await api_client.get("/api/v1/definitely-not-a-route", headers=auth_headers)
    miss_id = miss.headers["x-request-id"]
    miss_records = [record.getMessage() for record in caplog.records]
    assert any(E_NOT_FOUND in line and miss_id in line for line in miss_records), (
        f"a framework refusal must be correlatable too: {miss_records}"
    )
    assert miss_id != "", "the response carries an id even when the request minted one"


# ── E3: the named code reaches the LEDGER, not only the log ────────────────────────────────
def test_e03_a_failed_run_records_the_named_code_on_the_ledger(queue_db, install_run_spy):
    install_run_spy(
        delegate=False,
        error_factory=lambda: PolsiaError(
            E_INTEGRATION, "planted: the provider refused the request"
        ),
    )
    task_id = queue_db.create_task(title="ledger records the refusal")

    from celery_app.tasks.agent_tasks import _execute_agent_task

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "failed"
    row = queue_db.row(task_id)
    assert E_INTEGRATION in (row["error_message"] or ""), (
        "the task row's own error must name the code: a fleet reader never opens the worker log"
    )
    runs = queue_db.query("SELECT status, raw_log FROM agent_runs ORDER BY id DESC LIMIT 1")
    assert runs and runs[0]["status"] == "failed"
    assert E_INTEGRATION in (runs[0]["raw_log"] or ""), "the run ledger names it too"
    actions = [r["action"] for r in queue_db.query("SELECT action FROM activity_log ORDER BY id")]
    assert "task_failed" in actions, "and the feed carries one failure row for the owner"
    failed_rows = queue_db.query(
        "SELECT summary, level FROM activity_log WHERE action = 'task_failed'"
    )
    assert failed_rows and failed_rows[0]["level"] == "error"
    assert E_INTEGRATION in (failed_rows[0]["summary"] or "")


# ── E4: a delivery that did nothing is observable ──────────────────────────────────────────
def test_e04_a_duplicate_delivery_skip_is_observable_at_info(queue_db, install_run_spy, execute_body, caplog):
    install_run_spy()
    task_id = queue_db.create_task(title="observable skip")
    execute_body(task_id)  # first delivery settles it

    with caplog.at_level(logging.INFO):
        outcome = execute_body(task_id)

    assert outcome["status"] == "skipped"
    assert any(
        "skipped" in record.getMessage() and outcome["reason"] in record.getMessage()
        for record in caplog.records
    ), (
        "a delivery that deliberately did nothing must say so (with the code), or a missing run "
        f"is indistinguishable from a lost one: {[r.getMessage() for r in caplog.records]}"
    )


# ── E5: no silent exception handler in the territory ──────────────────────────────────────
def test_e05_no_silent_exception_handler_in_this_seats_files():
    offenders: list[str] = []
    for path in TERRITORY_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            silent = all(
                isinstance(statement, ast.Pass | ast.Continue | ast.Break)
                for statement in body
            )
            if not silent:
                continue
            caught = ast.unparse(node.type) if node.type is not None else "<bare>"
            if node.type is None or caught in ("Exception", "BaseException"):
                offenders.append(f"{path.name}:{node.lineno} except {caught}")
    assert not offenders, (
        "a broad catch with an empty body hides a real failure: name it, log it, or delete the "
        f"handler — {offenders}"
    )


# ── E6: no debug output in the product tree ───────────────────────────────────────────────
def test_e06_no_print_calls_in_the_product_tree():
    offenders: list[str] = []
    for root in (BACKEND_ROOT / "app", BACKEND_ROOT / "celery_app"):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert not offenders, f"leftover debug output: {offenders}"


# ── E7: a refused credential never leaks into the record ──────────────────────────────────
async def test_e07_a_presented_key_never_appears_in_the_response_or_the_logs(
    api_client, caplog
):
    planted = "sk-planted-secret-for-the-matrix-9f3a"
    with caplog.at_level(logging.DEBUG):
        resp = await api_client.get("/api/v1/tasks", headers={"X-API-Key": planted})
    assert resp.status_code == 401
    assert planted not in resp.text, "a refusal must not echo the credential it refused"
    for record in caplog.records:
        assert planted not in record.getMessage(), (
            f"the credential reached the server log at {record.name}:{record.lineno}"
        )


# ── E8: a RETURNED refusal is still a named refusal ───────────────────────────────────────
def test_e08_a_provider_adapters_returned_refusal_carries_code_message_and_fix(monkeypatch):
    """`integrations.send_email` RETURNS a refusal dict instead of raising (a provider config
    gap is a value, not an exception).  It must still name itself completely — the caller records
    it in `refusals[]`, and a bare `{"ok": False}` would be an unreadable row."""
    from app.agents import integrations

    monkeypatch.setattr(integrations, "_sendgrid_credentials", lambda: ("", ""))
    refusal = integrations.send_email(to="a@example.com", subject="s", body="b")

    assert refusal["ok"] is False
    error = refusal["error"]
    assert error["code"] == E_INTEGRATION
    assert error["message"].strip() and "SENDGRID_API_KEY" in error["message"]
    assert error["how_to_fix"].strip()
    assert isinstance(error["context"], dict) and error["context"]["missing"]
