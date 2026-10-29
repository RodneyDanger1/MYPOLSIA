"""WAVE 3 — the storefront catalogue's contract fields, and ONE renderer for site + package.

Wave 2 shipped the catalogue (F-09) and bridge's contract (`bridge/STOREFRONT-CONTRACT.md`) named
the two remaining display gaps as REQUESTS R-W2-B1:

  * B1.1 — a recorded `description` is rendered under the product name (one short line, escaped,
    bounded) instead of being ignored;
  * B1.3 — a SKU code is shown in its OWN slot (`<span class="product-sku">`) instead of
    masquerading as the display name; the name slot only ever carries a name-ish field.

B1.2 (`availability`) is deliberately NOT implemented here: bridge's contract §5 and bridge's own
conformance test (`test_w2_bridge_storefront_contract.py::test_no_page_invents_a_stock_or_availability_claim`)
state that the page must not claim stock it does not model.  Turning a free-form field into a
public "in stock" claim is a decision for that contract's owner, not a silent fix — recorded as
REQUESTS in `weaver/REPORT-W3.md`.

Wave 3 / R-W09-1: the downloadable site package (`site_package_service.render_site_package`) used
to render a brochure while the served site printed the catalogue.  The package now renders through
the SAME `site_service.catalogue_rows` + `catalogue_section` implementation, and the test below
pins that by comparing the two product lists byte-for-byte.

The rules that must keep holding: selling prices only (never `unit_cost`/`margin_pct`), "price not
recorded" for a row with no real price, no form/button/cart/checkout anywhere, an inquiry as the
only order path, every interpolated value escaped, deterministic output.
"""
from __future__ import annotations

import re

from app.models.company import CompanyConfig
from app.services import site_package_service, site_service

PAGES = ("index.html", "about.html", "contact.html")

