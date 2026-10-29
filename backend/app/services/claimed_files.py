"""F-15 (wave 3) — a run may only claim the files it really wrote.
F-W5-F21-1 (wave 5) — a run may only claim a character count its payload really measures.

Measured defect (VISUALS-2 F-15, live twice):

  * task #302's result said it "wrote `notes/302-logo-and-label-spec.md` in the local code
    workspace".  The path did not exist anywhere on the box, and the run's own ledger had no
    `code_write` call — the task was COMPLETED;
  * task #312 (`business_planning`) said *"Decision memo (task 312) written to
    `notes/312-candle-vs-soap-decision-memo.md`"* — an agent with NO write tool at all, no file,
    COMPLETED.

An owner cannot tell a fabricated deliverable from a lost one, which is the whole debate: the file
either exists in the run's workspace or the claim is false.

Measured defect (W4-F21-1, live once — task 327 / run 319):

  * the social run's summary said *"exactly 320 characters long … Character count verified at
    320"* and its notes repeated *"Tweet is exactly 320 characters"*;
  * the stored `posts[0].content` is **265** characters.  The product had no check between a run's
    self-reported artifact size and the artifact, so a false measurement rode to the owner as a
    verification sentence.

WHAT THIS MODULE DOES (and what it deliberately does not)

  * it READS the run's own human-facing text (`summary` + `notes`) for path-like tokens with a
    file extension, and checks each one against `<code_workspace_dir>/runs/<run_id>/` — the exact
    tree `code_write` writes into and `artifact_service` serves (this module uses that service's
    listing/containment rules rather than re-implementing path safety);
  * it READS the same text for an exact character-count ASSERTION about what the run produced and
    compares it with the lengths of the run's own deliverable text fields (a bounded key set,
    `DELIVERABLE_TEXT_KEYS`): an assertion no stored payload measures is LABELLED and recorded
    under `claimed_lengths`.  Five sentence SHAPES are not assertions about the output and are
    recorded as ignored, because a false alarm would be its own dishonesty: a platform limit
    ("the 280-character platform limit"), a BOUND ("all captions are well under 280 characters"), a
    task requirement ("the brief asks for exactly 320 characters"), a REPAIR REPORT ("was 372 chars,
    cut to 274"), and a measurement of FETCHED content ("the pricing page returned 197 characters of
    banner text") — the bound, repair and external shapes were found on the real corpus (runs 315,
    the social fitter's notes, and 299/282/238/317 on this install);
  * a claimed path that is NOT in the workspace is LABELLED in the very text the owner reads
    (`summary` gains one bracketed sentence, `notes` gains one line) and recorded as structured
    data under `claimed_files` on the stored output.  A claim is never silently deleted: the run
    said it, and the record must show both the claim and the check;
  * it never invents a path, never searches the whole filesystem, and never treats "the workspace
    is missing" as "the file may be somewhere else": no workspace means nothing was written by
    `code_write`, which is exactly what the label says;
  * a run that claims NO path and asserts NO count is untouched — no new keys, byte-identical
    output (the same rule the wave-2 cost guard follows);
  * an UNVERIFIABLE claim (a truncated listing, an unreadable workspace, a count assertion with no
    identifiable payload) is reported as `unverified`, not as missing: "could not check" must
    never read as "did not write" / "the count is false".

The seam is `BaseMeridianAgent.timed_run` (every dispatched run, planner runs included), which is
where the run's `summary` is finalised before the worker copies it into `tasks.result_summary`.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.errors import MeridianError
from app.services import artifact_service

#: The result key carrying the file audit.  Written ONLY when the run claimed at least one path, so a
#: run with no claims keeps its historical output key set.
CLAIMED_FILES_KEY = "claimed_files"

#: The result key carrying the character-count audit (W4-F21-1).  Written ONLY when the run's text
#: asserts at least one character count, so a run that asserts none keeps its key set.
CLAIMED_LENGTHS_KEY = "claimed_lengths"

#: The label a summary gains when it claimed a file that is not there (and, with its own wording,
#: when it asserted a character count the payload does not measure).  Bracketed and unmissable:
#: it rides the same text the false claim was in.
CLAIM_LABEL_OPEN = "[CLAIM CHECK:"

#: The opening of the LENGTH label's sentence.  Its full text is built by `_length_label` and
#: recognised by `_LABEL_BLOCK`, so a second audit pass never reads its own sentence as a claim.
LENGTH_LABEL_OPEN = f"{CLAIM_LABEL_OPEN} this run's text asserts"

#: Any label this module already appended.  Stripped before scanning, so a re-audit of a labelled
#: result never treats its own sentence as a model claim.
_LABEL_BLOCK = re.compile(r"\[CLAIM CHECK:[^\]]*\]", re.IGNORECASE)

#: Keys whose string values are DELIVERABLE TEXT — the payloads a character-count assertion can be
#: about.  Deliberately a bounded list: the check may only CONTRADICT a claim when it can see a
#: payload, and a claim with no identifiable payload is `unverified`, never "false".
DELIVERABLE_TEXT_KEYS = frozenset({
    "content", "body", "text", "caption", "copy", "message", "draft", "post", "tweet", "email",
    "article", "markdown", "html", "description", "excerpt", "headline", "subject", "notes_text",
})

#: Keys whose LIST OF STRINGS are deliverable texts (`drafts: ["…"]`, `tweets: ["…"]`).
DELIVERABLE_TEXT_LIST_KEYS = frozenset({
    "drafts", "tweets", "captions", "bodies", "texts", "articles", "variants", "responses",
    "replies", "emails", "posts",
})

#: A payload candidate shorter than this is not treated as a deliverable (ids, slugs, platform
#: names and one-word cells would otherwise be counted as artifacts).
PAYLOAD_MIN_CHARS = 20

#: How deep the payload walk goes.  A plan output nests posts under a run result; nothing in the
#: shipped schemas needs more than this, and an unbounded walk would scan megabytes per run.
PAYLOAD_WALK_DEPTH = 4

#: One count assertion: "320 characters", "320-char", "320 characters long".  The UNIT word is
#: required, so a bare number beside the word "limit" ("over the 280 limit") is never a claim.
_COUNT_TOKEN = re.compile(r"(?<![\w.])(\d{1,7})[\s-]*(?:characters?|chars?)(?![\w])", re.IGNORECASE)

#: The same assertion with the unit BEFORE the number: "Character count verified at 320".
_COUNT_PHRASE = re.compile(r"(?:character|char)\s+count[^.;\d]{0,30}?(\d{1,7})", re.IGNORECASE)

#: Phrase-level LIMIT statements — the number is the platform's bound, not this run's output.
#: Vocabulary includes what a model actually writes about a bound: "allows 280 characters",
#: "up to 2200 characters", "280 characters max", "the limit is 280".
_LIMIT_AFTER = re.compile(
    r"^\s*(?:characters?\s+)?(?:platform\s+)?(?:limit|maximum|max|cap|ceiling|guidance|allowed|"
    r"permitted|per\s+post|per\s+tweet|or\s+(?:less|fewer|more|under))\b",
    re.IGNORECASE,
)
_LIMIT_PHRASES = (
    re.compile(r"\b(?:limit|maximum|max|cap|ceiling|allow(?:s|ed|ing)?|permit(?:s|ted|ting)?|"
               r"up\s+to)\b[^.;\d]{0,20}(\d{1,7})\b", re.IGNORECASE),
    re.compile(r"(\d{1,7})\s*(?:characters?|chars?)?\s*(?:is|are)?\s*(?:the\s+)?(?:platform\s+)?"
               r"(?:limit|maximum|max|cap|ceiling|allowed|permitted)\b", re.IGNORECASE),
)

#: Phrase-level TASK/BRIEF requirements — the number is what the instruction asked for, not what
#: this run produced.  Run 319's own note ("…as the drill requires") comes AFTER its assertion, so
#: it does not excuse the claim; only a requirement phrase that CONTAINS the number does.
_TASK_PHRASES = (
    re.compile(r"\b(?:task|brief|prompt|instruction|request|requirement|spec|drill)s?\b"
               r"[^.;]{0,80}?(?:asks?|asked|requires?|required|instructs?|specif(?:y|ies|ied)|"
               r"calls?\s+for|demands?|says?)\b[^.;\d]{0,25}(\d{1,7})", re.IGNORECASE),
    re.compile(r"\b(?:asked|required|requested|instructed|specified|wanted)\s+(?:for\s+)?"
               r"(?:exactly\s+)?(\d{1,7})\b", re.IGNORECASE),
    re.compile(r"(\d{1,7})\s*(?:characters?|chars?)\s+(?:as|which|that)\s+(?:the\s+)?"
               r"(?:task|brief|prompt|drill|instructions?|requirements?)\b", re.IGNORECASE),
)

#: Phrase-level BOUND statements — "well under 280 characters", "under 1000 characters", "at most
#: 2200 chars".  A bound is not an exact count, so it can never be "contradicted" by measuring the
#: payload; measuring it against a bound would be a DIFFERENT check.  Found on the real corpus: run
#: 315 writes "all captions are well under 280 characters for X, and the longer drafts … under 1000
#: characters" — labelling that run would have been a false alarm about an honest sentence.
_BOUND_PHRASES = (
    re.compile(r"\b(?:well\s+)?(?:under|over|below|above|within|exceeds?|exceeded|at\s+most|"
               r"at\s+least|less\s+than|more\s+than|up\s+to|max(?:imum)?)\s+"
               r"(?:about\s+|roughly\s+|approximately\s+|~\s*)?(\d{1,7})\b", re.IGNORECASE),
    re.compile(r"(\d{1,7})\s*(?:characters?|chars?)\s*(?:or\s+(?:less|fewer|more|under))\b",
               re.IGNORECASE),
)

#: REPAIR-REPORT markers — the sentence describes a payload that was CUT/REPLACED, so neither number
#: is the current deliverable ("twitter post #0 was 372 chars, over the 280-char platform limit; it
#: was cut at a word boundary to 274 chars" — the wave-3 social fitter's own note).  The stored
#: payload is the FITTED text, so labelling the honest repair report would be a false alarm.  Bare
#: "fit" is deliberately NOT a marker: run 319's false claim sits in a sentence that ends "not fit
#: for publication", and the fitter note says "fitted".
_REPAIR_MARKERS = re.compile(
    r"\b(?:cut|trimmed|shortened|cropped|fitted|repaired|reduced)\b", re.IGNORECASE)

#: EXTERNAL-measurement markers — the count describes something the run FETCHED, not something it
#: wrote ("the pricing page returned 197 characters of banner text").  Found on the real corpus:
#: four competitor runs report the length of a fetched page/excerpt.
_EXTERNAL_MARKERS = re.compile(
    r"\b(?:returned|returns?|fetched|fetch|page|pages|url|urls|http|site|website|excerpt|"
    r"truncated|response|title\s+tag|html|banner|shell|body\s+text)\b", re.IGNORECASE)

#: OUTPUT markers that keep an external-sounding sentence about OUR deliverable ("the fetched brief's
#: caption is 280 characters") a claim.
_OUTPUT_MARKERS = re.compile(
    r"\b(?:drafted|draft|wrote|written|delivered|tweet|caption|post|posted|email|copy|"
    r"headline)\b", re.IGNORECASE)

#: Extensions a text deliverable can have.  A path token WITHOUT one of these is not treated as a
#: file claim (so "api/v1/tasks", "10/28/2026" and "and/or" never become claims).
CLAIM_EXTS = (
    "md", "markdown", "txt", "csv", "tsv", "json", "yaml", "yml", "html", "htm", "svg", "png",
    "jpg", "jpeg", "pdf", "py", "sh", "js", "ts", "tsx", "jsx", "sql", "xml", "docx", "xlsx",
)

#: One path-like token: at least one `/`, a known extension, and a bounded character set.  Quotes,
#: backticks and trailing sentence punctuation are stripped afterwards (models wrap paths in all of
#: them, and a claim mid-sentence ends with a full stop).  The trailing lookahead excludes letters,
#: digits and separators — but NOT `.`, so `wrote notes/a.md.` is still a claim of `notes/a.md`.
_PATH_TOKEN = re.compile(
    r"(?<![\w/.-])((?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.(?:" + "|".join(CLAIM_EXTS) + r"))(?![\w/-])",
    re.IGNORECASE,
)

#: Prefixes a claim may carry that are NOT part of the run-relative path.
_WORKSPACE_PREFIXES = ("data/workspace/", "./data/workspace/")


def _clean(token: str) -> str:
    """A raw token with wrapping quotes/backticks and sentence punctuation removed.

    A trailing `.` is stripped (a claim mid-sentence ends with a full stop) but leading dots are
    KEPT: `../x.md` must stay a traversal claim the audit can report, not silently become `x.md`.
    """
    text = token.strip().strip("`'\"()[]<>,;:«»“”‘’")
    return text.rstrip(".") if text.endswith(".") and not text.endswith("..") else text


def normalise_claimed_path(token: str) -> str:
    """A claimed token as a RUN-RELATIVE posix path (or "" when it names nothing usable).

    `runs/<id>/x.md`, `data/workspace/runs/<id>/x.md` and `./x.md` all normalise to the same
    run-relative path, because the claim is about a file IN THE RUN WORKSPACE either way.  A
    leading `../` or an absolute path is kept as-is so the audit can say "outside the workspace"
    instead of pretending it matched.
    """
    text = _clean(token).replace("\\", "/")
    if not text or "://" in text or any(ch.isspace() for ch in text):
        return ""
    while text.startswith("./"):
        text = text[2:]
    for prefix in _WORKSPACE_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    lowered = [part.lower() for part in parts]
    if "runs" in lowered:
        index = lowered.index("runs")
        # `runs/<id>/<path…>` (and a bare `runs/<id>`) is run-relative from <path…> on.
        remainder = parts[index + 2:]
        if remainder:
            return "/".join(remainder)
        return ""
    return "/".join(parts)


def extract_claimed_paths(text: str) -> list[str]:
    """Every distinct run-relative path a piece of run text CLAIMS, in first-seen order."""
    if not isinstance(text, str) or not text:
        return []
    seen: list[str] = []
    for match in _PATH_TOKEN.finditer(text):
        path = normalise_claimed_path(match.group(1))
        if path and path not in seen:
            seen.append(path)
    return seen


def _claim_texts(result: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    summary = result.get("summary")
    if isinstance(summary, str):
        texts.append(summary)
    notes = result.get("notes")
    if isinstance(notes, list):
        texts.extend(str(note) for note in notes if isinstance(note, str))
    return texts


def _label_summary(summary: str, missing: list[str]) -> str:
    """`summary` + ONE bracketed sentence naming the claims the workspace does not back."""
    shown = ", ".join(missing[:3])
    more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
    label = (
        f"{CLAIM_LABEL_OPEN} this run named {shown}{more}, but no such file exists in this run's "
        "workspace — the path was NOT written by this run]"
    )
    if CLAIM_LABEL_OPEN in summary:
        return summary
    return f"{summary.rstrip()} {label}".strip()


def audit_claimed_files(
    *, run_id: Any, result: dict[str, Any], root: str | None = None
) -> dict[str, Any]:
    """Check every file path the run's text claims against its OWN workspace; label what is absent.

    Returns the audit record (also written to `result[CLAIMED_FILES_KEY]` when there is anything
    to say) and MUTATES `result` in place: the summary gains its label and `notes` gains one line,
    because the summary is what the owner reads (`tasks.result_summary`).

    `{}` when the run claims no path, when the run has no id (an in-process probe, a planner step
    with no `agent_runs` row), or when the result carries no text.
    """
    if not isinstance(result, dict):
        return {}
    claimed: list[str] = []
    for text in _claim_texts(result):
        for path in extract_claimed_paths(text):
            if path not in claimed:
                claimed.append(path)
    if not claimed:
        return {}
    run_key = "" if run_id is None else str(run_id).strip()
    if not run_key:
        # No run identity => no workspace to check, and a guess would be worse than a label.
        return _record(result, {}, claimed, run_id="", truncated=False, unreadable=True)

    try:
        listing = artifact_service.list_run_artifacts(run_key, root)
    except MeridianError as exc:
        # E_NOT_FOUND: the run has no workspace at all (nothing was written).  E_CONFIG/E_VALIDATION/
        # E_STORAGE: the workspace cannot be read or is not a run workspace — UNVERIFIABLE, which is
        # not the same claim as "not written".
        written: dict[str, int] = {}
        if exc.code in ("E_NOT_FOUND", "E_VALIDATION"):
            return _record(result, written, claimed, run_id=run_key, truncated=False,
                           unreadable=False)
        return _record(result, written, claimed, run_id=run_key, truncated=False, unreadable=True)

    written = {entry.path: entry.size for entry in listing.files}
    return _record(
        result, written, claimed, run_id=run_key, truncated=listing.truncated, unreadable=False
    )


def _record(
    result: dict[str, Any],
    written: dict[str, int],
    claimed: list[str],
    *,
    run_id: str,
    truncated: bool,
    unreadable: bool,
) -> dict[str, Any]:
    """Build, persist and LABEL the audit.  One truth per claim: found / missing / unverified."""
    claims: list[dict[str, Any]] = []
    missing: list[str] = []
    unverified: list[str] = []
    for path in claimed:
        if path in written:
            claims.append({"path": path, "status": "written", "bytes": written[path]})
        elif truncated or unreadable:
            claims.append({"path": path, "status": "unverified"})
            unverified.append(path)
        else:
            claims.append({"path": path, "status": "missing"})
            missing.append(path)
    record: dict[str, Any] = {
        "run_id": run_id,
        "workspace": "unreadable" if unreadable else ("truncated" if truncated else "read"),
        "claims": claims,
        "missing": missing,
        "unverified": unverified,
    }
    result[CLAIMED_FILES_KEY] = record
    if missing:
        summary = str(result.get("summary") or "")
        result["summary"] = _label_summary(summary, missing)
        notes = result.get("notes")
        notes = [str(note) for note in notes] if isinstance(notes, list) else []
        line = (
            f"claim check: this run's text named {len(missing)} file(s) that are NOT in its run "
            f"workspace ({', '.join(missing[:5])}) — a claimed deliverable is only real when the "
            "run workspace holds it"
        )
        if line not in notes and len(notes) < 20:
            notes.append(line)
            result["notes"] = notes
    return record


# ---------------------------------------------------------------------------
# F-W5-F21-1 — a claimed character count must match the payload it describes
# ---------------------------------------------------------------------------

def _scan_texts(result: dict[str, Any]) -> list[str]:
    """The run's human-facing text, with this module's own labels/notes removed.

    Removing them makes the audit IDEMPOTENT (a re-audit of an already-labelled result must not
    read its own sentence as a model assertion) and keeps the record deterministic.
    """
    texts: list[str] = []
    summary = result.get("summary")
    if isinstance(summary, str):
        texts.append(_LABEL_BLOCK.sub(" ", summary))
    notes = result.get("notes")
    if isinstance(notes, list):
        for note in notes:
            if not isinstance(note, str):
                continue
            lowered = note.strip().lower()
            if lowered.startswith(("claim check:", "length check:")):
                continue
            texts.append(_LABEL_BLOCK.sub(" ", note))
    return texts


def _bounded(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _span_for(text: str, start: int, end: int) -> tuple[str, int]:
    """The sentence-ish span containing [start,end) plus the match's END offset inside it."""
    terminators = ".;!?\n"
    left = max(text.rfind(ch, 0, start) for ch in terminators) + 1
    rights = [text.find(ch, end) for ch in terminators]
    rights = [pos for pos in rights if pos != -1]
    right = min(rights) + 1 if rights else len(text)
    return text[left:right], end - left


