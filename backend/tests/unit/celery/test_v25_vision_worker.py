"""V25/L1 — the WORKER half of vision input: the attachment refs reach the agent's task dict.

The unit suite (`tests/unit/test_v25_vision.py`) proves the storage, the routes and the runtime;
this file proves the one line between them that no route test can see — `run_agent_task` reads the
claimed task's attachment rows and hands them to `crew_factory.run_agent_for_task` as
`task_dict["attachments"]`.  Without that seam an uploaded image would never reach a run, which is
precisely the "produced but never wired" defect class this repo's receipts keep naming.

Harness: the celery directory's file-backed `queue_db` + `execute_body` (the real async body) and
a recorder installed at the SAME seam the worker imports (`crew_factory.run_agent_for_task`, a
function-local import at call time).  No provider is called; the recorder returns a summary.
"""
from __future__ import annotations

import base64

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _add_attachment(queue_db, task_id: int, filename: str = "shot.png") -> None:
    from app.services import task_attachment_service as svc

    async def _write() -> None:
        async with queue_db.factory() as db:
            await svc.save_attachment(db, task_id=task_id, filename=filename, data=PNG_1PX)
            await db.commit()

    queue_db.run(_write())


def test_an_attached_image_reaches_the_agent_task_dict(
    queue_db, execute_body, monkeypatch, tmp_path
):
    from app.config import settings

    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"), raising=False)
    task_id = queue_db.create_task(title="Read the screenshot", agent_type="finance")
    _add_attachment(queue_db, task_id, "screen shot.png")

    captured: dict = {}

    def delegate(agent_type, task, context):  # noqa: ANN001 - the frozen crew_factory signature
        captured.update(task)
        return {"summary": f"captured {agent_type}"}

    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "run_agent_for_task", delegate)

    result = execute_body(task_id)

    assert result.get("status") == "completed", result
    refs = captured.get("attachments")
    assert isinstance(refs, list) and len(refs) == 1, captured
    assert refs[0]["filename"] == "screen shot.png"
    assert refs[0]["mime"] == "image/png"
    assert refs[0]["size_bytes"] == len(PNG_1PX)
    # The bytes are NOT copied into the payload — only the locator the run thread reads.
    assert "data_uri" not in refs[0] and refs[0]["stored_name"].endswith(".png")
    assert captured["title"] == "Read the screenshot", "the run's title rides the same dict"


def test_a_task_with_no_images_carries_an_empty_list_not_a_missing_key(
    queue_db, execute_body, monkeypatch, tmp_path
):
    """`[]` is a measured zero: the agent knows the task HAS no images.  A missing key would make
    \"no images\" indistinguishable from \"the worker forgot to look\"."""
    from app.config import settings

    monkeypatch.setattr(settings, "attachments_dir", str(tmp_path / "attachments"), raising=False)
    task_id = queue_db.create_task(title="No images here", agent_type="finance")

    captured: dict = {}

    def delegate(agent_type, task, context):  # noqa: ANN001
        captured.update(task)
        return {"summary": "ok"}

    import app.agents.crew_factory as crew_factory

    monkeypatch.setattr(crew_factory, "run_agent_for_task", delegate)

    result = execute_body(task_id)

    assert result.get("status") == "completed", result
    assert captured.get("attachments") == []
