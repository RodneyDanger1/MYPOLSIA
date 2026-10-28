"""WAVE 2 / F-09 + G-05 — the STOREFRONT CONTRACT, checked against the shipped generator.

`bridge/STOREFRONT-CONTRACT.md` is the written contract; this file is the executable half of it.
It is a CROSS-LANE CONFORMANCE test on purpose: `site_service.py` is the weaver's file, and the
wave-2 F-09 fix (a catalogue rendered from `company_config.pricing_model`) landed there.  Rather
than re-implementing it, this file pins the behaviour the contract promises, so a later refactor
that quietly breaks the storefront fails HERE by name.

THE CONTRACT (short form — see the markdown for the full text)

  1. The catalogue comes ONLY from `pricing_model`; no profile → no products, nothing invented.
  2. Only SELLING prices are printed.  `unit_cost`, `margin_pct`, `cogs` are internal numbers and
     must never reach a public page — above all, a row that carries ONLY a cost must render
     "price not recorded", never the cost as if it were the price.
  3. No cart, no checkout, no order form, no payment step.  The only order path is an inquiry
     (a `mailto:` when the profile has a contact address) and the page SAYS it is a catalogue.
  4. Every interpolated value is HTML-escaped, and rendering is byte-deterministic.
  5. The page never claims stock/availability it does not model.  (`pricing_model` has no
     availability field the generator reads — see REQUESTS R-W2-B1 in the lane report: the
     contract asks for `availability` to be honoured when present, and today it is NOT rendered.
     The honest behaviour that IS enforceable — and enforced here — is that no page invents a
     stock claim.)
  6. G-05: the contact page publishes `sender_legal_name` and `sender_postal_address` VERBATIM.
     This test pins that fact (it is why the publishing warning is required) — the warning itself
     is a create-response/UI change and is handed to the weaver as REQUESTS R-W2-B2.
"""
from __future__ import annotations

import re

from app.models.company import CompanyConfig
from app.services import site_service

PAGES = ("index.html", "about.html", "contact.html")

#: The trial's real shape (`pricing_model` as the candle trial set it).
SKUS_MODEL = {
    "currency": "USD",
    "model": "physical_dtc",
    "skus": [
        {"sku": "CP-FIRE-8", "name": "Fireside", "retail": 28, "unit_cost": 9.4, "margin_pct": 66.4},
        {"sku": "CP-CEDAR-8", "name": "Cedar & Sage", "retail": 28, "unit_cost": 9.4},
        {"sku": "CP-VANILLA-12", "name": "Vanilla Oak (12 oz)", "retail": 38, "unit_cost": 12.1},
    ],
    "note": "unit_cost = co-packer landed cost per unit",
}

#: The seed's shape (`{"plans": [{name, price}]}`) — a service/SaaS profile.
PLANS_MODEL = {
    "currency": "USD",
    "plans": [
        {"name": "Starter", "price": 49},
        {"name": "Pro", "price": 149},
    ],
}


def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Copper & Pine Candle Co.",
        "value_prop": "A clean-burning coconut-soy candle with a wood wick.",
        "target_market": "US home-decor buyers 28-45.",
        "description": "Solo-founded DTC candle brand.",
        "mission": "Make an ordinary evening a ritual.",
        "website_url": "",
        "notification_email": "hello@copperpine.example",
        "sender_legal_name": "Copper & Pine LLC",
        "sender_postal_address": "1 Main Street, Winona, MN 55987",
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _pages(company: CompanyConfig, **kwargs) -> dict[str, str]:
    kwargs.setdefault("slug", "copper-pine")
    kwargs.setdefault("base_url", "http://127.0.0.1:8000/")
    return site_service.render_pages(company, **kwargs)


def _joined(pages: dict[str, str]) -> str:
    return "\n".join(pages[name] for name in PAGES)


# ── 1 + 2: real products, selling prices only ────────────────────────────────────────────────


