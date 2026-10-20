"""V26 / L4 — vision on demand: `POST /api/v1/vision/describe` (F4).

RED-first at base `298bb4a`: `app.api.v1.vision`, `app.services.vision_service` and
`app.schemas.vision` do not exist, so this module fails at import (`ModuleNotFoundError`) and
no route is registered — the router is also mounted by THIS test in a minimal FastAPI app, so
the shipped app's include list (owned by L2) is not needed to exercise it.

What is pinned here:

  A. HAPPY PATH — a real 96x96 flat-colour PNG generated with stdlib `zlib`/`struct` (test
     fixture, not product code) is described: the response carries the model's own sentence,
     the served model id and a MEASURED token total.  The bytes handed to
     `deepseek_runtime.invoke(images=...)` are asserted to be exactly the attachment's bytes,
     so "red" and "blue" are decided by the image actually sent, not by the mock's mood.
  B. RESOLUTION — `attachment_id` describes that attachment; `task_id` resolves a task's
     SINGLE image; 0 images is `E_NOT_FOUND`; >1 image requires `attachment_id`
     (`E_VALIDATION`, message names the count).
  C. REFUSALS — unknown attachment/task and a vanished blob are `E_NOT_FOUND`; a non-image row
     and a malformed/ambiguous body are `E_VALIDATION`; no DeepSeek key is `E_CONFIG` naming
     `DEEPSEEK_API_KEY`; a provider failure is `E_UPSTREAM` carrying the provider's status.
     None of them may leak the key.
  D. HONESTY — the description is never persisted: `Task.result_summary` and the attachment
     row are byte-for-byte unchanged after a describe.  An unmeasured `usage` yields
     `tokens: null` (never a fabricated 0), and an empty model sentence is `E_UPSTREAM`, not a
     fake success.

The model call is a LABELLED HTTP DOUBLE (`_fake_invoke`) that implements the real runtime's
`invoke` signature and returns the real `claude_cli.InvokeResult` envelope shape; it is a test
double only and no product code branches on it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

HEADERS = {"X-API-Key": "test-key"}
MODEL = "deepseek-flash"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# fixtures: a REAL PNG (stdlib only) and the attachment store
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A real, valid flat-colour PNG — built with stdlib zlib/struct (TEST FIXTURE only)."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


RED_PNG = _png(96, 96, (255, 0, 0))
BLUE_PNG = _png(96, 96, (0, 0, 255))


@pytest.fixture
def attachments_root(tmp_path, monkeypatch):
    """Every test's blobs live in its own tmp dir — the shipped data dir is never touched."""
    from app.config import settings

    root = tmp_path / "attachments"
    monkeypatch.setattr(settings, "attachments_dir", str(root), raising=False)
    return root


@pytest.fixture
def configured_key(monkeypatch):
    """The key seam: a fake key.  Never a real one; never printed."""
    from app.services import secret_store

    monkeypatch.setattr(secret_store, "get_deepseek_api_key", lambda: "fake-deepseek-key-v26")
    return "fake-deepseek-key-v26"


