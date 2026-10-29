"""The copy CLAIM vocabulary this brand may publish — and a census that proves the data obeys it.

WHY THIS EXISTS (wave-5 TOP DATA/COPY ITEM; `bridge/BRAND-KIT.md` §8 is the authority).

The live company profile and the generated Cinder & Lye site once carried claims the supplier does
not support — *"a real net weight and a real safety block on every unit"* (UNVERIFIED until R1) and
*"poured in Los Angeles"* — and the contact page once published the owner's home address and legal
name (F-38).  The copy was corrected as DATA (one `PUT /api/v1/config` + a site regeneration), so
the correction is only as durable as the next write: nothing in the product stops an agent, a
prompt, or the owner's own edit from putting the old claims back.  This module is the missing
vocabulary.  It does not generate copy, rewrite a page or redact anything — it *classifies* text
against three lists and says where a claim or a private value was found:

* ``banned`` — FALSE for this supplier/product, so it must never be published
  (`BRAND-KIT §8`: coconut-soy, a reusable tin, hand-poured / small-batch / poured by us,
  non-toxic / clean-burning / eco-friendly, "our formula", and — until R1 is proven — ASTM /
  compliant / fire-safe / a printed safety block);
* ``unverified`` — a claim that needs a human sign-off before it can be stated as fact
  (the `BRAND-KIT` H-register: a net-weight assertion H3, "cold-process" soap H4 — ALLOWED as a
  PLAN-level descriptor since wave 6 because it is the supplier's own published `product_type`
  (W5-S1/S2) and the live sentences already read as future/conditional, BANNED as an offer until a
  soap SKU and a supplier contract exist — "Made in Arizona" H5, a burn-time claim that belongs to
  the supplier's product page);
* ``private`` — the owner's home address and legal name, the exact values F-38 exposed; a private
  match anywhere is a publication blocker regardless of which field it sits in.

The ALLOWED list is carried beside them as executable documentation: the strings the supplier's
own pages support (``soy candle``, ``100% natural soy wax``, ``cotton wick``, ``made to order``,
``printed and shipped from Chatsworth, CA``, ``our own label`` / ``our scent curation``).  Both
directions are pinned by ``tests/unit/test_w5_copy_census.py``: every banned term must fire on its
own recorded example, and every allowed example must census CLEAN — so the test fails if the
vocabulary drifts either way.

HONESTY RULES (the point of the module, in the tree's own idiom):

* the census never "fixes" text and never rewrites a finding into a pass; it reports
  ``(term, kind, where, field, excerpt)`` and the caller decides;
* a private match is reported MASKED — the finding proves the exposure without reproducing the
  value in a log, a receipt or an agent prompt (AGENTS.md §27);
* absence of a finding is not a claim that copy is *good*, only that it is clean of THIS
  vocabulary — the report says exactly that;
* the vocabulary is deliberately phrase-shaped, not vibe-shaped: ``same soy wax, same label,
  smaller pour`` is allowed copy and must not trip ``hand-poured``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Finding kinds.  `banned` and `private` block a publish; `unverified` is a review item.
BANNED = "banned"
UNVERIFIED = "unverified"
PRIVATE = "private"

#: The company-profile fields an agent prompt or a public page can render.  Pinned by a test: a new
#: copy-bearing field must be added here or it is silently un-scanned.
PROFILE_COPY_FIELDS: tuple[str, ...] = (
    "name",
    "mission",
    "vision",
    "description",
    "value_prop",
    "target_market",
    "product_type",
    "industry",
    "owner_goal",
    "owner_skills",
    "notification_email",
    "website_url",
    "sender_legal_name",
    "sender_postal_address",
    "pricing_model",
    "goals",
    "confirmed_idea",
)

#: The generated site's page files, in the order the generator writes them.
SITE_PAGE_NAMES: tuple[str, ...] = ("index.html", "about.html", "contact.html")


@dataclass(frozen=True)
class ClaimTerm:
    """One claim or value the census looks for, with its reason and its own positive example."""

    id: str
    kind: str
    pattern: str
    reason: str
    source: str
    #: A string that MUST produce a finding for this term (the test plants each one).
    example: str

    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One term found in one place.  `count` is occurrences, not distinct kinds."""

    term_id: str
    kind: str
    where: str
    field: str
    excerpt: str
    reason: str
    source: str
    count: int = 1
    row_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "term_id": self.term_id,
            "kind": self.kind,
            "where": self.where,
            "field": self.field,
            "excerpt": self.excerpt,
            "reason": self.reason,
            "source": self.source,
            "count": self.count,
        }
        if self.row_id is not None:
            data["row_id"] = self.row_id
        return data


