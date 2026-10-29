"""W4/SENTINEL — artifact PATH BOUNDS: an over-long path is a NAMED validation refusal.

Measured live on the rebuilt stack (HEAD 068d1e7), wave 4:

    GET /api/v1/artifacts/314/content?path=<500 x 'a'>
      -> 500  {"code": "E_INTERNAL", "message": "the request failed unexpectedly (OSError)"}
      -> backend log: OSError: [Errno 36] File name too long:
         '/app/data/workspace/runs/314/aaaa…'

`pathlib.Path.exists()` only swallows a short errno list (ENOENT/ENOTDIR/EBADF/ELOOP), so
`File name too long` propagated out of `resolve_artifact` as an UNNAMED 500 — a refusal the module's
own contract does not allow ("refused BY NAME" for every unsafe form).  Nothing was served and no
path escaped the workspace, so this was a contract/robustness defect, not an exposure.

The fix bounds the path BEFORE any filesystem call (`MAX_PATH_CHARS` / `MAX_PATH_COMPONENT_CHARS`,
a validation refusal — a component over NAME_MAX can never name a real file) and maps an unexpected
`OSError` from the probe calls to the named `E_STORAGE`.

RED leg: with the pre-fix service file restored, the two `*too_long*` tests fail with the raw
`OSError` (ENAMETOOLONG) and the long-total-path case raises instead of refusing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import MeridianError
from app.services import artifact_service as artifacts


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    run = workspace / "runs" / "314"
    (run / "notes").mkdir(parents=True)
    (run / "notes" / "one-pager.md").write_bytes(b"REAL ARTIFACT\n")
    return workspace


def _refusal(call) -> MeridianError:
    with pytest.raises(MeridianError) as excinfo:
        call()
    return excinfo.value


def test_a_component_over_name_max_is_the_named_validation_refusal(tmp_path):
    """300 chars can never name a real file (NAME_MAX 255) and used to escape as OSError."""
    workspace = _workspace(tmp_path)

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", "a" * 300, workspace))

    assert refusal.code == "E_VALIDATION"
    assert refusal.context.get("field") == "path"
    assert "too long" in refusal.message


def test_an_overlong_total_path_is_the_named_validation_refusal(tmp_path):
    """Every component is legal, the total is not: refused before any filesystem call."""
    workspace = _workspace(tmp_path)
    long_path = "/".join(["seg"] * 400)  # 1,599 chars

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", long_path, workspace))

    assert refusal.code == "E_VALIDATION"
    assert refusal.context.get("field") == "path"
    assert "too long" in refusal.message


def test_a_long_but_in_bounds_path_still_reaches_the_filesystem(tmp_path):
    """The bound must not turn a legitimate deep path into a validation refusal."""
    workspace = _workspace(tmp_path)
    path = "/".join(["seg"] * 60)  # 239 chars, 3-char components: under both bounds

    refusal = _refusal(lambda: artifacts.read_artifact_text("314", path, workspace))

    assert refusal.code == "E_NOT_FOUND"


def test_a_real_nested_file_still_reads_exactly(tmp_path):
    workspace = _workspace(tmp_path)

    content = artifacts.read_artifact_text("314", "notes/one-pager.md", workspace)

    assert content.text == "REAL ARTIFACT\n"
    assert content.path == "notes/one-pager.md"
    assert content.size == len(b"REAL ARTIFACT\n")


def test_the_bounds_are_the_documented_ones():
    assert artifacts.MAX_PATH_COMPONENT_CHARS == 255  # NAME_MAX on ext4/NTFS
    assert artifacts.MAX_PATH_CHARS >= 255  # a legitimate deep path still fits