@pytest_asyncio.fixture
async def vision_client(async_db_session, attachments_root, monkeypatch):
    """The router mounted in a MINIMAL app (main.py's include list is L2-owned).

    The two exception handlers mirror the shipped app's refusal bridge so the assertions read
    the REAL wire envelope (`{"ok": false, "error": {code, message, how_to_fix, context}}`,
    `X-Polsia-Error-Code`) rather than FastAPI's default `{"detail": ...}`: `PolsiaError` ->
    `errors.to_http_detail` + `errors.http_status_for` (main.py:335-343), and a coded
    `HTTPException` (what `refusal_guard` raises) -> the same envelope at its own status
    (main.py:345-374).  Importing `app.main` here would drag every other lane's router into
    this test, so the bridge is rebuilt from the canonical helpers instead.
    """
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.api.v1 import vision
    from app.config import settings
    from app.core.database import get_db
    from app.core.errors import PolsiaError, http_status_for, to_http_detail

    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    app = FastAPI()
    app.include_router(vision.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: async_db_session

    @app.exception_handler(PolsiaError)
    async def _polsia_error(_request, exc: PolsiaError) -> JSONResponse:
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _coded_http(_request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"]:
            envelope = {
                "ok": False,
                "error": {
                    "code": detail["code"],
                    "message": detail.get("message") or "",
                    "how_to_fix": detail.get("how_to_fix") or "",
                    "context": detail.get("context") if isinstance(detail.get("context"), dict) else {},
                },
            }
            headers = dict(exc.headers or {})
            headers.setdefault("X-Polsia-Error-Code", detail["code"])
            return JSONResponse(status_code=exc.status_code, content=envelope, headers=headers)
        return JSONResponse(status_code=exc.status_code, content={"detail": detail},
                            headers=dict(exc.headers or {}))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed_task(db, *, title: str = "Read the screenshot", result_summary: str | None = None):
    from app.models.task import Task

    task = Task(
        title=title,
        agent_type="finance",
        status="pending",
        priority=3,
        result_summary=result_summary,
    )
    db.add(task)
    await db.commit()
    return task


async def _attach(db, task_id: int, data: bytes = RED_PNG, filename: str = "shot.png"):
    from app.services import task_attachment_service as svc

    row = await svc.save_attachment(db, task_id=task_id, filename=filename, data=data)
    await db.commit()
    return row


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the LABELLED model double: real `invoke` signature, real envelope shape
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _fake_invoke(
    *,
    description: str = "The image is a solid red square.",
    usage: dict | None = None,
    is_error: bool = False,
    provider_status: int | None = None,
    served_model: str = MODEL,
):
    """Returns (fake, calls).  `fake` implements `deepseek_runtime.invoke`'s signature."""
    from app.agents import claude_cli

    calls: list[dict] = []

    def fake(
        inv,
        *,
        client=None,
        api_key=None,
        model=None,
        tools=None,
        agent_type=None,
        task_title=None,
        images=None,
    ):
        calls.append(
            {
                "inv": inv,
                "api_key": api_key,
                "model": model,
                "tools": tools,
                "agent_type": agent_type,
                "task_title": task_title,
                "images": images,
            }
        )
        envelope = {
            "type": "result",
            "is_error": is_error,
            "result": description,
            "model": served_model,
            "usage": {} if usage is None else usage,
        }
        if is_error:
            envelope.update(
                {"subtype": "error_api_error", "api_error_status": provider_status}
            )
        return claude_cli.InvokeResult(
            stdout=json.dumps(envelope),
            stderr="",
            exit_code=1 if is_error else 0,
            wall_seconds=0.01,
            transport="http",
        )

    return fake, calls


def _prompt_text(inv) -> str:
    """The prompt the service recovered into the Invocation (stdin, exactly as documented)."""
    from app.agents.deepseek_runtime import plan_from_invocation

    return plan_from_invocation(inv).prompt


# ─────────────────────────────────────────────────────────────────────────────────────────────
# A. happy path
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_describe_a_red_png_returns_the_model_sentence_model_and_measured_tokens(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG, "red.png")
    fake, calls = _fake_invoke(
        description="The image is a solid red square.",
        usage={"input_tokens": 120, "output_tokens": 18},
    )
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # V27/L4 re-pin (deliberate, additive): the describe answer gained `cached` — old:
    # {"description","model","tokens"} -> new: plus "cached" (False = this request measured it).
    assert set(body) == {"description", "model", "tokens", "cached"}, body
    assert body["description"] == "The image is a solid red square."
    assert body["model"] == MODEL
    assert body["tokens"] == 138, "tokens must be the measured prompt+completion total"
    assert configured_key not in response.text, "the key must never reach a response body"

    assert len(calls) == 1
    call = calls[0]
    assert call["api_key"] == configured_key
    assert call["model"] == MODEL
    assert call["tools"] is None, "a describe is one plain completion, never a tool run"
    assert len(call["images"]) == 1
    assert call["images"][0]["mime"] == "image/png"
    prefix = "data:image/png;base64,"
    assert call["images"][0]["data_uri"].startswith(prefix)
    sent = base64.b64decode(call["images"][0]["data_uri"][len(prefix):])
    assert sent == RED_PNG, "the model must receive the attachment's exact bytes"
    # The payload must pass the REAL runtime's own `images=` validation — this is the seam the
    # live call uses, so a shape the double tolerates but the runtime refuses would go RED here
    # rather than in the parent's live drill.
    from app.agents.deepseek_runtime import _image_parts as runtime_image_parts

    wire_parts = runtime_image_parts(call["images"])
    assert wire_parts[0]["type"] == "image_url"
    assert wire_parts[0]["image_url"]["url"] == call["images"][0]["data_uri"]
    assert "Describe this image factually" in _prompt_text(call["inv"])


@pytest.mark.asyncio
async def test_the_route_end_to_end_through_the_real_deepseek_runtime(
    vision_client, async_db_session, configured_key, monkeypatch
):
    """The REAL `deepseek_runtime.invoke` (only its HTTP transport is a double).

    This is the strongest local evidence that the API shape is real: the route builds the
    Invocation, the service calls the shipped runtime, the runtime validates the image parts,
    composes the OpenAI-compatible body, parses the completion and measures usage.  Only the
    network hop is a `httpx.MockTransport` provider double (labelled as such).
    """
    from functools import partial

    import httpx

    from app.agents import deepseek_runtime
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, BLUE_PNG, "blue.png")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [
                    {
                        "message": {"content": "The image is a solid blue square."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 90,
                    "completion_tokens": 12,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 90,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        vision_service, "invoke", partial(deepseek_runtime.invoke, client=client)
    )

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 200, response.text
    # V27/L4 re-pin (deliberate, additive): the answer gained `cached`; old dict
    # {description,model,tokens} -> new dict plus "cached": False (this call measured it).
    assert response.json() == {
        "description": "The image is a solid blue square.",
        "model": "deepseek-flash",
        "tokens": 102,
        "cached": False,
    }
    body = seen["body"]
    assert body["model"] == "deepseek-flash"
    content = body["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert "Describe this image factually" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == BLUE_PNG
    assert seen["auth"] == "Bearer fake-deepseek-key-v26"


@pytest.mark.asyncio
async def test_the_description_follows_the_bytes_actually_sent_blue_png(
    vision_client, async_db_session, configured_key, monkeypatch
):
    """The double answers based on the sha256 of the image it was handed — blue in => blue out."""
    from app.agents import claude_cli
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    blue = await _attach(async_db_session, task.id, BLUE_PNG, "blue.png")
    seen: list[str] = []

    def colour_aware(inv, *, images=None, **kwargs):
        images = images or []
        assert images, "no image reached the model"
        payload = base64.b64decode(images[0]["data_uri"].split(",", 1)[1])
        digest = hashlib.sha256(payload).digest()
        seen.append(digest.hex())
        description = (
            "The image is a solid blue square."
            if digest == hashlib.sha256(BLUE_PNG).digest()
            else "The image is a solid red square."
        )
        return claude_cli.InvokeResult(
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": description,
                    "model": MODEL,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
            stderr="",
            exit_code=0,
            wall_seconds=0.01,
            transport="http",
        )

    monkeypatch.setattr(vision_service, "invoke", colour_aware)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": blue.id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["description"] == "The image is a solid blue square."
    assert seen == [hashlib.sha256(BLUE_PNG).hexdigest()]


@pytest.mark.asyncio
async def test_describe_by_task_id_resolves_the_single_image(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    fake, calls = _fake_invoke(description="A red square.")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"task_id": task.id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["description"] == "A red square."
    assert len(calls) == 1
    assert calls[0]["images"][0]["mime"] == "image/png"
    assert base64.b64decode(calls[0]["images"][0]["data_uri"].split(",", 1)[1]) == RED_PNG
    assert row.id  # the attachment exists and was the one described


# ─────────────────────────────────────────────────────────────────────────────────────────────
# B. resolution refusals
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_images_on_a_task_require_attachment_id(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    await _attach(async_db_session, task.id, RED_PNG, "one.png")
    await _attach(async_db_session, task.id, BLUE_PNG, "two.png")
    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"task_id": task.id}
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert "2" in error["message"] and "attachment_id" in error["message"]
    assert response.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"
    assert calls == [], "an ambiguous task must not reach the model at all"


@pytest.mark.asyncio
async def test_a_task_with_no_images_is_named_not_found(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"task_id": task.id}
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "E_NOT_FOUND"
    assert error["context"]["task_id"] == task.id
    assert calls == []


@pytest.mark.asyncio
async def test_an_unknown_attachment_is_named_not_found(vision_client, configured_key, monkeypatch):
    from app.services import vision_service

    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": 424242}
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "E_NOT_FOUND"
    assert response.json()["error"]["context"]["attachment_id"] == 424242
    assert calls == []


@pytest.mark.asyncio
async def test_an_unknown_task_is_named_not_found(vision_client, configured_key, monkeypatch):
    from app.services import vision_service

    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"task_id": 424242}
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "E_NOT_FOUND"
    assert "424242" in error["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_a_blob_missing_from_the_data_volume_is_named_not_found(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import task_attachment_service as svc
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    svc.attachment_path(row).unlink()
    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "E_NOT_FOUND"
    assert error["context"]["attachment_id"] == row.id
    assert calls == []


# ─────────────────────────────────────────────────────────────────────────────────────────────
# C. body / config / upstream refusals
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"attachment_id": 1, "task_id": 1},
        {"attachment_id": True},          # bool is not an id
        {"attachment_id": 1.5},           # not an integer
        {"attachment_id": "1"},           # string, not an integer
        {"attachment_id": 1, "colour": "red"},  # unknown field
        {"attachment_id": None, "task_id": None},
    ],
)
async def test_a_malformed_or_ambiguous_body_is_named_validation(
    vision_client, async_db_session, configured_key, monkeypatch, body
):
    from app.services import vision_service

    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post("/api/v1/vision/describe", headers=HEADERS, json=body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "E_VALIDATION"
    assert response.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"
    assert calls == []


@pytest.mark.asyncio
async def test_a_non_json_body_is_named_validation(vision_client, configured_key):
    response = await vision_client.post(
        "/api/v1/vision/describe",
        headers={**HEADERS, "Content-Type": "application/json"},
        content=b"not json",
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "E_VALIDATION"


@pytest.mark.asyncio
async def test_a_non_image_attachment_row_is_refused_by_name(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.models.task import TaskAttachment
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = TaskAttachment(
        task_id=task.id,
        filename="notes.png",
        stored_name="a" * 32 + ".png",
        mime="text/plain",
        size_bytes=12,
        sha256="0" * 64,
    )
    async_db_session.add(row)
    await async_db_session.commit()
    fake, calls = _fake_invoke()
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "E_VALIDATION"
    assert calls == []


@pytest.mark.asyncio
async def test_no_deepseek_key_is_named_e_config_naming_the_env_var(
    vision_client, async_db_session, monkeypatch
):
    from app.services import secret_store, vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    monkeypatch.setattr(secret_store, "get_deepseek_api_key", lambda: None)

    def must_not_call(*args, **kwargs):  # pragma: no cover - asserted never reached
        raise AssertionError("invoke must not be called without a key")

    monkeypatch.setattr(vision_service, "invoke", must_not_call)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "E_CONFIG"
    assert "DEEPSEEK_API_KEY" in error["message"]
    assert response.headers.get("X-Polsia-Error-Code") == "E_CONFIG"


@pytest.mark.asyncio
async def test_a_provider_failure_is_named_e_upstream_with_the_provider_status(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    fake, _calls = _fake_invoke(is_error=True, provider_status=500, description="upstream boom")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "E_UPSTREAM"
    assert "500" in error["message"]
    assert error["context"]["provider_status"] == 500
    assert configured_key not in response.text


@pytest.mark.asyncio
async def test_an_empty_model_sentence_is_upstream_not_a_fake_success(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    fake, _calls = _fake_invoke(description="   \n")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "E_UPSTREAM"


@pytest.mark.asyncio
async def test_without_the_owner_key_the_route_is_401(vision_client, configured_key):
    response = await vision_client.post("/api/v1/vision/describe", json={"attachment_id": 1})

    assert response.status_code == 401, response.text


# ─────────────────────────────────────────────────────────────────────────────────────────────
# D. honesty: null tokens, no persistence
# ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unmeasured_usage_returns_null_tokens_never_a_fabricated_zero(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.services import vision_service

    task = await _seed_task(async_db_session)
    row = await _attach(async_db_session, task.id, RED_PNG)
    fake, _calls = _fake_invoke(description="A red square.", usage={})
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["tokens"] is None


@pytest.mark.asyncio
async def test_describe_never_overwrites_the_task_result_or_the_attachment_row(
    vision_client, async_db_session, configured_key, monkeypatch
):
    from app.models.task import Task
    from app.services import vision_service

    task = await _seed_task(async_db_session, result_summary="the original task result")
    row = await _attach(async_db_session, task.id, RED_PNG)
    original_sha = row.sha256
    original_mime = row.mime
    original_name = row.filename
    fake, _calls = _fake_invoke(description="A red square.")
    monkeypatch.setattr(vision_service, "invoke", fake)

    response = await vision_client.post(
        "/api/v1/vision/describe", headers=HEADERS, json={"attachment_id": row.id}
    )
    assert response.status_code == 200, response.text

    refreshed = await async_db_session.get(Task, task.id)
    await async_db_session.refresh(refreshed)
    assert refreshed.result_summary == "the original task result", (
        "the description is model output for the operator, never the task's result"
    )
    from app.models.task import TaskAttachment

    attachment = await async_db_session.get(TaskAttachment, row.id)
    await async_db_session.refresh(attachment)
    assert (attachment.sha256, attachment.mime, attachment.filename) == (
        original_sha,
        original_mime,
        original_name,
    )