# ── the vocabulary ───────────────────────────────────────────────────────────────────────────────
# Every pattern is a PHRASE the brand either cannot support or has not proven.  Sources are named so
# a future reader can re-check the decision instead of re-deriving it.

BANNED_TERMS: tuple[ClaimTerm, ...] = (
    ClaimTerm(
        id="coconut_soy",
        kind=BANNED,
        pattern=r"coconut[\s-]*(?:soy|wax)",
        reason=(
            "the supplier publishes 100% natural soy wax; a coconut-soy blend is a different "
            "supplier's product (Dreamship), so the claim is false for this line"
        ),
        source="BRAND-KIT §8 / W4-S1, W4-S2, W4-S12",
        example="Our coconut-soy blend burns slower.",
    ),
    ClaimTerm(
        id="tin_container",
        kind=BANNED,
        pattern=r"\btins?\b",
        reason=(
            "the supplier's candle catalogue is glass jars only; the only tin found is a 40-unit "
            "wholesale buy that is not this product"
        ),
        source="BRAND-KIT §8 / W4-S14",
        example="Comes in a reusable tin.",
    ),
    ClaimTerm(
        id="hand_poured",
        kind=BANNED,
        pattern=r"hand[\s-]*pou?r(?:ed|ing)",
        reason=(
            "the supplier pours on its own floor in Chatsworth; 'hand-poured' by this brand would "
            "be a false process claim"
        ),
        source="BRAND-KIT §8 / W4-S2, W4-S6",
        example="Hand-poured in small batches.",
    ),
    ClaimTerm(
        id="poured_by_us",
        kind=BANNED,
        pattern=r"pou?red\s+by\s+us|\bwe\s+(?:hand[\s-]*)?pou?r\b|\bour\s+own\s+pour\b",
        reason=(
            "this brand does not pour; the print-on-demand partner manufactures and ships, so a "
            "'we pour' claim is false"
        ),
        source="BRAND-KIT §8 / W4-S6",
        example="Every candle poured by us.",
    ),
    ClaimTerm(
        id="poured_in_la",
        kind=BANNED,
        pattern=r"pou?red\s+in\s+(?:los\s+angeles|\bla\b|l\.a\.)",
        reason=(
            "the allowed origin claim is 'printed and shipped from Chatsworth, California' / 'in the "
            "Los Angeles area'; 'poured in Los Angeles' asserts a process and a place we do not own"
        ),
        source="BRAND-KIT §8 / H5 / R1",
        example="Poured in Los Angeles.",
    ),
    ClaimTerm(
        id="small_batch",
        kind=BANNED,
        pattern=r"small[\s-]*batch(?:es)?",
        reason=(
            "the supplier is a print-on-demand operation with published lead times; 'small-batch' "
            "is a production-method claim the brand cannot make"
        ),
        source="BRAND-KIT §8",
        example="Small-batch candles, made with care.",
    ),
    ClaimTerm(
        id="non_toxic",
        kind=BANNED,
        pattern=r"non[\s-]*toxic",
        reason=(
            "these are the supplier's product-page statements; repeating them moves the legal "
            "exposure onto this brand with no attestation on file"
        ),
        source="BRAND-KIT §8 / W4-S2",
        example="Non-toxic and clean-burning.",
    ),
    ClaimTerm(
        id="clean_burning",
        kind=BANNED,
        pattern=r"clean[\s-]*burning",
        reason="the supplier's product-page statement, not this brand's to repeat",
        source="BRAND-KIT §8 / W4-S2",
        example="A clean-burning soy candle.",
    ),
    ClaimTerm(
        id="eco_friendly",
        kind=BANNED,
        pattern=r"eco[\s-]*friendly",
        reason="the supplier's product-page statement, not this brand's to repeat",
        source="BRAND-KIT §8 / W4-S2",
        example="Eco-friendly packaging.",
    ),
    ClaimTerm(
        id="our_formula",
        kind=BANNED,
        pattern=(
            r"\bour\s+(?:own\s+)?(?:unique\s+)?formula\b"
            r"|proprietary\s+(?:formula|blend)\b"
            r"|unique\s+formulation\b"
        ),
        reason="the wax and the scent are the supplier's, so 'our formula' is BANNED for P1",
        source="BRAND-KIT §8",
        example="Our formula blends two waxes.",
    ),
    ClaimTerm(
        id="astm",
        kind=BANNED,
        pattern=r"\bASTM\b|\bF2058\b|\bF2417\b",
        reason=(
            "ASTM F2058/F2417 text is paywalled and R1 is unproven; nothing may cite ASTM until a "
            "sample and a written supplier answer exist"
        ),
        source="BRAND-KIT §8 / H1 / R1",
        example="Compliant with ASTM F2058.",
    ),
    ClaimTerm(
        id="fire_safe",
        kind=BANNED,
        pattern=r"fire[\s-]*safe",
        reason="a safety claim blocked until R1 is proven",
        source="BRAND-KIT §8 / R1",
        example="Fire-safe and printed on the label.",
    ),
    ClaimTerm(
        id="compliant",
        kind=BANNED,
        # Phrase-scoped on purpose: "a compliant first touch" (email compliance) and
        # "IFRA-compliant stock fragrances" (the supplier's fragrance statement) are NOT label or
        # safety claims and must not be flagged — a census that cries wolf is a census nobody runs.
        pattern=(
            r"(?:label|safety|astm|CPSC|CPC)[\s-]+compliant"
            r"|compliant\s+(?:with\s+)?(?:astm|F2058|F2417|CPSC|the\s+label|label|safety)"
        ),
        reason=(
            "a label/safety/ASTM compliance claim is blocked until R1 is proven; the word IS the "
            "claim (a bare 'compliant' about something else, e.g. an email process, is not)"
        ),
        source="BRAND-KIT §8 / R1",
        example="Label compliant and ready to ship.",
    ),
    ClaimTerm(
        id="safety_block",
        kind=BANNED,
        pattern=(
            r"safety\s+block"
            r"|\bsafety\s+panel\b"
            r"|printed\s+safety\s+text"
        ),
        reason=(
            "R1 is unproven: the hero SKU has ONE 3x2in print area and the supplier never states "
            "that its safety text is printed, so a 'safety block on every unit' claim is BLOCKED"
        ),
        source="BRAND-KIT §8 / H1, H2 / R1",
        example="A real safety block on every unit.",
    ),
    ClaimTerm(
        id="cosmetic_claim",
        kind=BANNED,
        pattern=(
            r"moisturis|moisturiz|antibacterial|anti-bacterial|aromatherap|therapeutic"
            r"|sleep\s*aid|\bcures?\b|\btreats?\b"
        ),
        reason=(
            "no soap SKU exists and no cosmetic/therapeutic claim has any evidence; a body-care "
            "claim is regulated copy"
        ),
        source="BRAND-KIT §8.2 / W6-S1 (cosmetic family, proven unenforced before this term)",
        example="Moisturising and aromatherapy benefits.",
    ),
    ClaimTerm(
        id="unattested",
        kind=BANNED,
        pattern=(
            r"\borganic\b|fair[\s-]*trade|\bhandmade\b|dermatologist[\s-]*tested"
            r"|clinically\s+tested"
        ),
        reason=(
            "organic/fair-trade/handmade/dermatologist-tested are certifications or third-party "
            "tests no supplier has provided for this line"
        ),
        source="BRAND-KIT §8.2 / W6-S1 (unattested family, proven unenforced before this term)",
        example="Certified organic, fair-trade and handmade.",
    ),
    ClaimTerm(
        id="soap_offer",
        kind=BANNED,
        pattern=(
            # An OFFER means a price/sale shape attached to the soap itself. "made-to-order ... cold-process
            # soap next" is the allowed PLAN sentence and must not match (bridge W6 false-positive check).
            r"cold[\s-]*process(?:\s+\w+){0,4}\s+soap[^.\n]{0,60}?(?:\$\s?\d|\bfor\s+sale\b|\bavailable\s+now\b|\bin\s+stock\b|\bshop\s+now\b)"
            r"|\b(?:buy|order|shop)\s+(?:our|the|a|these|this)?\s*cold[\s-]*process\s+soap"
        ),
        reason=(
            "H4: cold-process soap is allowed as a PLAN only; no soap SKU is contracted, so "
            "offering it (price/order/buy) is a claim the business cannot fulfil"
        ),
        source="BRAND-KIT §8.3 / H4 (bridge W6: the offer ban was not executable before this term)",
        example="Our cold-process soap, $14.",
    ),
)

