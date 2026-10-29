"""W2/SENTINEL — the phone driver path anchor: `/app/scripts`, never the filesystem root.

THE FINDING (wave 1, §4.1 P3, message-only).  `DRIVER_PATH` was
`Path(__file__).resolve().parents[3] / "scripts" / "phone_send_driver.py"`.  That is correct in a
checkout (`<repo>/backend/app/services/...` -> `<repo>/scripts/...`) and WRONG in the backend image,
where the package is `/app/app/services/...` so `parents[3]` is `/` — the operator's refusal said
`/scripts/phone_send_driver.py`, a path that does not exist in the image and is not where anyone
would mount the driver.

THE FIX.  `BACKEND_ROOT = parents[2]` (the package root: `<repo>/backend` or `/app`), and
`driver_candidates()` names BOTH real layouts, package-adjacent first (`<root>/scripts`) then its
parent (`<root>/../scripts`, which is where the checkout keeps it).  `DRIVER_PATH` is the first
candidate that exists at import time, else the primary — so the refusal names `/app/scripts/...` in
the image and the repo's own script in a checkout.

NO BEHAVIOUR CHANGE WHEN THE DRIVER IS ABSENT: the same `E_PHONE_UNAVAILABLE` refusal, the same
"the phone send driver is missing: <path>" message shape; only the path and the metadata are now
true, and the context lists every candidate so an operator is not sent to the wrong directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.core.errors import E_PHONE_UNAVAILABLE, MeridianError
from app.services import phone_send_service as service


# ── the anchor arithmetic, against the REAL image layout ─────────────────────────────────────
def test_the_old_anchor_was_the_filesystem_root_in_the_image_layout() -> None:
    """The defect, reproduced as pure path arithmetic (no container needed to see it)."""
    in_image = Path("/app/app/services/phone_send_service.py")
    old_anchor = in_image.parents[3]  # the shipped expression

    assert len(old_anchor.parts) == 1, (
        f"parents[3] of the image path is {old_anchor!r} — a filesystem ROOT, not /app"
    )
    assert old_anchor == in_image.anchor or str(old_anchor) in {"/", "\\"}, (
        "the old anchor is the root itself"
    )
    named = (old_anchor / "scripts" / service.DRIVER_FILENAME).as_posix()
    assert named.endswith("/scripts/phone_send_driver.py"), named
    assert "/app/" not in named, (
        f"the wave-1 refusal named {named!r}: no /app component, so the operator was sent to a "
        "directory that does not exist in the image"
    )


def test_the_new_anchor_is_the_package_root_in_both_layouts() -> None:
    checkout = Path("/repo/backend/app/services/phone_send_service.py")
    in_image = Path("/app/app/services/phone_send_service.py")

    assert checkout.parents[2].as_posix() == "/repo/backend"
    assert in_image.parents[2].as_posix() == "/app"

    for package_root, package_scripts in (("/repo/backend", "/repo/backend/scripts"),
                                          ("/app", "/app/scripts")):
        root = Path(package_root)
        clone = Path(f"{package_root}/app/services/phone_send_service.py")
        assert clone.parents[2] == root
        assert (root / "scripts" / service.DRIVER_FILENAME).as_posix() == (
            f"{package_scripts}/phone_send_driver.py"
        )


def test_the_running_module_anchors_at_the_package_root(monkeypatch) -> None:
    """The real module (not a simulation): BACKEND_ROOT is `backend/`, not the repo root."""
    assert Path(service.__file__).resolve().parents[2] == service.BACKEND_ROOT
    assert service.BACKEND_ROOT.name == "backend"


def test_the_candidates_name_the_package_layout_and_then_its_parent(monkeypatch) -> None:
    fake_root = Path("/app")
    monkeypatch.setattr(service, "BACKEND_ROOT", fake_root)
    candidates = [path.as_posix() for path in service.driver_candidates()]

    assert candidates == [
        "/app/scripts/phone_send_driver.py",
        "/scripts/phone_send_driver.py",
    ], "the primary is the mount point the image can actually have; the parent is the fallback"


def test_the_checkout_resolves_the_script_that_exists_in_this_tree() -> None:
    repo_root = service.BACKEND_ROOT.parent
    checkout_driver = repo_root / "scripts" / service.DRIVER_FILENAME

    assert checkout_driver in service.driver_candidates()
    if checkout_driver.is_file():
        assert checkout_driver == service.DRIVER_PATH, (
            "in a checkout the shipped script must be the one this process would run, exactly as "
            "before the fix (the old anchor also resolved here)"
        )


# ── the refusal: same code, true path, honest metadata ──────────────────────────────────────
@pytest.mark.asyncio
async def test_the_missing_driver_refusal_names_every_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """A refusal must send the operator to the right directory, and show all of them."""
    fake_root = tmp_path / "app"
    fake_root.mkdir()
    monkeypatch.setattr(service, "BACKEND_ROOT", fake_root)
    monkeypatch.setattr(service, "DRIVER_PATH", fake_root / "gone.py")
    monkeypatch.delenv("MERIDIAN_PHONE_SEND_DRIVER", raising=False)
    monkeypatch.setattr(settings, "sandbox_mode", False)
    monkeypatch.delenv("MERIDIAN_KILL_SWITCH", raising=False)
    monkeypatch.setattr(settings, "kill_switch", "")

    with pytest.raises(MeridianError) as caught:
        await service.send("+15550104477", "sentinel driver-path probe — must not send")

    err = caught.value
    assert err.code == E_PHONE_UNAVAILABLE
    assert "phone send driver is missing" in err.message
    assert err.context["driver_path"] == str(fake_root / "gone.py")
    assert err.context["driver_candidates"] == [
        str(fake_root / "scripts" / service.DRIVER_FILENAME),
        str(tmp_path / "scripts" / service.DRIVER_FILENAME),
    ]
    assert err.context["driver_path_env"] == "MERIDIAN_PHONE_SEND_DRIVER"


@pytest.mark.asyncio
async def test_the_env_override_is_honoured_without_a_candidate_search(
    tmp_path: Path, monkeypatch
) -> None:
    override = tmp_path / "custom_driver.py"
    monkeypatch.setenv("MERIDIAN_PHONE_SEND_DRIVER", str(override))

    assert service.driver_executable() == override


def test_the_primary_path_is_used_when_no_candidate_exists(monkeypatch) -> None:
    """`driver_candidates()[0]` is the import-time fallback: the path the refusal will name."""
    primary = service.driver_candidates()[0]
    with monkeypatch.context() as patch:
        patch.setattr(service, "BACKEND_ROOT", Path("/definitely/not/here"))
        assert service.driver_candidates()[0] == Path(
            "/definitely/not/here/scripts/phone_send_driver.py"
        )
    assert primary.name == service.DRIVER_FILENAME