def _numbers_in(patterns: tuple[re.Pattern[str], ...], text: str) -> set[int]:
    """Every number captured by `patterns` in `text` (patterns capture digits only)."""
    found: set[int] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            found.add(int(match.group(1)))
    return found


def _is_limit_reference(span: str, number: int, after: str) -> bool:
    """True when the number is the PLATFORM's bound ("280 characters max", "limit is 280")."""
    if _LIMIT_AFTER.match(after):
        return True
    return number in _numbers_in(_LIMIT_PHRASES, span)


def _is_task_requirement(span: str, number: int) -> bool:
    """True when the number is what the TASK asked for ("the brief asks for exactly 320")."""
    return number in _numbers_in(_TASK_PHRASES, span)


def _is_bound_statement(span: str, number: int) -> bool:
    """True when the number is a BOUND ("under 280 characters"), not an exact count."""
    return number in _numbers_in(_BOUND_PHRASES, span)


def _is_external_measurement(span: str, number: int) -> bool:
    """True when the count describes a FETCHED page/excerpt rather than the run's own deliverable."""
    if number not in _numbers_in((_COUNT_TOKEN, _COUNT_PHRASE), span):
        return False
    if _OUTPUT_MARKERS.search(span):
        return False
    return bool(_EXTERNAL_MARKERS.search(span))