UNVERIFIED_TERMS: tuple[ClaimTerm, ...] = (
    ClaimTerm(
        id="net_weight_assertion",
        kind=UNVERIFIED,
        pattern=(
            r"(?:real|printed|guaranteed|certified|verified)\s+net\s+weight"
            r"|net\s+weight\s+on\s+every\s+unit"
        ),
        reason=(
            "whether '9 oz' is the wax net weight or the jar capacity is H3, unproven; a net-weight "
            "ASSERTION must be signed off (the bare '9 oz' in a product name is not this)"
        ),
        source="BRAND-KIT §8 / H3 / R1",
        example="A real net weight and a real safety block on every unit.",
    ),
    ClaimTerm(
        id="soap_process",
        kind=UNVERIFIED,
        pattern=r"cold[\s-]*process",
        reason=(
            "the term is the supplier's own published product type (W5-S1/S2), so it is true as a "
            "PLAN-level descriptor and the live sentences read as future/conditional; but this brand "
            "has no soap SKU and no supplier contract, so it is valid only in a plan sentence and "
            "must never appear in an offer sentence — and never with an origin, net-weight, ingredient "
            "or cosmetic claim (BRAND-KIT §8.3, H4)"
        ),
        source=(
            "BRAND-KIT §8.3 (cold-process: ALLOWED as a plan, BANNED as an offer) / W5-S1, W5-S2 / H4"
        ),
        example="Cold-process soap next.",
    ),
    ClaimTerm(
        id="made_in_arizona",
        kind=UNVERIFIED,
        pattern=r"made\s+in\s+arizona",
        reason="an origin claim that must match the actual supplier and invoice (H5)",
        source="BRAND-KIT §8 / H5",
        example="Made in Arizona.",
    ),
    ClaimTerm(
        id="burn_time",
        kind=UNVERIFIED,
        pattern=r"\d+\s*[\s-]*(?:hour|hr)s?\s+burn|\bburn\s+time\b",
        reason=(
            "a burn-duration figure is a measurement of the supplier's product, not this brand's; "
            "the wave-1 '55-hour burn' was exactly this fabrication (F-24)"
        ),
        source="BRAND-KIT §8 / F-24 wave-3",
        example="A 55-hour burn.",
    ),
)

