"""WAVE 5 — the copy CLAIM census: the corrected brand copy cannot regress silently.

THE DEFECT this pins shut: the live company profile and the generated Cinder & Lye site carried
*"a real net weight and a real safety block on every unit"* (UNVERIFIED per bridge R1) and
*"poured in Los Angeles"*, and the contact page published the owner's home address and legal name
(F-38).  Wave 4 corrected the DATA (one `PUT /api/v1/config` + a site regeneration).  Data
corrections are not durable by themselves: the next write — an agent's draft, a prompt, the owner's
own edit — can put the same strings back, and nothing in the product would notice.

WHAT THIS PINS:

  * the vocabulary is COMPLETE in both directions: every `bridge/BRAND-KIT.md` §8 banned claim has
    a term whose own recorded example fires, and every supplier-supported claim (soy candle,
    100% natural soy wax, cotton wick, made to order, printed/shipped from Chatsworth, "in the
    Los Angeles area", our label/scent curation) censuses CLEAN — so the allow-list is executable
    documentation, not a comment;
  * the correction the Lead APPLIED is clean of banned and private terms (the two `cold-process`
    soap mentions remain a documented H4 watch item — reported, not silently passed);
  * the census guards the PUBLISHED artifact, not just the fields: a banned claim placed in the
    profile is found in the rendered `index.html`/`about.html` by `census_pages`, and the corrected
    profile renders pages with zero findings;
  * the F-38 privacy values are caught wherever they appear (profile field or contact page), are
    reported MASKED, and a cleared profile renders a contact page with none;
  * the profile field list is pinned to what the site generator publishes, and memory rows are
    scanned (a stale claim in memory is what the MODEL reads — wave-3 F-24).

RED leg (measured, `raw-w5/RED-*`): removing one banned term (`coconut_soy`) from the vocabulary
fails `test_every_banned_claim_in_the_decision_fires_on_its_own_example`; making `census_text`
return no findings fails the whole census half.  Both restored byte-exact (sha256 in REPORT-W5 §2).
"""
from __future__ import annotations

import pytest

from app.models.company import CompanyConfig
from app.services import copy_claims, site_service

# The exact strings the Lead's correction left on the live profile (GET /api/v1/config,
# 2026-10-29T02:10Z).  Used as a fixture so the correction itself is pinned, not just the rules.
LIVE_MISSION = (
    "Make a small, honest home-fragrance line: made-to-order soy candles now, cold-process soap "
    "next - 100% natural soy wax, cotton wick, printed and shipped from Chatsworth, CA."
)
LIVE_VISION = (
    "Candles first (print-on-demand, made-to-order, printed and shipped from Chatsworth, CA), "
    "soap second only once the candle line proves demand at a price that covers its own cost."
)
LIVE_DESCRIPTION = (
    "Cinder & Lye is a one-person, US direct-to-consumer brand selling custom-labelled soy candles "
    "made to order by a print-on-demand partner and shipped from Chatsworth, California; "
    "cold-process soap follows in a later wave. The owner curates the scents and writes the label; "
    "the partner prints, labels and ships."
)
LIVE_VALUE_PROP = (
    "Light, then lather: made-to-order soy candles, our scent curation and our own label, printed "
    "and shipped from Chatsworth, California."
)
LIVE_TARGET_MARKET = (
    "US gift buyers aged 25-44 who buy candles for themselves and as small gifts, and who shop "
    "small DTC brands on Instagram. They buy one or two candles at a time and read the label."
)
LIVE_SKUS = [
    {
        "sku": "CL-WSL-9",
        "name": "White Sage + Lavender 9 oz",
        "retail": 26,
        "description": (
            "Soy candle in a 9 oz straight-sided glass with a cotton wick. White sage + lavender. "
            "Made to order; printed and shipped from Chatsworth, CA."
        ),
        "availability": "made_to_order",
    },
    {
        "sku": "CL-WSL-4",
        "name": "White Sage + Lavender 4 oz",
        "retail": 19,
        "description": "The same scent in a 4 oz amber glass with a cork lid. Same soy wax, same "
        "label, smaller pour.",
        "availability": "made_to_order",
    },
    {
        "sku": "CL-DUO",
        "name": "Two-Candle Duo - 9 oz + 4 oz",
        "retail": 42,
        "description": "One 9 oz glass and one 4 oz amber, boxed together. Cheaper than buying the "
        "two apart.",
        "availability": "made_to_order",
    },
]