SKUS_MODEL = {
    "currency": "USD",
    "model": "physical_dtc",
    "skus": [
        {
            "sku": "CP-FIRE-9",
            "name": "Fireside",
            "retail": 26,
            "unit_cost": 9.49,
            "margin_pct": 63.5,
            "description": "Coconut-soy, wood wick, 9 oz.",
        },
        {
            "sku": "CP-CEDAR-9",
            "name": "Cedar & Sage",
            "retail": 26,
            "unit_cost": 9.49,
            "margin_pct": 63.5,
        },
        {
            "sku": "CP-VANILLA-12",
            "name": "Vanilla Oak (12 oz)",
            "retail": 34,
            "unit_cost": 12.99,
            "margin_pct": 61.8,
        },
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


def _product_list(document: str) -> str:
    """The exact bytes between the catalogue list's open and close tags ('' when absent)."""
    match = re.search(r'<ul class="product-list">.*?</ul>', document, flags=re.S)
    return match.group(0) if match else ""


def _descriptions(document: str) -> list[str]:
    return re.findall(r'<span class="product-desc">(.*?)</span>', document, flags=re.S)


# ── R-W2-B1.1 / B1.3 — the three contract fields ────────────────────────────────────────────
#: Bridge's wave-3 chosen catalogue (`bridge/REPORT-W3.md` §7, `PRODUCT-DECISION.md`): a
#: print-on-demand custom-label candle brand, three selling rows with `sku`/`name`/`retail`/
#: `description`/`availability` and `currency: "USD"` — and NO `unit_cost`/`margin_pct` in the
#: public shape. `availability` is recorded but must not become a public stock claim.
BRIDGE_W3_MODEL = {
    "currency": "USD",
    "model": "physical_dtc",
    "skus": [
        {
            "sku": "CL-9-WSLA",
            "name": "White Sage + Lavender · 9 oz",
            "retail": 26.00,
            "description": "Coconut-soy, cotton wick, 50-60 h burn.",
            "availability": "in_stock",
        },
        {
            "sku": "CL-4-ORCH",
            "name": "Sea Salt + Orchid · 4 oz",
            "retail": 18.00,
            "description": "Coconut-soy, cotton wick, 25-30 h burn.",
            "availability": "made_to_order",
        },
        {
            "sku": "SET-4-MUG",
            "name": "Candle & Mug Gift Set",
            "retail": 34.00,
            "description": "The 4 oz candle with a stoneware mug.",
            "availability": "out_of_stock",
        },
    ],
}


def test_the_chosen_wave3_catalogue_renders_honestly():
    """The product bridge's wave-3 decision picked, through the real generator.

    Names and selling prices print; recorded descriptions print; the SKU codes do not take the
    name slot; NO availability claim is invented (the contract forbids stock claims); the only
    order path is an inquiry; and the package renders the same catalogue.
    """
    company = _company(pricing_model=BRIDGE_W3_MODEL)

    pages = _pages(company)
    index = pages["index.html"]

    assert '<section id="products">' in index
    assert "White Sage + Lavender · 9 oz" in index and "$26" in index
    assert "Sea Salt + Orchid · 4 oz" in index and "$18" in index
    assert "Candle &amp; Mug Gift Set" in index and "$34" in index
    assert "Coconut-soy, cotton wick, 50-60 h burn." in index
    assert index.count('class="product-desc"') == 3
    # The identifiers are not names, and no stock claim is printed for ANY recorded availability.
    for code in ("CL-9-WSLA", "CL-4-ORCH", "SET-4-MUG"):
        assert code not in index
    joined = "\n".join(pages[name] for name in PAGES).lower()
    for claim in ("in stock", "out of stock", "made to order", "ships in", "ready to ship"):
        assert claim not in joined, f"invented a stock claim ({claim!r})"
    # Inquiry path only — never a form, cart or checkout.
    assert "Ask about an order" in index
    assert "mailto:hello@copperpine.example?subject=Order%20inquiry" in index
    assert "This is a catalogue, not a shop" in index
    for dead_end in ("<form", "<button", "<input", "add to cart", "buy now"):
        assert dead_end not in joined

    package_html = site_package_service.render_site_package(company)["index.html"]
    assert _product_list(package_html) == _product_list(index)
    assert "Coconut-soy, cotton wick, 50-60 h burn." in package_html


# ── B1.1 — the recorded description ──────────────────────────────────────────────────────────


def test_a_recorded_description_renders_under_the_product_name():
    index = _pages(_company(pricing_model=SKUS_MODEL))["index.html"]

    assert '<section id="products">' in index
    assert _descriptions(index) == ["Coconut-soy, wood wick, 9 oz."]
    # Under the name it belongs to, not attached to another product.
    row = re.search(r"<li>.*?Fireside.*?</li>", index, flags=re.S)
    assert row is not None
    assert 'class="product-desc"' in row.group(0)


def test_a_description_is_one_collapsed_line_and_bounded():
    long_text = "  coconut-soy\n\n   wood wick\t" + ("x" * 400)
    model = {"skus": [{"name": "Long copy", "retail": 26, "description": long_text}]}

    index = _pages(_company(pricing_model=model))["index.html"]
    rendered = _descriptions(index)

    assert len(rendered) == 1
    assert "\n" not in rendered[0] and "\t" not in rendered[0]
    assert rendered[0].startswith("coconut-soy wood wick x")
    assert len(rendered[0]) <= site_service.MAX_PRODUCT_DESCRIPTION
    assert rendered[0].endswith("…")


def test_a_hostile_description_is_escaped():
    model = {
        "skus": [
            {"name": "Fireside", "retail": 26, "description": '<script>alert("x")</script>'}
        ]
    }

    index = _pages(_company(pricing_model=model))["index.html"]

    assert "<script>" not in index
    assert "&lt;script&gt;" in index


def test_a_non_string_description_renders_nothing():
    """`123` in the description slot is not a description — it is not printed as one."""
    model = {"skus": [{"name": "Fireside", "retail": 26, "description": 123}]}

    index = _pages(_company(pricing_model=model))["index.html"]

    assert "Fireside" in index
    assert _descriptions(index) == []


# ── B1.3 — the identifier slot is not the name slot ──────────────────────────────────────────


def test_a_sku_code_is_not_the_display_name():
    model = {
        "skus": [
            {"sku": "CP-FIRE-9", "name": "Fireside", "retail": 26},
            {"sku": "no-name-field", "retail": 12},
        ]
    }

    index = _pages(_company(pricing_model=model))["index.html"]

    # The named row: the name is the label, and the code is NOT rendered as one.
    assert '<span class="product-name">Fireside</span>' in index
    assert "CP-FIRE-9" not in index
    # The code-only row: its identifier lives in the SKU slot, with no invented name.
    assert '<span class="product-sku">no-name-field</span>' in index
    assert index.count('class="product-name"') == 1
    # Both prices are real and printed.
    assert "$26" in index and "$12" in index


def test_a_row_with_neither_a_name_nor_an_identifier_is_not_invented():
    model = {"skus": [{"retail": 26, "description": "A product with no label at all"}]}

    index = _pages(_company(pricing_model=model))["index.html"]

    assert '<section id="products">' not in index
    assert "A product with no label at all" not in index


# ── the rules that must keep holding ────────────────────────────────────────────────────────


def test_no_cost_or_margin_reaches_the_page_and_no_shop_controls_appear():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    joined = "\n".join(pages[name] for name in PAGES)

    for private in ("9.49", "12.99", "63.5", "61.8", "unit_cost", "margin_pct"):
        assert private not in joined, f"internal number {private!r} leaked onto a public page"
    for dead_end in ("<form", "<button", "<input", "add to cart", "buy now"):
        assert dead_end not in joined
    assert "This is a catalogue, not a shop" in pages["index.html"]


def test_a_row_with_no_real_price_says_so_next_to_its_description():
    model = {
        "skus": [
            {
                "sku": "CP-MYSTERY",
                "name": "Mystery Jar",
                "description": "A trial scent.",
                "unit_cost": 9.4,
            }
        ]
    }

    index = _pages(_company(pricing_model=model))["index.html"]

    assert "Mystery Jar" in index
    assert "A trial scent." in index
    assert "price not recorded" in index
    assert "$9.40" not in index and "$9.4" not in index


# ── R-W09-1 — the downloadable package renders the SAME catalogue ────────────────────────────


def test_a_catalogue_with_no_contact_address_does_not_point_at_an_empty_contact_page():
    """Found LIVE on `/sites/cinder-lye/` (wave 2): the catalogue said "use the contact page to
    get in touch" while the contact page printed no ADDRESS route at all (no email configured).
    A public page must not point at an order path it does not have."""
    pages = _pages(_company(pricing_model=SKUS_MODEL, notification_email=""))
    index = pages["index.html"]
    contact = pages["contact.html"]

    assert "Ordering is by inquiry." in index
    assert "use the contact page to get in touch" not in index
    # The contact page states the real reason and the real path, not a dead end.
    assert "Orders are by inquiry once a contact address is added" in contact
    assert "there is no cart or online checkout" in contact
    assert "mailto:" not in "\n".join(pages[name] for name in PAGES)


def test_a_catalogue_with_no_contact_details_at_all_publishes_no_address():
    pages = _pages(
        _company(
            pricing_model=SKUS_MODEL,
            notification_email="",
            sender_legal_name="",
            sender_postal_address="",
        )
    )

    assert "No contact details have been added to the company profile yet" in pages["contact.html"]
    assert "mailto:" not in "\n".join(pages[name] for name in PAGES)
    # The catalogue still does not promise an order path that does not exist.
    assert "Ordering is by inquiry." in pages["index.html"]


def test_the_package_carries_the_same_catalogue_as_the_served_site():
    company = _company(pricing_model=SKUS_MODEL)

    package_html = site_package_service.render_site_package(company)["index.html"]
    served_html = _pages(company)["index.html"]

    assert '<section id="products">' in package_html
    assert _product_list(package_html) == _product_list(served_html)
    assert "Fireside" in package_html and "$26" in package_html
    assert "Coconut-soy, wood wick, 9 oz." in package_html
    assert "This is a catalogue, not a shop" in package_html
    # No internal number, and no shop control, in the package either.
    for private in ("9.49", "12.99", "63.5", "unit_cost", "margin_pct"):
        assert private not in package_html, f"internal number {private!r} leaked into the package"
    for dead_end in ("<form", "<button", "<input", "add to cart", "buy now"):
        assert dead_end not in package_html


def test_the_package_readme_states_the_catalogue_boundary():
    company = _company(pricing_model=SKUS_MODEL)

    readme = site_package_service.render_site_package(company)["HOSTING-README.md"]

    assert "Products" in readme
    assert "catalogue, not a shop" in readme
    assert "no cart, checkout or online payment" in readme
    assert "Internal costs and margins are never written" in readme


def test_a_package_without_a_catalogue_stays_a_brochure():
    package_html = site_package_service.render_site_package(_company())["index.html"]
    readme = site_package_service.render_site_package(_company())["HOSTING-README.md"]

    assert '<section id="products">' not in package_html
    assert "price not recorded" not in package_html
    assert "catalogue, not a shop" not in readme
    assert "## Products" not in readme


def test_a_hostile_product_description_is_escaped_in_the_package():
    model = {"skus": [{"name": "Fireside", "retail": 26, "description": "<b>bold</b>"}]}

    package_html = site_package_service.render_site_package(
        _company(pricing_model=model)
    )["index.html"]

    assert "<b>bold</b>" not in package_html
    assert "&lt;b&gt;bold&lt;/b&gt;" in package_html


def test_the_package_with_a_catalogue_stays_byte_deterministic():
    company = _company(pricing_model=SKUS_MODEL)

    first = site_package_service.render_site_package(company)
    second = site_package_service.render_site_package(company)

    assert first == second
    for name, content in first.items():
        assert content.encode("utf-8") == second[name].encode("utf-8")
