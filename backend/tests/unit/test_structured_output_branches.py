"""R21-struct — the adversarial branch sweep for `app.agents.structured_output`.

This module is the single choke point between "the CLI printed something" and "the orchestrator got
a schema-valid object": `base_agent._classify_invoke`/`_finish_call`/`_retryable`,
`call_claude_structured`'s ladder, `deepseek_runtime`'s envelope producer and
`schemas.agent_outputs` all consume it. Every branch below is exercised against the REAL functions
(no monkeypatched production code), and every defect this sweep found is pinned by a test that
fails on the pre-fix body — the RED legs are named in each section.

Found and fixed (symptom -> root cause -> fix), each with its pre-fix failure mode:

1. `classify_error_envelope` crashed with `TypeError: sequence item 0: expected str instance, dict
   found` when an error envelope's `errors` was not a list of strings (`list[dict]`, `list[int]`,
   `list[None]`, a bare int) and silently mangled a bare str/dict detail (`"boom"` -> `"b; o; o;
   m"`; `{"msg": "x"}` -> `"msg"`). No consumer wraps the classifier, so the raw `TypeError`
   escaped `_classify_invoke`, `_finish_call`, `_retryable`, `resolve_output` and the ladder. Root
   cause: `"; ".join(env.get("errors") or [])`. Fixed by `_errors_detail`.
   RED: `test_retries_exhausted_with_object_errors_*` + the `_errors_detail` parametrization.
2. `coerce_types` rewrote an UNRECOGNIZED string to a definite `False` for a bool field
   (`"maybe"/"unclear"/"2"/""`), and the validator then accepted the fabricated value — a payload
   the ladder reports as OK carrying a value the model never said. Root cause: a membership test
   that answers False for every unmapped token. Fixed by the `_BOOL_TRUE_TOKENS`/`_BOOL_FALSE_TOKENS`
   allowlist. RED: `test_unrecognized_bool_token_is_never_rewritten_to_false`.
3. `_de_trailing_commas` mutated string CONTENT while repairing a trailing comma
   (`{"note": "x,}", "a": 1,}` -> `{"note": "x}", "a": 1}`), delivering a different payload as a
   successful `text_extract`. Root cause: a regex blind to string state. Fixed by an escape-aware
   scan. RED: `test_trailing_comma_repair_never_rewrites_string_content`.
4. Both extractors ended in ONE whole-span slice (first-bracket..last-bracket, end-anchored), so
   they returned nothing for `{"a": 1} — done`, `Result follows: {name} then {"a": 1}` and for a
   hook line holding a brace. Root cause: that regex, whose comment claimed a "last
   balanced-brace slice". Fixed by `_last_json_value`. RED:
   `test_json_followed_by_prose_is_still_extracted`, `test_brace_in_prose_does_not_hide_the_payload`,
   `test_envelope_after_hook_noise_with_a_brace_is_found`.
5. `resolve_output` consumed `required_fields` twice, so a GENERATOR (the documented `Iterable[str]`)
   made the lenient `PARTIAL_RESULT` branch unreachable. RED:
   `test_partial_result_classification_works_for_any_iterable_required_fields`.
6. A deeply nested JSON blob in model text raised `RecursionError` out of
   `extract_json_from_text`/`resolve_output` (json recurses and raises that, not JSONDecodeError).
   Fixed by catching it and failing the scan closed. RED:
   `test_deeply_nested_result_text_is_a_named_failure_not_a_crash`.

Documented behaviour pinned WITHOUT edits (no defect proven — stated so a later lane does not read
them as approvals):

* a `stream-json` run whose final line is truncated yields the earlier event, so the failure is
  named E112 (EMPTY_RESULT) rather than E104 — same `E_CLI_JSON` surface code and the same retry
  verdict (`claude_cli.is_retryable_failure` reads subtype/status, and that event has neither);
* `is_error` that is not a real boolean (`1`, `"true"`) is not an error-class envelope (E106), and
  the `is True` test is the conservative one — truthiness would read the STRING `"false"` as an
  error. The contract types `is_error: boolean` (CLI-CONTRACT.md §2.1);
* `extract_json_from_text("null")` is `None`, indistinguishable from "nothing parsed" (the return
  type is `Any | None`);
* with `settings.claude_repair_attempts > 1` the ladder log carries two `R2->` entries (the loop's
  re-prompt and the narrow-extraction call). Reported, not renamed: the label is a surface the
  parent's existing records quote. `test_ladder_log_distinguishes_narrow_extraction_when_reprompts_2`
  pins both configurations;
* `e_code_for` collapses the deliberate `""` entries (`OK`, `DUPLICATE_SUPPRESSED`) and the
  firewall-owned `RATE_CAPPED` to `E_AGENT_BAD_OUTPUT`; an E-code must never be empty.
"""
from __future__ import annotations

import json
import sys

import pytest
from pydantic import BaseModel, ConfigDict

from app.agents import claude_cli, structured_output
from app.agents.base_agent import BasePolsiaAgent
from app.agents.structured_output import (
    FAILURE_TO_E_CODE,
    REMEDIATION,
    FailureCode,
    OutputResolution,
    RepairPolicy,
    classify_error_envelope,
    coerce_types,
    e_code_for,
    extract_envelope,
    extract_json_from_text,
    policy_from_settings,
    prompt_repair_message,
    resolve_output,
    run_repair_ladder,
    usage_from_envelope_raw,
)
from app.core.errors import (
    E_AGENT_BAD_OUTPUT,
    E_BUDGET_EXCEEDED,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_POLICY_DENIED,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    PolsiaError,
)


class _Agent(BasePolsiaAgent):
    agent_type = "r21probe"

    def run(self, task, context):  # pragma: no cover - never dispatched here
        return {"summary": "probe"}


class _FlagModel(BaseModel):
    """Only a real boolean validates (pydantic's lax table rejects `maybe`/`unclear`/`2`/`""`)."""

    flagged: bool


class _StrictFlagModel(BaseModel):
    """A STRICT bool field: pydantic's lax table already accepts `"true"`/`"yes"`/`"0"`, so the
    coercion branch is what a strict (or narrowly-typed) consumer actually needs."""

    model_config = ConfigDict(strict=True)

    flagged: bool


class _SummaryModel(BaseModel):
    summary: str
    count: int


def _invoke_result(stdout: str, exit_code: int = 1) -> claude_cli.InvokeResult:
    return claude_cli.InvokeResult(stdout=stdout, stderr="stderr detail", exit_code=exit_code,
                                   wall_seconds=0.01, transport="argv")


def _env(**kw) -> str:
    """A success-shaped CLI envelope carrying whatever the test needs."""
    base = {"type": "result", "subtype": "success", "is_error": False}
    base.update(kw)
    return json.dumps(base)