def _is_repair_report(span: str, lookahead: str = "") -> bool:
    """True when the sentence reports a CUT/REPLACED payload rather than the current deliverable.

    The marker may sit in the SAME sentence ("was cut at a word boundary to 274 chars") or in the
    sentence that immediately follows ("was 372 chars …; it was cut … to 274 chars").  A marker in
    the lookahead only counts when that lookahead also carries another character count: a bare
    "I cut it" after a size claim does not turn the claim into a repair report.
    """
    if _REPAIR_MARKERS.search(span):
        return True
    if not lookahead or not _REPAIR_MARKERS.search(lookahead):
        return False
    return bool(_COUNT_TOKEN.search(lookahead) or _COUNT_PHRASE.search(lookahead))


def _count_assertions(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(claims, ignored)` — exact character counts asserted about the run's own output.

    A count that reads as a platform limit, as a BOUND ("under 280 characters"), as what the task
    demanded, as a REPAIR REPORT ("was 372 chars … was cut to 274"), or as a measurement of a FETCHED
    page is recorded as IGNORED (with the reason) instead of being compared to the payload: the check
    must never manufacture a contradiction out of a true sentence.  Only an exact assertion about the
    run's own deliverable survives to be judged.
    """
    claims: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for pattern in (_COUNT_TOKEN, _COUNT_PHRASE):
        for match in pattern.finditer(text):
            number = int(match.group(1))
            span, rel_end = _span_for(text, match.start(), match.end())
            after = span[rel_end:]
            cleaned = _bounded(span)
            if _is_limit_reference(span, number, after):
                ignored.append({"asserted": number, "why": "platform_limit", "span": cleaned})
                continue
            if _is_bound_statement(span, number):
                ignored.append({"asserted": number, "why": "bound_statement", "span": cleaned})
                continue
            if _is_task_requirement(span, number):
                ignored.append({"asserted": number, "why": "task_requirement", "span": cleaned})
                continue
            if _is_repair_report(span, text[match.end(): match.end() + 160]):
                ignored.append({"asserted": number, "why": "repair_report", "span": cleaned})
                continue
            if _is_external_measurement(span, number):
                ignored.append({"asserted": number, "why": "external_measurement",
                                "span": cleaned})
                continue
            claims.append({"asserted": number, "span": cleaned})
    return claims, ignored


def _payload_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The run's own deliverable texts, `[{path, chars, aliases}]`, deduplicated by VALUE.

    The walk is bounded in depth and in keys ON PURPOSE.  The check may only CONTRADICT an
    assertion when it can see a payload; a text the walk cannot identify leaves the assertion
    `unverified` rather than "false".
    """
    found: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    def walk(value: Any, path: str, key: str, depth: int) -> None:
        if depth > PAYLOAD_WALK_DEPTH:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                child_path = f"{path}.{child_key}" if path else str(child_key)
                walk(child, child_path, str(child_key), depth + 1)
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", key, depth + 1)
            return
        if not isinstance(value, str):
            return
        if key not in DELIVERABLE_TEXT_KEYS and key not in DELIVERABLE_TEXT_LIST_KEYS:
            return
        if len(value) < PAYLOAD_MIN_CHARS:
            return
        existing = seen.get(value)
        if existing is not None:
            if path not in existing["aliases"]:
                existing["aliases"].append(path)
            return
        entry = {"path": path, "chars": len(value), "aliases": [path]}
        seen[value] = entry
        found.append(entry)

    walk(result, "", "", 0)
    return found


def _length_label(contradicted: list[int], payloads: list[dict[str, Any]]) -> str:
    """ONE bracketed sentence naming the asserted count(s) and the measured payload(s)."""
    asserted = ", ".join(str(number) for number in contradicted[:3])
    more = f" and {len(contradicted) - 3} more" if len(contradicted) > 3 else ""
    measured = ", ".join(
        f"{entry['chars']} characters ({entry['path']})" for entry in payloads[:3]
    ) or "no readable deliverable text"
    return (
        f"{LENGTH_LABEL_OPEN} {asserted}{more} characters; no stored deliverable text of this run "
        f"measures it — the stored text measures {measured}. A claimed length is only real when "
        "the payload measures it]"
    )


def audit_claimed_lengths(*, result: dict[str, Any]) -> dict[str, Any]:
    """Check every exact character count the run's text ASSERTED against its own payload.

    Returns the audit record (also written to `result[CLAIMED_LENGTHS_KEY]` when the run asserted
    at least one count) and MUTATES `result` in place: the summary gains ONE bracketed sentence and
    `notes` gains one line when an assertion is CONTRADICTED — because the summary is what the
    owner reads (`tasks.result_summary`).

    Per assertion the status is exactly one of:
      * `matched`      — a stored deliverable text measures the asserted count (no label);
      * `contradicted` — a deliverable text exists and NONE measures it (labelled);
      * `unverified`   — no identifiable deliverable text to check against (never labelled: "could
        not check" must not read as "the count is false").

    `{}` when the run asserts no character count at all (byte-identical output), which is the
    ordinary case for every non-copy agent.
    """
    if not isinstance(result, dict):
        return {}
    claims: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for text in _scan_texts(result):
        text_claims, text_ignored = _count_assertions(text)
        claims.extend(text_claims)
        ignored.extend(text_ignored)
    if not claims and not ignored:
        return {}

    payloads = _payload_candidates(result)
    judged: list[dict[str, Any]] = []
    contradicted: list[int] = []
    for claim in claims:
        asserted = int(claim["asserted"])
        measured = [{"path": entry["path"], "chars": entry["chars"], "aliases": entry["aliases"]}
                    for entry in payloads]
        if any(entry["chars"] == asserted for entry in payloads):
            status = "matched"
        elif payloads:
            status = "contradicted"
        else:
            status = "unverified"
        judging = {"asserted": asserted, "status": status, "span": claim["span"]}
        if status == "contradicted":
            judging["measured"] = measured
            if asserted not in contradicted:
                contradicted.append(asserted)
        judged.append(judging)

    record: dict[str, Any] = {
        "claims": judged,
        "ignored": ignored,
        "payloads": [{"path": entry["path"], "chars": entry["chars"],
                      "aliases": entry["aliases"]} for entry in payloads],
    }
    result[CLAIMED_LENGTHS_KEY] = record
    if not contradicted:
        return record

    summary = str(result.get("summary") or "")
    if LENGTH_LABEL_OPEN not in summary:
        result["summary"] = f"{summary.rstrip()} {_length_label(contradicted, payloads)}".strip()
    notes = result.get("notes")
    notes = [str(note) for note in notes] if isinstance(notes, list) else []
    measured_text = ", ".join(f"{entry['chars']} ({entry['path']})" for entry in payloads[:5])
    line = (
        f"length check: this run's text asserted {', '.join(str(n) for n in contradicted)} "
        f"characters; its stored deliverable text measures {measured_text} — the claimed count was "
        "NOT verified by the payload"
    )
    if line not in notes and len(notes) < 20:
        notes.append(line)
        result["notes"] = notes
    return record


__all__ = [
    "CLAIMED_FILES_KEY",
    "CLAIMED_LENGTHS_KEY",
    "CLAIM_EXTS",
    "CLAIM_LABEL_OPEN",
    "DELIVERABLE_TEXT_KEYS",
    "DELIVERABLE_TEXT_LIST_KEYS",
    "LENGTH_LABEL_OPEN",
    "PAYLOAD_MIN_CHARS",
    "audit_claimed_files",
    "audit_claimed_lengths",
    "extract_claimed_paths",
    "normalise_claimed_path",
]