#: PRIVATE detectors are GENERIC on purpose: the finding is "a personal address or name was
#: published", not "this one address" — a literal would be both weaker and itself a leak. The owner's
#: legal name is added DYNAMICALLY from the profile's own `sender_legal_name` at census time, so the
#: repository never stores the value (see `_private_terms_for`).
_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|drive|dr|lane|ln|way|boulevard|blvd|court|ct|place|pl|terrace|ter"
    r"|parkway|pkwy|highway|hwy|circle|cir|loop|trail|trl"
)
PRIVATE_TERMS: tuple[ClaimTerm, ...] = (
    ClaimTerm(
        id="owner_home_street",
        kind=PRIVATE,
        pattern=rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'-]+\s+){{1,4}}(?:{_STREET_SUFFIX})\b",
        reason=(
            "a street address in copy is a personal-location exposure (F-38: the contact page printed "
            "the owner's home address); no public candle page should carry one"
        ),
        source="auditor REPORT-W4 §7 / F-38",
        example="42 Example Lane",
    ),
    ClaimTerm(
        id="owner_home_city",
        kind=PRIVATE,
        pattern=r"\b[A-Za-z][A-Za-z .'-]{1,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
        reason=(
            "a city + state + ZIP in copy is a personal-location exposure (F-38); the owner's home "
            "city/ZIP must not be published"
        ),
        source="auditor REPORT-W4 §7 / F-38",
        example="Springfield, IL 62704",
    ),
)