def _err_env(**kw) -> str:
    base = {"type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": "it broke", "api_error_status": None}
    base.update(kw)
    return json.dumps(base)


# ─────────────────────────────────────────────────────────────────────────────
# 1. extract_envelope — every strategy, every failure
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_stdout_is_a_value_error_naming_the_cause():
    for stdout in ("", "   ", "\n\t ", None):
        with pytest.raises(ValueError, match="empty stdout"):
            extract_envelope(stdout)  # type: ignore[arg-type]


def test_one_json_object_on_stdout_is_parsed_even_when_pretty_printed():
    assert extract_envelope('{"type": "result"}') == {"type": "result"}
    pretty = json.dumps({"type": "result", "subtype": "success", "nested": {"a": [1, 2]}}, indent=2)
    assert extract_envelope(pretty)["subtype"] == "success"


@pytest.mark.parametrize("stdout", [
    "[1, 2, 3]",           # a JSON array is not an envelope
    "42",
    '"a string"',
    "null",
    "true",
    "not json at all",
])
def test_non_object_stdout_is_a_value_error_not_a_payload(stdout):
    """The envelope is an object by contract; a bare array/scalar must never be returned as one."""
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_envelope(stdout)


def test_stream_json_last_result_line_wins_over_earlier_events():
    stdout = ('{"type":"system","subtype":"init"}\n'
              '{"type":"assistant","message":{"content":[]}}\n'
              '{"type":"result","subtype":"success","result":"done"}')
    assert extract_envelope(stdout)["type"] == "result"
    assert extract_envelope(stdout)["result"] == "done"


def test_noise_lines_that_do_not_start_with_a_brace_are_skipped():
    stdout = 'warning: no json here\n{"type":"result","subtype":"success"}'
    assert extract_envelope(stdout)["subtype"] == "success"


def test_a_brace_line_that_does_not_parse_is_stepped_over():
    """A truncated/braced junk line must not stop the scan: the good object above it is still the
    envelope (JSONDecodeError on a candidate line -> continue)."""
    stdout = '{"type":"result","subtype":"success"}\n{"broken": '
    assert extract_envelope(stdout)["subtype"] == "success"


def test_truncated_stream_json_final_line_yields_the_earlier_event_PINNED():
    """PINNED (residual, not an approval): a cut-off result line means the run never finished, but
    the reverse line scan legitimately returns the last COMPLETE event. Both the E-code surface
    (`E_CLI_JSON`) and the retry verdict are identical to the E104 classification, so this is a
    naming difference, not a behaviour difference — recorded here so a later lane sees it stated."""
    stdout = '{"type":"system","subtype":"init"}\n{"type":"result","subtype":"suc'
    assert extract_envelope(stdout) == {"type": "system", "subtype": "init"}
    resolution = resolve_output(stdout=stdout, exit_code=0)
    assert resolution.code is FailureCode.EMPTY_RESULT
    assert resolution.e_code == E_CLI_JSON


def test_envelope_after_hook_noise_with_a_brace_is_found():
    """RED (fix 4): the first-brace..last-brace slice could not decode this line."""
    stdout = 'hook {"plugin":"x"} {"type":"result","subtype":"success"}'
    assert extract_envelope(stdout) == {"type": "result", "subtype": "success"}


def test_two_objects_on_one_line_take_the_last_one():
    """RED (fix 4): the greedy span `{"a": 1} {"b": 2}` is not JSON at all."""
    assert extract_envelope('{"a": 1} {"b": 2}') == {"b": 2}
    assert extract_envelope('{"type":"result","subtype":"error_max_turns"} '
                            '{"type":"result","subtype":"success"}')["subtype"] == "success"


def test_envelope_followed_by_prose_is_still_found():
    """RED (fix 4): a sentence after the object defeated the end-anchored slice."""
    assert extract_envelope('{"type":"result","subtype":"success"} — done')["type"] == "result"


def test_leading_brace_in_prose_does_not_hide_the_envelope():
    """RED (fix 4): `config {x} ok {...}` matched from the FIRST brace and never decoded."""
    stdout = 'config {x} ok {"type":"result","subtype":"success"}'
    assert extract_envelope(stdout)["subtype"] == "success"


def test_nested_objects_return_the_OUTER_envelope_never_an_inner_one():
    """The scan jumps past each decoded value, so a nested object can never win as "the last"."""
    stdout = 'noise {"type":"result","structured_output":{"a":{"b":1}},"usage":{"in":2}}'
    parsed = extract_envelope(stdout)
    assert parsed["type"] == "result"
    assert parsed["structured_output"] == {"a": {"b": 1}}


def test_bom_prefixed_envelope_is_parsed_by_the_last_resort():
    assert extract_envelope('\ufeff{"type":"result","subtype":"success"}')["type"] == "result"


def test_envelope_needing_the_comma_repair_is_parsed_by_the_last_resort():
    assert extract_envelope('{"type":"result","subtype":"success",}')["type"] == "result"


def test_deeply_nested_stdout_is_a_named_value_error_not_a_RecursionError():
    """RED (fix 6): json's decoder raises RecursionError (not JSONDecodeError) past the limit."""
    depth = sys.getrecursionlimit() * 5
    deep = "[" * depth + "1" + "]" * depth
    with pytest.raises(RecursionError):  # control: the input really is over the limit
        json.loads(deep)
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_envelope(deep)
    # ... and a nesting depth inside the limit still parses, so the guard is not over-reaching.
    shallow = json.dumps({"type": "result", "structured_output": json.loads("[" * 200 + "1" + "]" * 200)})
    assert extract_envelope(shallow)["type"] == "result"


# ─────────────────────────────────────────────────────────────────────────────
# 2. extract_json_from_text + the text repairs
# ─────────────────────────────────────────────────────────────────────────────
def test_text_extraction_of_nothing_is_none():
    assert extract_json_from_text("") is None
    assert extract_json_from_text("   \n\t ") is None
    assert extract_json_from_text("just prose, no payload") is None
    assert extract_json_from_text('{"a": 1, "b": ') is None          # truncated object