# Synthetic values only: the real owner identity must never be committed (AGENTS.md §27).
HOME_ADDRESS = "42 Example Lane, Springfield, IL 62704, USA"
OWNER_LEGAL_NAME = "Alex Example"


def _company(**overrides: object) -> CompanyConfig:
    values: dict[str, object] = {
        "name": "Cinder & Lye",
        "mission": LIVE_MISSION,
        "vision": LIVE_VISION,
        "description": LIVE_DESCRIPTION,
        "value_prop": LIVE_VALUE_PROP,
        "target_market": LIVE_TARGET_MARKET,
        "pricing_model": {"currency": "USD", "skus": LIVE_SKUS},
        "goals": {},
        "sender_legal_name": None,
        "sender_postal_address": None,
        "notification_email": None,
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _html(pages: dict[str, str]) -> dict[str, str]:
    return {name: text for name, text in pages.items() if name.endswith(".html")}


# ── 1. the vocabulary is complete in BOTH directions ─────────────────────────────────────────────
@pytest.mark.parametrize("term", copy_claims.ALL_TERMS, ids=lambda term: term.id)
def test_every_term_fires_on_its_own_recorded_example(term: copy_claims.ClaimTerm) -> None:
    """Every term carries a positive example, and the census must find that exact term in it."""
    findings = copy_claims.census_text(term.example, where="test")
    matched = [finding for finding in findings if finding.term_id == term.id]
    assert matched, (
        f"{term.id} did not fire on its own example {term.example!r} — the vocabulary and its "
        "example drifted apart, and a term that cannot fire protects nothing"
    )
    assert matched[0].kind == term.kind
    assert matched[0].reason.strip() and matched[0].source.strip()


def test_every_banned_claim_in_the_decision_is_present() -> None:
    """`BRAND-KIT §8` ban list -> term ids.  Deleting one is a silent re-permission."""
    required = {
        "coconut_soy",  # coconut-soy / coconut wax
        "tin_container",  # reusable tin / tin candle
        "hand_poured",  # hand-poured
        "poured_by_us",  # poured by us
        "poured_in_la",  # poured in Los Angeles (the exact live string)
        "small_batch",  # small-batch
        "non_toxic",  # non-toxic
        "clean_burning",  # clean-burning
        "eco_friendly",  # eco-friendly
        "our_formula",  # our formula / proprietary blend
        "astm",  # ASTM / F2058 / F2417 (blocked until R1)
        "fire_safe",  # fire-safe (blocked until R1)
        "safety_block",  # printed safety block (blocked until R1)
        "compliant",  # compliant / ready to ship (14 ids total, BRAND-KIT §8.2)
    }
    banned_ids = {term.id for term in copy_claims.BANNED_TERMS}
    assert required <= banned_ids, f"the ban list lost term(s): {sorted(required - banned_ids)}"


@pytest.mark.parametrize(
    ("claim_id", "example", "why"),
    copy_claims.ALLOWED_CLAIMS,
    ids=[claim[0] for claim in copy_claims.ALLOWED_CLAIMS],
)
def test_every_allowed_claim_censuses_clean(claim_id: str, example: str, why: str) -> None:
    """The supplier-supported claims must NOT be flagged — an over-broad ban blocks real copy."""
    findings = copy_claims.census_text(example, where="test")
    assert findings == [], f"allowed claim {claim_id} ({why}) was flagged: {findings}"


@pytest.mark.parametrize(
    ("family", "copy"),
    [
        ("cosmetic_claim", "Moisturising and antibacterial, with aromatherapy benefits."),
        ("cosmetic_claim", "This candle cures insomnia and treats anxiety."),
        ("unattested", "Certified organic, fair-trade and handmade."),
        ("unattested", "Dermatologist-tested and clinically tested."),
        ("soap_offer", "Our cold-process soap, $14."),
        ("soap_offer", "Buy our cold process soap today."),
    ],
)
def test_the_new_banned_families_fire(family: str, copy: str) -> None:
    """BRAND-KIT §8.2/§8.3 claimed these families were censused; before this test they fired 0."""
    findings = copy_claims.census_text(copy, where="test")
    assert family in {finding.term_id for finding in findings if finding.kind == "banned"}, findings
    assert copy_claims.blocking(findings), "a banned family must block a publish"


def test_a_banned_excerpt_never_reproduces_a_private_value() -> None:
    """The banned-claim excerpt is a window: a mixed field could include an address too."""
    findings = copy_claims.census_text(
        "Our coconut-soy blend is poured at 42 Example Lane, Springfield, IL 62704.",
        where="test",
    )
    banned = [finding for finding in findings if finding.kind == "banned"]
    assert banned, "the banned claim itself must still fire"
    for finding in banned:
        assert "42 Example Lane" not in finding.excerpt
        assert "62704" not in finding.excerpt


def test_no_allowed_example_matches_any_banned_or_private_pattern() -> None:
    """A narrower statement of the same rule, so a new ban cannot quietly eat allowed copy."""
    forbidden = [
        term for term in copy_claims.ALL_TERMS if term.kind in (copy_claims.BANNED, copy_claims.PRIVATE)
    ]
    for term in forbidden:
        for claim_id, example, _why in copy_claims.ALLOWED_CLAIMS:
            assert not term.regex().search(example), (
                f"banned/private term {term.id} matches allowed example {claim_id} ({example!r})"
            )


#: Strings that LOOK like a banned claim to a keyword scan but are not one, and must stay silent.
#: Measured on live `memory_entries` 14/50 ("a compliant first touch" — an email process, not a
#: label claim) and 205 ("IFRA-compliant stock fragrances" — the supplier's fragrance statement):
#: a census that cries wolf is a census nobody runs.
MUST_NOT_FIRE: tuple[tuple[str, str], ...] = (
    ("email_compliance", "Positioning memos before a compliant first touch can be written."),
    ("ifra_fragrance", "30 IFRA-compliant stock fragrances at no surcharge."),
    ("pci_compliance", "The payment processor is PCI compliant."),
    ("size_comparison_pour", "Same soy wax, same label, smaller pour."),
    ("la_area_origin", "Shipped from Chatsworth, in the Los Angeles area."),
)


@pytest.mark.parametrize(("case_id", "text"), MUST_NOT_FIRE, ids=[case[0] for case in MUST_NOT_FIRE])
def test_a_non_claim_mention_does_not_fire(case_id: str, text: str) -> None:
    findings = copy_claims.census_text(text, where="test")
    assert findings == [], f"{case_id} is not a banned claim but was flagged: {findings}"


def test_term_ids_are_unique() -> None:
    ids = [term.id for term in copy_claims.ALL_TERMS]
    assert len(ids) == len(set(ids))


# ── 2. the correction the Lead APPLIED is clean (banned + private), with ONE documented watch ────
def test_the_applied_correction_censuses_clean_except_the_documented_soap_watch() -> None:
    """The live profile's corrected copy: zero banned, zero private, and the known H4 watch.

    `cold-process` is the supplier's own published `product_type` (W5-S1/S2), so wave 6 answered H4 as
    ALLOWED-AS-PLAN (`BRAND-KIT §8.3`) while keeping it in the `unverified` class: the vocabulary
    cannot express "only in a future/conditional sentence", so every run still makes a human read the
    sentence once.  It is asserted as an EXPECTED unverified finding — the honest state — instead of
    being silently absent from the vocabulary.
    """
    findings = copy_claims.census_profile(_company())
    counts = copy_claims.summarise(findings)
    assert counts == {"banned": 0, "unverified": 2, "private": 0}, counts
    assert {finding.term_id for finding in findings} == {"soap_process"}
    assert {finding.field for finding in findings} == {"mission", "description"}


def test_the_old_live_claims_are_caught() -> None:
    """The exact strings wave 4 removed from the live profile must fire, or the correction is
    unpinned: a future write that pastes them back has to fail this suite."""
    old_missions = [
        "A real net weight and a real safety block on every unit.",
        "Made-to-order soy candle poured in Los Angeles.",
        "Hand-poured coconut-soy in a reusable tin.",
    ]
    found = set()
    for text in old_missions:
        found |= {finding.term_id for finding in copy_claims.census_text(text, where="test")}
    assert {
        "net_weight_assertion",
        "safety_block",
        "poured_in_la",
        "hand_poured",
        "coconut_soy",
        "tin_container",
    } <= found


def test_json_fields_are_deep_walked_with_their_path() -> None:
    """`pricing_model` is free-form JSON: a SKU description is copy that reaches a public page."""
    profile = _company(
        pricing_model={
            "currency": "USD",
            "skus": [
                {"sku": "CL-X", "name": "X", "retail": 20,
                 "description": "Hand-poured in a reusable tin."},
            ],
        }
    )
    findings = copy_claims.census_profile(profile)
    banned = {
        (finding.field, finding.term_id) for finding in findings if finding.kind == "banned"
    }
    assert banned == {
        ("pricing_model.skus[0].description", "hand_poured"),
        ("pricing_model.skus[0].description", "tin_container"),
    }


def test_non_string_scalars_are_not_censused() -> None:
    assert copy_claims.census_text("", where="test") == []
    assert copy_claims.census_profile({"name": "Cinder & Lye", "daily_cycle_hour": 6}) == []


# ── 3. the census guards the PUBLISHED artifact, not just the fields ─────────────────────────────
def test_a_banned_claim_in_the_profile_is_found_on_the_rendered_page() -> None:
    """The generator prints what the profile holds, so the page is where a claim becomes public."""
    company = _company(value_prop="Hand-poured in small batches.")
    pages = _html(site_service.render_pages(company))
    assert set(pages) == set(copy_claims.SITE_PAGE_NAMES)
    findings = copy_claims.census_pages(pages)
    banned = {finding.term_id for finding in findings if finding.kind == "banned"}
    assert banned == {"hand_poured", "small_batch"}
    banned_fields = {finding.field for finding in findings if finding.kind == "banned"}
    assert banned_fields == {"index.html"}
    # The mission/description text on the same pages still carries the documented H4 watch item.
    assert {
        (finding.field, finding.term_id) for finding in findings if finding.kind != "banned"
    } == {("index.html", "soap_process"), ("about.html", "soap_process")}


def test_the_corrected_profile_renders_pages_with_no_banned_or_private_claims() -> None:
    pages = _html(site_service.render_pages(_company()))
    findings = copy_claims.census_pages(pages)
    assert copy_claims.summarise(findings) == {"banned": 0, "unverified": 2, "private": 0}
    # The `about` page carries the mission and the description; the index carries the value prop.
    assert {finding.field for finding in findings} == {"index.html", "about.html"}


def test_the_site_meta_and_sitemap_are_scanned_too() -> None:
    """`meta.json`'s SEO description is served and indexed; it is copy, not metadata scaffolding."""
    pages = {
        "index.html": "<p>Fine.</p>",
        "meta.json": '{"seo": {"description": "A fire-safe candle."}}',
        "sitemap.xml": "<urlset><url><loc>http://x/</loc></url></urlset>",
    }
    findings = copy_claims.census_pages(pages)
    assert [(finding.field, finding.term_id) for finding in findings] == [
        ("meta.json", "fire_safe")
    ]


# ── 4. F-38 privacy: the home address and legal name are caught wherever they appear ─────────────
def test_the_f38_values_are_private_findings_in_the_profile() -> None:
    profile = _company(
        sender_postal_address=HOME_ADDRESS,
        sender_legal_name=OWNER_LEGAL_NAME,
    )
    findings = [finding for finding in copy_claims.census_profile(profile) if finding.kind == "private"]
    assert {finding.term_id for finding in findings} == {
        "owner_home_street",
        "owner_home_city",
        "owner_legal_name",
    }
    assert {finding.field for finding in findings} == {"sender_postal_address", "sender_legal_name"}


def test_the_f38_values_are_caught_on_the_generated_contact_page() -> None:
    """The page is what a visitor reads; the profile field alone does not prove the exposure."""
    company = _company(
        sender_postal_address=HOME_ADDRESS,
        sender_legal_name=OWNER_LEGAL_NAME,
    )
    pages = _html(site_service.render_pages(company))
    findings = copy_claims.census_pages(pages, private_values=[OWNER_LEGAL_NAME])
    private = {finding.term_id for finding in findings if finding.kind == "private"}
    assert private == {"owner_home_street", "owner_home_city", "owner_legal_name"}
    assert OWNER_LEGAL_NAME in pages["contact.html"]  # the exposure the census just named


def test_a_private_finding_never_reproduces_the_value() -> None:
    findings = copy_claims.census_text(HOME_ADDRESS, where="generated_site", field="contact.html")
    private = [finding for finding in findings if finding.kind == "private"]
    assert private
    for finding in private:
        assert "1135" not in finding.excerpt
        assert "Winona" not in finding.excerpt
        assert "redacted" in finding.excerpt


def test_the_cleared_profile_renders_a_contact_page_with_no_private_value() -> None:
    pages = _html(site_service.render_pages(_company()))
    findings = copy_claims.census_pages(pages)
    assert [finding for finding in findings if finding.kind == "private"] == []
    assert "No contact details have been added" in pages["contact.html"]


# ── 5. scope pins: a new copy-bearing field cannot be silently un-scanned ────────────────────────
def test_the_profile_field_list_covers_every_field_the_site_publishes() -> None:
    """`_profile_fields` is the renderer's reader; every key of it must map to a scanned field."""
    mapping = {"contact_email": "notification_email"}
    published = set(site_service._profile_fields(_company()))
    scanned = set(copy_claims.PROFILE_COPY_FIELDS)
    missing = {mapping.get(name, name) for name in published} - scanned
    assert not missing, f"the site publishes field(s) the census does not scan: {sorted(missing)}"


def test_the_page_list_matches_the_generator() -> None:
    assert copy_claims.SITE_PAGE_NAMES == site_service.PAGES


# ── 6. memory rows: what the MODEL reads next ────────────────────────────────────────────────────
def test_memory_rows_are_censused_with_their_row_id() -> None:
    rows = [
        {"id": 219, "category": "agent_run", "source": "competitor_research",
         "title": "Research POD candle makers", "content": "Coconut-soy in a reusable tin."},
        {"id": 220, "category": "agent_run", "source": "social_media",
         "title": "Draft post", "content": "Made-to-order soy candle, cotton wick."},
    ]
    findings = copy_claims.census_memory_rows(rows)
    by_row = {(finding.row_id, finding.term_id) for finding in findings}
    assert by_row == {
        (219, "coconut_soy"),
        (219, "tin_container"),
    }
    for finding in findings:
        assert finding.where == "memory_entries"


def test_a_private_value_in_memory_is_caught_too() -> None:
    rows = [{"id": 142, "title": "welcome", "content": f"Ship to {HOME_ADDRESS}."}]
    findings = copy_claims.census_memory_rows(rows)
    assert {finding.kind for finding in findings} == {"private"}


# ── 7. the small contract the future publish gate will use ───────────────────────────────────────
def test_blocking_is_banned_plus_private_and_never_unverified() -> None:
    findings = copy_claims.census_text(
        "Hand-poured. Cold-process soap. 42 Example Lane.",
        where="test",
    )
    blocking = copy_claims.blocking(findings)
    assert {finding.kind for finding in blocking} == {"banned", "private"}
    assert "soap_process" not in {finding.term_id for finding in blocking}
    assert copy_claims.summarise(findings) == {"banned": 1, "unverified": 1, "private": 1}
    assert all(isinstance(finding.as_dict(), dict) for finding in findings)