def _private_terms_for(owner_legal_name: str | None) -> tuple[ClaimTerm, ...]:
    """The static private detectors + a DYNAMIC one for the owner's configured legal name.

    The literal never lives in source: it is read from the profile at census time and escaped into a
    pattern, so a test can plant a synthetic name and the real name is still caught when configured.
    """
    name = (owner_legal_name or "").strip()
    if not name:
        return PRIVATE_TERMS
    dynamic = ClaimTerm(
        id="owner_legal_name",
        kind=PRIVATE,
        pattern=rf"\b{re.escape(name)}\b",
        reason="the owner's legal name must not be published as a public contact identity (F-38)",
        source="auditor REPORT-W4 §7 / F-38",
        example=name,
    )
    return PRIVATE_TERMS + (dynamic,)

ALL_TERMS: tuple[ClaimTerm, ...] = BANNED_TERMS + UNVERIFIED_TERMS + PRIVATE_TERMS

#: `(id, example, why it is allowed)` — the supplier's own published words, or this brand's own
#: label/curation.  Pinned: every example here must census CLEAN.
ALLOWED_CLAIMS: tuple[tuple[str, str, str], ...] = (
    ("soy_candle", "made-to-order soy candles", "the supplier's published wax (BRAND-KIT §8)"),
    ("natural_soy_wax", "100% natural soy wax", "the supplier's own words (W4-S2)"),
    ("cotton_wick", "cotton wick", "the supplier's published wick (W4-S2)"),
    ("made_to_order", "made to order", "POD by definition; the supplier's lead-time text (W4-S6)"),
    (
        "chatsworth_origin",
        "printed and shipped from Chatsworth, CA",
        "the supplier's published location (W4-S6, W4-S7)",
    ),
    ("la_area", "in the Los Angeles area", "allowed phrasing of the same location (BRAND-KIT §8)"),
    (
        "our_label",
        "our scent curation and our own label",
        "the label artwork is ours and the scent is chosen from the published list (W4-S2)",
    ),
    ("custom_labelled", "custom-labelled soy candles", "the label is ours, the wax is the supplier's"),
    (
        "smaller_pour",
        "Same soy wax, same label, smaller pour.",
        "a size comparison, not a hand-poured claim — the false-positive this vocabulary avoids",
    ),
    # [W5] the final allowed list (`bridge/BRAND-KIT.md` §8.1) gained three product sentences that
    # the recorded reads support: the two vessel descriptions (W4-S2's published catalogue) and the
    # inquiry-only ordering sentence the generated site already prints.  Each is pinned clean by
    # `test_every_allowed_claim_censuses_clean` (the parametrised test grows with this tuple).
    (
        "vessel_9oz",
        "9 oz straight-sided glass",
        "the supplier's published vessel for the hero SKU (W4-S2; BRAND-KIT §8.1 [W5])",
    ),
    (
        "vessel_4oz",
        "4 oz amber glass with a cork lid",
        "the supplier's published vessel for the 4 oz SKU (W4-S2; BRAND-KIT §8.1 [W5])",
    ),
    (
        "orders_by_inquiry",
        "Orders are by inquiry.",
        "the site's own ordering sentence while no contact email exists (BRAND-KIT §8.1 [W5])",
    ),
)