def test_text_extraction_of_the_plain_shapes():
    assert extract_json_from_text('{"a": 1}') == {"a": 1}
    assert extract_json_from_text('[1, 2, 3]') == [1, 2, 3]
    assert extract_json_from_text('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]
    assert extract_json_from_text('{"a": {"b": [1, {"c": 2}]}}') == {"a": {"b": [1, {"c": 2}]}}
    assert extract_json_from_text("  {\"a\": 1}  ") == {"a": 1}       # whitespace tolerated
    assert extract_json_from_text("true") is True                     # JSON scalars are values too
    assert extract_json_from_text("null") is None                     # PINNED: same None as "none"


def test_text_extraction_prefers_the_fenced_block_even_with_prose_around_it():
    assert extract_json_from_text('here you go:\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert extract_json_from_text('```\n{"a": 1}\n```') == {"a": 1}


def test_text_extraction_repairs_smart_quotes_and_trailing_commas():
    assert extract_json_from_text('{\u201ca\u201d: \u201cv\u201d,}') == {"a": "v"}
    assert extract_json_from_text('{"a": 1,}') == {"a": 1}
    assert extract_json_from_text('{"a": [1, 2,],}') == {"a": [1, 2]}
    assert extract_json_from_text('sure:\n```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}


def test_trailing_comma_repair_never_rewrites_string_content():
    """RED (fix 3): the regex repair deleted commas INSIDE string literals, so the caller got a
    different payload than the model wrote — as a successful `text_extract`."""
    assert extract_json_from_text('{"note": "x,}", "a": 1,}') == {"note": "x,}", "a": 1}
    assert extract_json_from_text('{"note": "y,]", "a": 1,}') == {"note": "y,]", "a": 1}
    assert extract_json_from_text('{"note": "he said \\"a,}\\"", "a": 1,}') == {
        "note": 'he said "a,}"', "a": 1}
    assert extract_json_from_text('```json\n{"code": "d = {\\"a\\": 1,}", "n": 2,}\n```') == {
        "code": 'd = {"a": 1,}', "n": 2}
    # a comma-brace inside a string of a payload that needs NO repair is untouched either way
    assert extract_json_from_text('{"note": "x,}", "a": 1}') == {"note": "x,}", "a": 1}


def test_text_extraction_last_resort_finds_the_object_after_prose_braces():
    """RED (fix 4): every whole-span candidate failed on these, returning None."""
    assert extract_json_from_text('Result follows: {name} then {"a": 1}') == {"a": 1}
    assert extract_json_from_text('use {x} here: {"a": 1}') == {"a": 1}
    assert extract_json_from_text('{"a": 1} — hope that helps') == {"a": 1}
    assert extract_json_from_text('Result follows: {name} then {"a": 1,}') == {"a": 1}


def test_text_extraction_last_resort_takes_the_last_value_not_the_example():
    """A schema example followed by the real answer must not hand the caller the example."""
    text = 'schema example: {"summary": "string"}\nmy answer: {"summary": "real", "count": 3}'
    assert extract_json_from_text(text) == {"summary": "real", "count": 3}
    assert extract_json_from_text('{"first": 1} then {"second": 2}') == {"second": 2}


def test_text_extraction_never_returns_a_NESTED_object_as_the_payload():
    """The scan steps past each decoded value: the outer object is the payload, not its child."""
    assert extract_json_from_text('noise {"outer": {"inner": {"leaf": 1}}}') == {
        "outer": {"inner": {"leaf": 1}}}
    assert extract_json_from_text('noise {"outer": {"inner": 1}} tail') == {"outer": {"inner": 1}}


def test_text_extraction_keeps_unicode_and_emoji_byte_exact():
    payload = {"summary": "fête 🎉 中文 — naïve café ☕", "emoji_key🎈": "value"}
    assert extract_json_from_text(json.dumps(payload, ensure_ascii=False)) == payload
    assert extract_json_from_text('```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```') == payload


def test_text_extraction_of_a_bare_deep_enough_structure_still_works():
    depth = 200
    monkey_nested = "[" * depth + "1" + "]" * depth
    assert extract_json_from_text(monkey_nested) == json.loads(monkey_nested)


def test_deeply_nested_result_text_is_a_named_failure_not_a_crash():
    """RED (fix 6): this raised RecursionError out of the extraction and out of resolve_output."""
    depth = sys.getrecursionlimit() * 5
    deep = "[" * depth + "1" + "]" * depth
    assert extract_json_from_text(deep) is None
    resolution = resolve_output(stdout=_env(result=deep), exit_code=0)
    assert resolution.ok is False
    assert resolution.code is FailureCode.STRUCTURED_OUTPUT_MISSING
    assert resolution.e_code == E_CLI_JSON


def test_deeply_nested_structured_output_payload_is_refused_by_name():
    depth = sys.getrecursionlimit() * 5
    deep_text = "[" * depth + "1" + "]" * depth
    resolution = resolve_output(stdout=_env(result=deep_text, structured_output=None), exit_code=0)
    assert resolution.code is FailureCode.STRUCTURED_OUTPUT_MISSING


def test_de_trailing_commas_unit_level():
    from app.agents.structured_output import _de_trailing_commas

    assert _de_trailing_commas('{"a": 1,}') == '{"a": 1}'
    assert _de_trailing_commas('[1, 2,]') == '[1, 2]'
    assert _de_trailing_commas('{"a": [1,], "b": {"c": 2,},}') == '{"a": [1], "b": {"c": 2}}'
    assert _de_trailing_commas('{"s": "a,}",}') == '{"s": "a,}"}'      # string content survives
    assert _de_trailing_commas('{"s": "a \\",}",}') == '{"s": "a \\",}"}'
    assert _de_trailing_commas('{"a": 1, "b": 2}') == '{"a": 1, "b": 2}'  # inner commas stay
    assert _de_trailing_commas("") == ""


def test_last_json_value_helper_contract():
    from app.agents.structured_output import _last_json_value

    assert _last_json_value("no json here") is None
    assert _last_json_value("{broken") is None
    assert _last_json_value('{"a": 1}') == {"a": 1}
    assert _last_json_value('x {"a": 1,} y') == {"a": 1}            # repaired pass
    assert _last_json_value('{name} {"a": 1}') == {"a": 1}          # prose brace skipped
    assert _last_json_value('{"a": {"b": 1}}') == {"a": {"b": 1}}   # nested skipped
    assert _last_json_value("") is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. the failure taxonomy: classify_error_envelope + resolve_output branches
# ─────────────────────────────────────────────────────────────────────────────
def test_classify_error_envelope_arm_by_arm():
    assert classify_error_envelope({"subtype": "error_max_turns",
                                    "terminal_reason": "max_turns"}) == (
        FailureCode.MAX_TURNS, "terminal_reason=max_turns")
    assert classify_error_envelope({"subtype": "error_max_budget_usd"}) == (
        FailureCode.BUDGET_EXCEEDED, "CLI budget cap reached")
    assert classify_error_envelope({"subtype": "error_max_structured_output_retries",
                                    "errors": ["a", "b"]}) == (
        FailureCode.STRUCTURED_RETRIES_EXHAUSTED, "a; b")
    assert classify_error_envelope({"subtype": "error_during_execution",
                                    "api_error_status": 401}) == (
        FailureCode.CLI_AUTH_FAILED, "api_error_status=401")
    assert classify_error_envelope({"subtype": "error_during_execution",
                                    "api_error_status": 403}) == (
        FailureCode.CLI_AUTH_FAILED, "api_error_status=403")
    for reason in ("authentication_failed", "oauth_org_not_allowed", "account_on_hold"):
        code, detail = classify_error_envelope({
            "subtype": "error_during_execution", "startup_failure_reason": reason})
        assert code is FailureCode.CLI_AUTH_FAILED
        assert detail == "api_error_status=None"
    assert classify_error_envelope({"subtype": "error_during_execution"}) == (
        FailureCode.ENVELOPE_IS_ERROR, "api_error_status=None")
    assert classify_error_envelope({}) == (
        FailureCode.ENVELOPE_IS_ERROR, "is_error=true without subtype")
    assert classify_error_envelope({"subtype": "error_max_tokens"}) == (
        FailureCode.ENVELOPE_IS_ERROR, "subtype=error_max_tokens")
    assert classify_error_envelope({"subtype": "error_wat"}) == (
        FailureCode.ENVELOPE_IS_ERROR, "subtype=error_wat")


@pytest.mark.parametrize(("errors", "want"), [
    (["a", "b"], "a; b"),
    ([{"field": "x", "msg": "bad"}], "{'field': 'x', 'msg': 'bad'}"),   # RED (fix 1)
    ([1, 2], "1; 2"),                                                   # RED (fix 1)
    ([None, "x"], "None; x"),                                           # RED (fix 1)
    ("boom", "boom"),                                                   # RED (fix 1)
    ({"msg": "boom"}, "{'msg': 'boom'}"),                               # RED (fix 1)
    (42, "42"),                                                         # RED (fix 1)
    ([], ""),
    (None, ""),
])
def test_errors_detail_never_crashes_and_never_drops_the_message(errors, want):
    """RED (fix 1): `list[dict]`/`list[int]`/`list[None]`/`int` raised TypeError; a str/dict was
    mangled to `"b; o; o; m"`/`"msg"` (value dropped)."""
    from app.agents.structured_output import _errors_detail

    assert _errors_detail(errors) == want
    env = {"subtype": "error_max_structured_output_retries", "errors": errors}
    assert classify_error_envelope(env) == (FailureCode.STRUCTURED_RETRIES_EXHAUSTED, want)


def test_errors_detail_truncates_at_400_like_the_documented_contract():
    from app.agents.structured_output import _errors_detail

    assert _errors_detail(["x" * 500]) == "x" * 400
    assert len(_errors_detail([str(i) for i in range(200)])) == 400


def test_retries_exhausted_with_object_errors_keeps_the_run_alive():
    """RED (fix 1) at the CONSUMER: this envelope used to raise a raw TypeError out of
    `_classify_invoke`, `_finish_call`, `_retryable`, `resolve_output` and `run_repair_ladder`."""
    stdout = _err_env(subtype="error_max_structured_output_retries",
                      errors=[{"field": "summary", "message": "required property missing"}])
    result = _invoke_result(stdout)
    agent = _Agent()

    failure, detail, env = agent._classify_invoke(result)
    assert failure is FailureCode.STRUCTURED_RETRIES_EXHAUSTED
    assert "summary" in detail and "required property missing" in detail
    assert env["subtype"] == "error_max_structured_output_retries"

    with pytest.raises(PolsiaError) as excinfo:
        agent._finish_call(result, 1, transport="argv")
    assert excinfo.value.code == E_AGENT_BAD_OUTPUT
    assert "POLSIA-E109" in str(excinfo.value)
    assert agent._retryable(result) is False

    assert resolve_output(stdout=stdout, exit_code=1).code is FailureCode.STRUCTURED_RETRIES_EXHAUSTED
    assert run_repair_ladder(dispatch=lambda override: (stdout, 1),
                             resolve_kwargs={"schema_name": "s"}).code is (
        FailureCode.STRUCTURED_RETRIES_EXHAUSTED)


def test_resolve_output_transport_level_failures():
    assert resolve_output(stdout=_env(result="x"), exit_code=143).code is FailureCode.CLI_TIMEOUT
    # 143 is OUR kill: it wins even over an envelope that happens to carry a valid payload
    assert resolve_output(stdout=_env(result="x", structured_output={"a": 1}),
                          exit_code=143).code is FailureCode.CLI_TIMEOUT
    assert resolve_output(stdout="not json", exit_code=1).code is FailureCode.CLI_NONZERO_EXIT
    assert resolve_output(stdout="not json", exit_code=0).code is FailureCode.CLI_STDOUT_NOT_JSON
    assert resolve_output(stdout="", exit_code=0).code is FailureCode.CLI_STDOUT_NOT_JSON


def test_resolve_output_envelope_level_failures_are_named():
    assert resolve_output(stdout=_err_env(), exit_code=0).code is FailureCode.ENVELOPE_IS_ERROR
    assert resolve_output(stdout=_err_env(subtype="success", is_error=True),
                          exit_code=0).code is FailureCode.ENVELOPE_IS_ERROR
    for subtype in ("error_max_turns", "error_max_budget_usd",
                    "error_max_structured_output_retries", "error_during_execution"):
        resolution = resolve_output(stdout=_err_env(subtype=subtype), exit_code=0)
        assert resolution.ok is False and resolution.payload is None
        assert resolution.hint  # every failure carries its remediation, never a bare code
    assert resolve_output(stdout=_err_env(subtype="error_during_execution",
                                          api_error_status=401),
                          exit_code=0).code is FailureCode.CLI_AUTH_FAILED


def test_resolve_output_payload_absent_branches():
    text_extract = resolve_output(stdout=_env(result='{"a": 1}'), exit_code=0)
    assert text_extract.ok and text_extract.payload == {"a": 1}
    assert text_extract.used_repair == "text_extract"

    assert resolve_output(stdout=_env(result="I can't help with that."),
                          exit_code=0).code is FailureCode.REFUSAL
    assert resolve_output(stdout=_env(result="  "), exit_code=0).code is FailureCode.EMPTY_RESULT
    assert resolve_output(stdout=_env(result="I did the thing."),
                          exit_code=0).code is FailureCode.STRUCTURED_OUTPUT_MISSING
    # explicit null structured_output is the same branch as an absent one
    assert resolve_output(stdout=_env(result="I did it", structured_output=None),
                          exit_code=0).code is FailureCode.STRUCTURED_OUTPUT_MISSING


@pytest.mark.parametrize("marker", [
    "i can't help with that", "I cannot help with that", "I won't do this",
    "I will not comply", "unable to assist", "cannot assist with that",
    "I'm not able to help here",
])
def test_every_documented_refusal_marker_is_recognized(marker):
    resolution = resolve_output(stdout=_env(result=marker), exit_code=0)
    assert resolution.code is FailureCode.REFUSAL
    assert resolution.raw_result  # the refusal text is kept for the record


def test_refusal_markers_do_not_fire_on_ordinary_prose_or_on_a_valid_payload():
    assert resolve_output(stdout=_env(result="the competitor will not lower prices"),
                          exit_code=0).code is FailureCode.STRUCTURED_OUTPUT_MISSING
    # a refusal-shaped sentence is irrelevant when the envelope DOES carry the payload
    resolution = resolve_output(stdout=_env(result="I cannot help with that",
                                            structured_output={"a": 1}), exit_code=0)
    assert resolution.ok and resolution.payload == {"a": 1}


@pytest.mark.parametrize(("payload", "type_name"), [
    ('{"a": 1}', "str"),        # structured_output holding a JSON STRING is off-contract
    ([1, 2], "list"),
    (0, "int"),                 # falsy non-None: not the "absent" branch
    (False, "bool"),
])
def test_non_object_structured_output_is_named_schema_failure(payload, type_name):
    resolution = resolve_output(stdout=_env(structured_output=payload), exit_code=0)
    assert resolution.code is FailureCode.SCHEMA_VALIDATION_FAILED
    assert resolution.detail == f"structured_output is {type_name}, expected object"
    assert resolution.payload is None


def test_resolve_output_raw_result_truncation_is_bounded_on_every_branch():
    long_text = "x" * 500
    ok = resolve_output(stdout=_env(result=long_text, structured_output={"a": 1}), exit_code=0)
    assert len(ok.raw_result) == 200
    missing = resolve_output(stdout=_env(result=long_text), exit_code=0)
    assert len(missing.raw_result) == 500


# ─────────────────────────────────────────────────────────────────────────────
# 4. coerce_types + the validator / PARTIAL_RESULT branches
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("raw", "want"), [
    ("true", True), ("TRUE", True), ("  yes  ", True), ("1", True),
    ("false", False), ("0", False), ("no", False), ("No", False),
])
def test_recognized_bool_tokens_are_coerced(raw, want):
    assert coerce_types({"flagged": raw}, {"flagged": bool}) == {"flagged": want}


@pytest.mark.parametrize("raw", ["maybe", "unknown", "partial", "unclear", "enabled", "2", "", "Tru"])
def test_unrecognized_bool_tokens_are_left_alone(raw):
    """RED (fix 2): every one of these used to become `False` here."""
    assert coerce_types({"flagged": raw}, {"flagged": bool}) == {"flagged": raw}


def test_unrecognized_bool_token_is_never_rewritten_to_false():
    """RED (fix 2) end to end through the REAL validator: before the fix this returned ok=True with
    a payload carrying `flagged: False` — a value the model never produced."""
    resolution = resolve_output(stdout=_env(structured_output={"flagged": "maybe"}), exit_code=0,
                                validator=_FlagModel.model_validate,
                                coercions={"flagged": bool},
                                required_fields=tuple(_FlagModel.model_json_schema()["required"]))
    assert resolution.ok is False
    assert resolution.used_repair is None
    assert resolution.code is FailureCode.SCHEMA_VALIDATION_FAILED
    assert "flagged" in resolution.detail


def test_recognized_bool_token_still_repairs_end_to_end():
    """A STRICT bool field is what forces the repair: pydantic's lax table already accepts
    `"true"`/`"yes"`/`"0"` itself, so a lax model would need no coercion at all. What matters is
    that a recognized token becomes a REAL bool and is recorded as `type_coercion`."""
    for raw, want in (("true", True), ("yes", True), ("1", True),
                      ("false", False), ("0", False), ("no", False)):
        resolution = resolve_output(stdout=_env(structured_output={"flagged": raw}), exit_code=0,
                                    validator=_StrictFlagModel.model_validate,
                                    coercions={"flagged": bool})
        assert resolution.ok and resolution.used_repair == "type_coercion"
        assert resolution.payload["flagged"] is want


def test_int_coercion_rescues_the_real_production_validator():
    """`_top_level_coercions` (base_agent L674-685) maps an int-annotated field to `int`, and
    pydantic's lax mode REJECTS the string `"4.7"` — so this is the repair production takes."""
    resolution = resolve_output(stdout=_env(structured_output={"summary": "s", "count": "4.7"}),
                                exit_code=0, validator=_SummaryModel.model_validate,
                                coercions={"count": int})
    assert resolution.ok and resolution.used_repair == "type_coercion"
    assert resolution.payload["count"] == 4


def test_coerce_types_scalar_table():
    assert coerce_types({"n": "42"}, {"n": int}) == {"n": 42}
    assert coerce_types({"n": "4.7"}, {"n": int}) == {"n": 4}          # int(float(...)), as before
    assert coerce_types({"n": "n/a"}, {"n": int}) == {"n": "n/a"}      # unparseable: untouched
    assert coerce_types({"f": "3.5"}, {"f": float}) == {"f": 3.5}
    assert coerce_types({"f": "x"}, {"f": float}) == {"f": "x"}
    assert coerce_types({"s": 3.5}, {"s": str}) == {"s": "3.5"}
    assert coerce_types({"s": True}, {"s": str}) == {"s": "True"}
    assert coerce_types({"s": {"a": 1}}, {"s": str}) == {"s": {"a": 1}}   # container untouched
    assert coerce_types({"s": [1]}, {"s": str}) == {"s": [1]}
    assert coerce_types({"other": 1}, {"s": str}) == {"other": 1}         # missing key untouched
    assert coerce_types({"flagged": None}, {"flagged": bool}) == {"flagged": None}
    assert coerce_types({"flagged": True}, {"flagged": bool}) == {"flagged": True}
    assert coerce_types({"n": 1}, {"n": bool}) == {"n": 1}                # int -> bool: not ours
    assert coerce_types([1, 2], {"n": int}) == [1, 2]                     # non-dict passthrough
    assert coerce_types("a string", {"n": int}) == "a string"


def test_coerce_types_returns_a_copy_and_never_mutates_its_input():
    original = {"flagged": "maybe", "n": "42"}
    coerced = coerce_types(original, {"flagged": bool, "n": int})
    assert original == {"flagged": "maybe", "n": "42"}
    assert coerced == {"flagged": "maybe", "n": 42}


def test_validator_path_success_records_no_repair():
    resolution = resolve_output(stdout=_env(structured_output={"flagged": True}), exit_code=0,
                                validator=_FlagModel.model_validate)
    assert resolution.ok and resolution.used_repair is None
    assert resolution.ladder_log == [] and resolution.partial is False


def test_validator_disagreement_without_a_usable_repair_is_E110_with_the_real_reason():
    resolution = resolve_output(stdout=_env(structured_output={"flagged": 7}), exit_code=0,
                                validator=_FlagModel.model_validate,
                                coercions={"flagged": bool})
    assert resolution.code is FailureCode.SCHEMA_VALIDATION_FAILED
    assert "flagged" in resolution.detail and len(resolution.detail) <= 400
    assert resolution.payload == {"flagged": 7}     # the payload is kept for the human
    assert resolution.used_repair is None
    # no coercions requested at all: the same branch, still named
    assert resolve_output(stdout=_env(structured_output={"flagged": 7}), exit_code=0,
                          validator=_FlagModel.model_validate).code is (
        FailureCode.SCHEMA_VALIDATION_FAILED)


def test_coercion_that_does_not_rescue_the_payload_returns_the_ORIGINAL():
    """The coercion branch must not hand back a half-repaired payload: when the coerced dict still
    fails, `payload` stays the original and the validator's own message is the detail."""
    resolution = resolve_output(stdout=_env(structured_output={"summary": 5, "count": "4.7"}),
                                exit_code=0, validator=_SummaryModel.model_validate,
                                coercions={"count": int})
    assert resolution.ok is False
    assert resolution.code is FailureCode.SCHEMA_VALIDATION_FAILED
    assert resolution.used_repair is None
    assert resolution.payload == {"summary": 5, "count": "4.7"}     # NOT the coerced dict
    assert "summary" in resolution.detail


def test_partial_result_classification_works_for_any_iterable_required_fields():
    """RED (fix 5): the GENERATOR case used to come back E110 — `required_fields` was consumed
    twice, so the second `list(...)` was empty and `len(missing) < len(required)` could not hold."""
    stdout = _env(structured_output={"summary": "only this one"})
    for required in (("summary", "count"), ["summary", "count"],
                     (f for f in ("summary", "count"))):
        resolution = resolve_output(stdout=stdout, exit_code=0,
                                    validator=_SummaryModel.model_validate,
                                    required_fields=required, tolerate_partial=True)
        assert resolution.code is FailureCode.PARTIAL_RESULT
        assert resolution.partial is True and resolution.ok is False
        assert resolution.payload == {"summary": "only this one"}
        assert resolution.detail == "missing=['count']"
        assert resolution.e_code == E_AGENT_BAD_OUTPUT


def test_partial_is_not_offered_when_tolerance_is_off_or_nothing_is_missing():
    stdout = _env(structured_output={"summary": "only this one"})
    kwargs = {"validator": _SummaryModel.model_validate, "required_fields": ("summary", "count")}
    assert resolve_output(stdout=stdout, exit_code=0, tolerate_partial=False, **kwargs).code is (
        FailureCode.SCHEMA_VALIDATION_FAILED)
    # every required field absent -> not a partial: nothing was produced at all
    assert resolve_output(stdout=_env(structured_output={"other": 1}), exit_code=0,
                          tolerate_partial=True, **kwargs).code is (
        FailureCode.SCHEMA_VALIDATION_FAILED)
    # no required_fields declared -> nothing can be "missing"
    assert resolve_output(stdout=stdout, exit_code=0, tolerate_partial=True,
                          validator=_SummaryModel.model_validate).code is (
        FailureCode.SCHEMA_VALIDATION_FAILED)
    # the key IS present but the type is wrong -> a schema failure, not a partial
    assert resolve_output(stdout=_env(structured_output={"summary": "s", "count": "not-an-int"}),
                          exit_code=0, tolerate_partial=True,
                          validator=_SummaryModel.model_validate,
                          required_fields=("summary", "count")).code is (
        FailureCode.SCHEMA_VALIDATION_FAILED)


# ─────────────────────────────────────────────────────────────────────────────
# 5. the repair ladder
# ─────────────────────────────────────────────────────────────────────────────
def _script(results: list[tuple[str, int]]):
    """A dispatch double that replays canned (stdout, exit_code) pairs and records the prompts."""
    state: dict = {"calls": 0, "overrides": []}

    def dispatch(override):
        state["overrides"].append(override)
        out = results[min(state["calls"], len(results) - 1)]
        state["calls"] += 1
        return out

    return dispatch, state


def test_ladder_R0_success_never_claims_a_repair():
    dispatch, state = _script([(_env(structured_output={"flagged": True}), 0)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy())
    assert resolution.ok and resolution.used_repair is None
    assert resolution.ladder_log == ["R0->POLSIA-E000"]
    assert state["overrides"] == [None]
    assert state["calls"] == 1


def test_ladder_R1_re_prompt_success_carries_the_correction_message():
    dispatch, state = _script([(_env(result="not json at all"), 0),
                               (_env(structured_output={"flagged": True}), 0)])
    policy = RepairPolicy()
    resolution = run_repair_ladder(dispatch=dispatch,
                                   resolve_kwargs={"validator": _FlagModel.model_validate,
                                                   "schema_name": "flag_agent"},
                                   policy=policy)
    assert resolution.ok and resolution.used_repair == "re_prompt"
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R1->POLSIA-E000"]
    assert policy.ladder_log == resolution.ladder_log
    assert state["calls"] == 2
    correction = state["overrides"][1]
    assert state["overrides"][0] is None
    assert "flag_agent" in correction and "POLSIA-E106" in correction
    assert "Return ONLY a single JSON object" in correction


def test_ladder_R2_narrow_extraction_after_exhausted_reprompts():
    dispatch, state = _script([(_env(result="not json at all"), 0),
                               (_env(structured_output={"flagged": 7}), 0),
                               (_env(structured_output={"flagged": True}), 0)])
    policy = RepairPolicy()
    resolution = run_repair_ladder(dispatch=dispatch,
                                   resolve_kwargs={"validator": _FlagModel.model_validate,
                                                   "schema_name": "flag_agent"},
                                   policy=policy)
    assert resolution.ok and resolution.used_repair == "narrow_extraction"
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R1->POLSIA-E110", "R2->POLSIA-E000"]
    assert policy.ladder_log == resolution.ladder_log
    assert state["calls"] == 3
    narrow = state["overrides"][2]
    assert "Extract the following fields" in narrow and "flag_agent" in narrow
    assert "use null" in narrow


def test_ladder_exhaustion_returns_a_named_failure_with_every_attempt_logged():
    dispatch, state = _script([(_env(result="still not json"), 0)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy())
    assert resolution.ok is False
    assert resolution.code is FailureCode.STRUCTURED_OUTPUT_MISSING
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R1->POLSIA-E106", "R2->POLSIA-E106"]
    assert state["calls"] == 3
    # the FINAL code is the last attempt's, not the first: named in the receipt as the contract
    assert resolution.detail == ""


def test_ladder_last_code_is_named_when_the_failure_class_changes_between_attempts():
    dispatch, _ = _script([(_env(structured_output={"flagged": 7}), 0), (_env(result="  "), 0)])
    resolution = run_repair_ladder(dispatch=dispatch,
                                   resolve_kwargs={"validator": _FlagModel.model_validate,
                                                   "schema_name": "s"},
                                   policy=RepairPolicy())
    assert resolution.code is FailureCode.EMPTY_RESULT        # WINNER = EMPTY_RESULT (documented)
    assert resolution.ladder_log == ["R0->POLSIA-E110", "R1->POLSIA-E112"]
    assert resolution.code is not FailureCode.SCHEMA_VALIDATION_FAILED


def test_ladder_zero_reprompts_still_allows_the_narrow_call():
    dispatch, state = _script([(_env(result="not json at all"), 0),
                               (_env(structured_output={"flagged": True}), 0)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy(max_cli_reprompts=0))
    assert resolution.ok and resolution.used_repair == "narrow_extraction"
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R2->POLSIA-E000"]
    assert state["calls"] == 2      # no re-prompt was paid for; only the narrow extraction


def test_ladder_narrow_extraction_disabled_and_ineligible_codes():
    dispatch, state = _script([(_env(result="not json at all"), 0)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy(allow_narrow_extraction=False))
    assert resolution.code is FailureCode.STRUCTURED_OUTPUT_MISSING
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R1->POLSIA-E106"]
    assert state["calls"] == 2

    for stdout, exit_code, code in [("", 143, FailureCode.CLI_TIMEOUT),
                                    (_env(result="I cannot help with that."), 0, FailureCode.REFUSAL),
                                    ("garbage", 1, FailureCode.CLI_NONZERO_EXIT)]:
        dispatch, state = _script([(stdout, exit_code)])
        resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                       policy=RepairPolicy())
        assert resolution.code is code
        assert state["calls"] == 2          # R0 + R1 re-prompt, and NO narrow extraction
        assert all(r.startswith(("R0->", "R1->")) for r in resolution.ladder_log)


def test_ladder_log_distinguishes_narrow_extraction_when_reprompts_2():
    """PINNED (reported, not renamed): with `claude_repair_attempts > 1` the narrow-extraction call
    wears the loop's own `R2->` label, so the log alone cannot tell a re-prompt from the extraction
    call. The default (1) leaves the label exact, which is why this is a record, not an edit."""
    dispatch, state = _script([(_env(result="not json at all"), 0)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy(max_cli_reprompts=2))
    assert state["calls"] == 4          # R0, R1, R2 re-prompts + the narrow call
    assert resolution.ladder_log == ["R0->POLSIA-E106", "R1->POLSIA-E106",
                                     "R2->POLSIA-E106", "R2->POLSIA-E106"]


def test_ladder_default_policy_comes_from_settings(monkeypatch):
    import app.config as app_config

    assert policy_from_settings().max_cli_reprompts == max(
        0, int(app_config.settings.claude_repair_attempts or 0))
    assert policy_from_settings().allow_narrow_extraction is True
    monkeypatch.setattr(app_config.settings, "claude_repair_attempts", 3)
    assert policy_from_settings().max_cli_reprompts == 3
    monkeypatch.setattr(app_config.settings, "claude_repair_attempts", -5)
    assert policy_from_settings().max_cli_reprompts == 0        # clamped, never a negative range
    monkeypatch.setattr(app_config.settings, "claude_repair_attempts", None)
    assert policy_from_settings().max_cli_reprompts == 0


def test_ladder_reaches_a_named_failure_for_the_object_errors_envelope():
    """RED (fix 1) through the ladder: this used to raise TypeError out of `resolve_output`."""
    stdout = _err_env(subtype="error_max_structured_output_retries", errors=[{"a": 1}])
    dispatch, _ = _script([(stdout, 1)])
    resolution = run_repair_ladder(dispatch=dispatch, resolve_kwargs={"schema_name": "s"},
                                   policy=RepairPolicy())
    assert resolution.code is FailureCode.STRUCTURED_RETRIES_EXHAUSTED
    assert resolution.hint == REMEDIATION[FailureCode.STRUCTURED_RETRIES_EXHAUSTED]


def test_prompt_repair_message_content_and_empty_detail_fallback():
    message = prompt_repair_message(FailureCode.SCHEMA_VALIDATION_FAILED, "", "schema_x")
    assert "schema_x" in message
    assert FailureCode.SCHEMA_VALIDATION_FAILED.value in message      # detail falls back to the code
    assert "validator_error:" in message


# ─────────────────────────────────────────────────────────────────────────────
# 6. OutputResolution / e_code_for / the frozen mappings
# ─────────────────────────────────────────────────────────────────────────────
def test_outputresolution_defaults_and_e_code_property():
    resolution = OutputResolution(True, {"a": 1}, FailureCode.OK)
    assert (resolution.hint, resolution.used_repair, resolution.raw_result) == ("", None, "")
    assert resolution.partial is False and resolution.ladder_log == []
    # PINNED: `e_code` is the API-layer refusal code, and an E-code must never be empty — so even
    # the non-failure OK member resolves to the generic bad-output code (never "").
    assert resolution.e_code == E_AGENT_BAD_OUTPUT
    assert FAILURE_TO_E_CODE[FailureCode.OK] == ""


@pytest.mark.parametrize(("code", "want"), [
    (FailureCode.CLI_BINARY_MISSING, E_CLI_MISSING),
    (FailureCode.CLI_AUTH_FAILED, E_CLI_EXIT),
    (FailureCode.CLI_TIMEOUT, E_CLI_TIMEOUT),
    (FailureCode.CLI_NONZERO_EXIT, E_CLI_EXIT),
    (FailureCode.CLI_STDOUT_NOT_JSON, E_CLI_JSON),
    (FailureCode.ENVELOPE_IS_ERROR, E_CLI_EXIT),
    (FailureCode.STRUCTURED_OUTPUT_MISSING, E_CLI_JSON),
    (FailureCode.MAX_TURNS, E_CLI_EXIT),
    (FailureCode.BUDGET_EXCEEDED, E_BUDGET_EXCEEDED),
    (FailureCode.STRUCTURED_RETRIES_EXHAUSTED, E_AGENT_BAD_OUTPUT),
    (FailureCode.SCHEMA_VALIDATION_FAILED, E_AGENT_BAD_OUTPUT),
    (FailureCode.REFUSAL, E_VALIDATION),
    (FailureCode.EMPTY_RESULT, E_CLI_JSON),
    (FailureCode.PARTIAL_RESULT, E_AGENT_BAD_OUTPUT),
    (FailureCode.POLICY_BLOCKED, E_SANDBOX_BLOCKED),
    (FailureCode.INJECTION_FLAGGED, E_POLICY_DENIED),
    (FailureCode.KILL_SWITCH, E_POLICY_DENIED),
    (FailureCode.UNKNOWN, E_CLI_JSON),
])
def test_every_taxonomy_member_maps_to_its_frozen_surface_code(code, want):
    assert e_code_for(code) == want
    assert FAILURE_TO_E_CODE[code] == want


def test_unmapped_and_empty_mappings_never_surface_an_empty_e_code():
    """PINNED: `OK`/`DUPLICATE_SUPPRESSED` are deliberately `""` (no failure) and E117 belongs to
    the policy firewall; `e_code_for` must still never hand an API layer an empty code."""
    assert FAILURE_TO_E_CODE[FailureCode.OK] == ""
    assert FAILURE_TO_E_CODE[FailureCode.DUPLICATE_SUPPRESSED] == ""
    assert e_code_for(FailureCode.OK) == E_AGENT_BAD_OUTPUT
    assert e_code_for(FailureCode.DUPLICATE_SUPPRESSED) == E_AGENT_BAD_OUTPUT
    assert FailureCode.RATE_CAPPED.value == "POLSIA-E117"
    assert e_code_for(FailureCode.RATE_CAPPED) == E_AGENT_BAD_OUTPUT   # firewall-owned member


def test_every_failure_member_has_a_remediation_row():
    missing = [c.value for c in FailureCode
               if c not in (FailureCode.OK, FailureCode.RATE_CAPPED) and c not in REMEDIATION]
    assert missing == []
    assert all(REMEDIATION[c].strip() for c in REMEDIATION)


def test_to_polsia_error_carries_the_code_detail_hint_and_context():
    resolution = OutputResolution(False, None, FailureCode.MAX_TURNS, detail="terminal_reason=x",
                                  used_repair="re_prompt", raw_result="y" * 900,
                                  ladder_log=["R0->POLSIA-E107"], hint="")
    error = resolution.to_polsia_error(agent_type="ads_agent")
    assert isinstance(error, PolsiaError)
    assert error.code == E_CLI_EXIT
    assert "ads_agent failed: POLSIA-E107 terminal_reason=x" in str(error)
    assert error.how_to_fix == REMEDIATION[FailureCode.MAX_TURNS]
    assert error.context == {
        "failure_code": "POLSIA-E107",
        "detail": "terminal_reason=x",
        "used_repair": "re_prompt",
        "ladder": ["R0->POLSIA-E107"],
        "raw_result": "y" * 500,
    }


def test_to_polsia_error_prefers_an_explicit_hint_and_defaults_the_agent_name():
    resolution = OutputResolution(False, None, FailureCode.UNKNOWN, hint="call the human")
    assert resolution.to_polsia_error().how_to_fix == "call the human"
    assert "agent failed: POLSIA-E119" in str(resolution.to_polsia_error())
    # a member with no REMEDIATION row yields an empty hint rather than a crash
    assert OutputResolution(False, None, FailureCode.RATE_CAPPED).to_polsia_error().how_to_fix == ""


def test_to_polsia_error_on_an_OK_resolution_is_never_a_success_shaped_error():
    error = OutputResolution(True, {"a": 1}, FailureCode.OK).to_polsia_error(agent_type="a")
    assert error.code == E_AGENT_BAD_OUTPUT      # never an empty code, never a fake success


def test_usage_from_envelope_raw_keeps_the_accounting_subobjects_verbatim():
    full = usage_from_envelope_raw({
        "usage": {"input_tokens": 1}, "modelUsage": {"m": {"costUSD": 0.1}},
        "total_cost_usd": 0.1, "duration_ms": 5, "num_turns": 2, "session_id": "s",
        "subtype": "success", "is_error": False, "terminal_reason": "done",
        "permission_denials": [{"tool": "Bash"}],
    })
    assert full == {
        "usage": {"input_tokens": 1}, "modelUsage": {"m": {"costUSD": 0.1}},
        "total_cost_usd": 0.1, "duration_ms": 5, "num_turns": 2, "session_id": "s",
        "subtype": "success", "is_error": False, "terminal_reason": "done",
        "permission_denials": [{"tool": "Bash"}],
    }
    empty = usage_from_envelope_raw({})
    assert empty["usage"] == {} and empty["modelUsage"] == {}
    assert empty["permission_denials"] == []
    assert all(empty[k] is None for k in ("total_cost_usd", "duration_ms", "num_turns", "session_id",
                                         "subtype", "is_error", "terminal_reason"))
    assert usage_from_envelope_raw({"model_usage": {"m": 1}})["modelUsage"] == {"m": 1}
    nulls = usage_from_envelope_raw({"usage": None, "modelUsage": None, "permission_denials": None})
    assert nulls["usage"] == {} and nulls["modelUsage"] == {} and nulls["permission_denials"] == []
    # verbatim means verbatim: a non-dict usage object is not "cleaned up" here
    assert usage_from_envelope_raw({"usage": [1, 2]})["usage"] == [1, 2]


def test_the_module_still_exports_the_frozen_surface():
    for name in ("FAILURE_TO_E_CODE", "FailureCode", "OutputResolution", "REMEDIATION",
                 "RepairPolicy", "classify_error_envelope", "coerce_types", "e_code_for",
                 "extract_envelope", "extract_json_from_text", "policy_from_settings",
                 "prompt_repair_message", "resolve_output", "run_repair_ladder",
                 "usage_from_envelope_raw"):
        assert name in structured_output.__all__
        assert hasattr(structured_output, name)
    # every failure member keeps its stable POLSIA code (never renumbered)
    assert [c.value for c in FailureCode][:4] == ["POLSIA-E000", "POLSIA-E100",
                                                  "POLSIA-E101", "POLSIA-E102"]


# ─────────────────────────────────────────────────────────────────────────────
# 7. the real provider seam — an envelope from deepseek_runtime still resolves
# ─────────────────────────────────────────────────────────────────────────────
def test_the_deepseek_envelope_shape_resolves_through_the_real_path():
    """The HTTP runtime feeds this module: `structured_output` only when the content parsed as an
    object, the raw content in `result` otherwise (deepseek_runtime L685-693)."""
    parsed = claude_cli.InvokeResult(
        stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "result": '{"flagged": true}', "structured_output": {"flagged": True},
                           "session_id": "polsia:1:test", "usage": {"input_tokens": 1}}),
        stderr="", exit_code=0, wall_seconds=0.01, transport="http")
    envelope = extract_envelope(parsed.stdout)
    resolution = resolve_output(stdout=parsed.stdout, exit_code=parsed.exit_code,
                                validator=_FlagModel.model_validate, schema_name="test")
    assert resolution.ok and resolution.used_repair is None
    assert envelope["session_id"] == "polsia:1:test"

    prose = claude_cli.InvokeResult(
        stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False,
                           "result": 'here you go:\n```json\n{"flagged": true,}\n```'}),
        stderr="", exit_code=0, wall_seconds=0.01, transport="http")
    repaired = resolve_output(stdout=prose.stdout, exit_code=0,
                              validator=_FlagModel.model_validate, schema_name="test")
    assert repaired.ok and repaired.used_repair == "text_extract"
    assert repaired.payload == {"flagged": True}