def test_the_trial_skus_render_as_a_catalogue_with_real_prices():
    index = _pages(_company(pricing_model=SKUS_MODEL))["index.html"]

    assert '<section id="products">' in index
    assert "Fireside" in index and "$28" in index
    assert "Vanilla Oak (12 oz)" in index and "$38" in index
    assert "This is a catalogue, not a shop" in index


def test_the_seed_plans_shape_also_renders():
    index = _pages(_company(pricing_model=PLANS_MODEL))["index.html"]

    assert '<section id="products">' in index
    assert "Starter" in index and "$49" in index
    assert "Pro" in index and "$149" in index


def test_internal_costs_and_margins_never_reach_a_public_page():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    joined = _joined(pages)

    for private in ("9.4", "12.1", "66.4", "unit_cost", "margin_pct", "landed cost"):
        assert private not in joined, f"internal value {private!r} leaked onto a public page"


def test_a_cost_only_row_says_price_not_recorded_and_never_prints_the_cost():
    """The sharpest failure mode: a cost printed as a price would be a fabricated price claim."""
    model = {"currency": "USD", "skus": [{"name": "Mystery Tin", "unit_cost": 9.4}]}

    index = _pages(_company(pricing_model=model))["index.html"]

    assert "Mystery Tin" in index
    assert "price not recorded" in index
    assert "$9.40" not in index and "$9.4" not in index
    assert "$0" not in index


def test_a_price_that_is_not_a_real_number_is_treated_as_unrecorded():
    model = {
        "currency": "USD",
        "skus": [
            {"name": "String price", "retail": "28"},
            {"name": "Bool price", "retail": True},
            {"name": "Negative price", "retail": -5},
            {"name": "Good price", "retail": 28.5},
        ],
    }

    index = _pages(_company(pricing_model=model))["index.html"]

    assert index.count("price not recorded") == 3
    assert "$28.50" in index
    assert "-$5" not in index and "$-5" not in index


# ── 1: nothing invented from an absent/unknown shape ─────────────────────────────────────────


def test_an_absent_or_unrecognised_pricing_model_renders_no_catalogue():
    shapes = [
        None,
        {},
        {"currency": "USD"},
        {"plans": "not a list"},
        {"skus": []},
        {"offerings": [{"name": "", "price": 5}]},
        {"skus": ["just a string", 7]},
    ]
    for model in shapes:
        index = _pages(_company(pricing_model=model))["index.html"]
        assert '<section id="products">' not in index, f"invented a catalogue for {model!r}"


def test_a_row_with_only_a_sku_code_shows_the_code_and_invents_no_name():
    """The one recognisable row that carries no NAME field: the product's own identifier.

    Neither the price nor a name is invented — `12` renders because the row recorded it, and the
    code is the row's only human-readable label.  CONTRACT NUANCE (REQUESTS R-W2-B1): the contract
    says the display NAME comes from `name`/`title`/`label` and that a SKU code, when it is all a
    row has, belongs in the SKU slot — today it is rendered in the name slot.  That is a display
    refinement, not a fabrication: nothing here was made up.
    """
    model = {"currency": "USD", "skus": [{"sku": "no-name-field", "retail": 12}]}

    index = _pages(_company(pricing_model=model))["index.html"]

    assert "no-name-field" in index
    assert "$12" in index


# ── 3: no fake shop — catalogue + inquiry only ──────────────────────────────────────────────


def test_no_page_contains_a_form_button_cart_or_checkout():
    for model in (SKUS_MODEL, PLANS_MODEL, None):
        pages = _pages(_company(pricing_model=model))
        joined = _joined(pages)

        assert "<form" not in joined, "the generated site must never carry a form"
        assert "<button" not in joined, "no control may appear on a static page"
        assert "<input" not in joined
        for banned in ("add to cart", "buy now", "checkout", "payment", "credit card"):
            # The ONE allowed occurrence is the honesty sentence ("no cart, checkout or online
            # payment") — anything not inside a sentence that negates it is a fake shop.
            for match in re.finditer(banned, joined, flags=re.IGNORECASE):
                window = joined[max(0, match.start() - 80): match.end() + 80].lower()
                assert "no " in window or "not a" in window or "there is no" in window, (
                    f"{banned!r} appears outside an honest denial: ...{window}..."
                )