#: Public names.  The live census script and the data-level test import this module (not a copy).
__all__ = [
    "ALLOWED_CLAIMS",
    "ALL_TERMS",
    "BANNED",
    "BANNED_TERMS",
    "Finding",
    "PRIVATE",
    "PRIVATE_TERMS",
    "PROFILE_COPY_FIELDS",
    "SITE_PAGE_NAMES",
    "UNVERIFIED",
    "UNVERIFIED_TERMS",
    "ClaimTerm",
    "blocking",
    "census_memory_rows",
    "census_pages",
    "census_profile",
    "census_text",
    "summarise",
]


def _excerpt(text: str, start: int, end: int, kind: str) -> str:
    """A short window around the match.  PRIVATE values are never reproduced (AGENTS.md §27)."""
    if kind == PRIVATE:
        return f"[redacted: {end - start} chars of a recorded private value]"
    left = max(0, start - 40)
    right = min(len(text), end + 40)
    window = re.sub(r"\s+", " ", text[left:right]).strip()
    # A banned-claim excerpt is a WINDOW around the match: it can also contain a private value
    # from a mixed field. Neither this module nor its callers may reproduce that value, so the
    # same GENERIC private detectors are applied to the window before it is returned.
    for private_term in PRIVATE_TERMS:
        window = private_term.regex().sub("[redacted]", window)
    return ("…" if left > 0 else "") + window + ("…" if right < len(text) else "")


def census_text(
    text: str,
    *,
    where: str,
    field: str = "text",
    row_id: int | None = None,
    terms: Iterable[ClaimTerm] | None = None,
) -> list[Finding]:
    """Every vocabulary hit in ONE string, deduped per (term, where, field) with an occurrence count.

    Empty/None text is `[]`, not a finding — an absent field is not a claim.
    """
    if not text:
        return []
    findings: list[Finding] = []
    for term in terms if terms is not None else ALL_TERMS:
        matches = list(term.regex().finditer(text))
        if not matches:
            continue
        first = matches[0]
        findings.append(
            Finding(
                term_id=term.id,
                kind=term.kind,
                where=where,
                field=field,
                excerpt=_excerpt(text, first.start(), first.end(), term.kind),
                reason=term.reason,
                source=term.source,
                count=len(matches),
                row_id=row_id,
            )
        )
    return findings


def _walk_json(value: Any, path: str, *, where: str, row_id: int | None = None) -> list[Finding]:
    """Deep-walk a free-form JSON column, scanning every STRING with its path as the field name.

    The profile carries `pricing_model`, `goals` and `confirmed_idea` as free-form JSON, and the
    copy that reaches a public page or a prompt lives in the strings inside them (SKU
    descriptions, goal sentences) — walking only the top-level keys would miss them.
    """
    findings: list[Finding] = []
    if isinstance(value, str):
        findings.extend(census_text(value, where=where, field=path, row_id=row_id))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            findings.extend(
                _walk_json(item, f"{path}.{key}" if path else str(key), where=where, row_id=row_id)
            )
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings.extend(_walk_json(item, f"{path}[{index}]", where=where, row_id=row_id))
    return findings


def _field_value(profile: Any, field: str) -> Any:
    """Read a field from an ORM row or a plain mapping — the census accepts both on purpose."""
    if isinstance(profile, Mapping):
        return profile.get(field)
    return getattr(profile, field, None)


def census_profile(profile: Any) -> list[Finding]:
    """Census a company profile (ORM row or the `context["company"]` mapping).

    Scans every field in `PROFILE_COPY_FIELDS`; JSON fields are deep-walked.  The result is the
    profile's own answer — the site generator prints what these fields hold, so a profile that
    censuses clean cannot render a banned claim (pinned by the renderer integration test).
    """
    findings: list[Finding] = []
    terms = (
        BANNED_TERMS + UNVERIFIED_TERMS + _private_terms_for(_field_value(profile, "sender_legal_name"))
    )
    for field in PROFILE_COPY_FIELDS:
        value = _field_value(profile, field)
        if value is None:
            continue
        if isinstance(value, Mapping | list | tuple):
            findings.extend(_walk_json(value, field, where="company_profile"))
        elif isinstance(value, str):
            findings.extend(census_text(value, where="company_profile", field=field, terms=terms))
        else:
            # A non-string scalar (a number, a bool) cannot carry a claim.
            continue
    return findings


def census_pages(
    pages: Mapping[str, str], *, private_values: Iterable[str] = ()
) -> list[Finding]:
    """Census the GENERATED site: every page file the generator wrote, by filename.

    The page is the published artifact, so this is the check that matters before a deploy: it
    catches a claim that entered through a field the profile census scans AND one that entered
    through the template (a hard-coded sentence), because both end up in the file.

    `private_values` are the owner's configured identity values (legal name / address): they are
    escaped into DYNAMIC private patterns at call time, so a page that prints them is caught while
    the repository never stores the values themselves.
    """
    dynamic: list[ClaimTerm] = []
    for value in private_values:
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        dynamic.append(
            ClaimTerm(
                id="owner_legal_name",
                kind=PRIVATE,
                pattern=rf"\b{re.escape(cleaned)}\b",
                reason="the owner's configured identity must not be published on a generated page (F-38)",
                source="auditor REPORT-W4 §7 / F-38",
                example=cleaned,
            )
        )
    terms = BANNED_TERMS + UNVERIFIED_TERMS + PRIVATE_TERMS + tuple(dynamic)
    findings: list[Finding] = []
    for name in sorted(pages):
        findings.extend(census_text(pages[name], where="generated_site", field=name, terms=terms))
    return findings


def census_memory_rows(rows: Iterable[Mapping[str, Any]]) -> list[Finding]:
    """Census memory rows (`id`, `title`, `content`; `category`/`source`/`tags` are context only).

    A memory row is what the model READS on the next run, so a stale claim here is a copy risk
    even when no public page carries it (wave-3 F-24: run 312 reasoned from a previous company's
    '55-hour burn').  Rows are the owner's data: this function only reads and never tags or edits.
    """
    findings: list[Finding] = []
    for row in rows:
        row_id = row.get("id")
        identifier = int(row_id) if isinstance(row_id, int | str) and str(row_id).isdigit() else None
        for field in ("title", "content"):
            text = row.get(field)
            if isinstance(text, str) and text:
                findings.extend(
                    census_text(
                        text,
                        where="memory_entries",
                        field=field,
                        row_id=identifier,
                    )
                )
    return findings


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    """The subset that must not publish: a false claim or a private value."""
    return [finding for finding in findings if finding.kind in (BANNED, PRIVATE)]


def summarise(findings: Iterable[Finding]) -> dict[str, int]:
    """`{kind: count}` for a receipt (always all three keys, even when the count is zero)."""
    counts: dict[str, int] = {BANNED: 0, UNVERIFIED: 0, PRIVATE: 0}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return counts