def test_the_order_path_is_an_inquiry_and_the_page_says_so():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    index = pages["index.html"]
    contact = pages["contact.html"]

    assert "Ask about an order" in index
    assert f'mailto:{_company().notification_email}' in index
    assert "there is no cart or online checkout" in contact
    assert "This page has no form" in contact


def test_a_profile_with_no_contact_address_still_renders_an_honest_order_path():
    pages = _pages(_company(pricing_model=SKUS_MODEL, notification_email=""))
    joined = _joined(pages)

    assert "Ordering is by inquiry" in joined
    assert "mailto:" not in joined
    assert "@" not in joined.replace("/*", ""), "no address may be invented"


def test_the_catalogue_is_bounded_and_says_when_it_is_cut():
    model = {
        "currency": "USD",
        "skus": [{"name": f"SKU {index}", "retail": 10 + index} for index in range(60)],
    }

    index = _pages(_company(pricing_model=model))["index.html"]

    assert index.count('class="product-name"') == 50
    assert "more product(s) are recorded" in index


# ── 5: no invented availability claim ───────────────────────────────────────────────────────


def test_no_page_invents_a_stock_or_availability_claim():
    model = {
        "currency": "USD",
        "skus": [{"name": "Fireside", "retail": 28, "availability": "in_stock", "qty": 12}],
    }

    joined = _joined(_pages(_company(pricing_model=model))).lower()

    for claim in ("in stock", "available now", "ships in", "ready to ship", "only 12 left"):
        assert claim not in joined, (
            f"the page invented an availability claim ({claim!r}); pricing_model availability is "
            "not a modelled fact (contract §5 / REQUESTS R-W2-B1)"
        )


# ── 4: escaping + determinism ───────────────────────────────────────────────────────────────


def test_hostile_product_and_profile_values_are_escaped():
    model = {
        "currency": "USD",
        "skus": [{"name": '<script>alert("x")</script>', "retail": 28}],
    }
    pages = _pages(
        _company(
            pricing_model=model,
            value_prop='<img src=x onerror=alert(1)>',
            sender_postal_address="1 Main St <b>bold</b>",
        )
    )
    joined = _joined(pages)

    assert "<script>" not in joined
    assert "<img" not in joined
    assert "<b>bold</b>" not in joined
    assert "&lt;script&gt;" in joined


def test_rendering_is_byte_deterministic():
    company = _company(pricing_model=SKUS_MODEL)

    first = _pages(company)
    second = _pages(company)

    assert first == second
    for name in PAGES:
        assert first[name].encode("utf-8") == second[name].encode("utf-8")


# ── 6: G-05 — what the contact page publishes verbatim ─────────────────────────────────────


def test_the_contact_page_publishes_the_stored_legal_name_and_address_verbatim():
    """This is the G-05 risk, pinned as a FACT: publishing a home address is one click."""
    pages = _pages(_company(pricing_model=SKUS_MODEL))

    assert "1 Main Street, Winona, MN 55987" in pages["contact.html"]
    assert "Copper &amp; Pine LLC" in pages["contact.html"]
    assert "Legal name" in pages["contact.html"]
    assert "Address" in pages["contact.html"]
    # The address is a CONTACT-page detail: it must not be sprayed across the brand pages.
    assert "1 Main Street" not in pages["index.html"]
    assert "1 Main Street" not in pages["about.html"]


def test_an_empty_profile_publishes_no_contact_details_rather_than_placeholders():
    pages = _pages(
        _company(
            pricing_model=None,
            notification_email="",
            sender_legal_name="",
            sender_postal_address="",
        )
    )

    assert "No contact details have been added" in pages["contact.html"]
    assert "Legal name" not in pages["contact.html"]
