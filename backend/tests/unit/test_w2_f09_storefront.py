"""WAVE 2 / F-09 — the generated site prints the REAL catalogue, and never becomes a fake shop.

The measured defect (Visuals F-09, P2): `POST /api/v1/sites` produced a 9,135-byte brochure with
the name, tagline, mission and "Get in touch to learn more." — no SKU, no price — while
`company_config.pricing_model` held three priced SKUs.  The owner asked for a website for a candle
company and got one that could not show the product it sells.

This test fixes the contract on the generator (`site_service.render_pages`, the function both
`generate_site` and `regenerate_site` write from):

  * a recognisable product list (the seed's `plans` shape AND the trial's `skus` shape) renders a
    Products section with the REAL names and selling prices;
  * ONLY selling prices are printed — internal `unit_cost` / `margin_pct` numbers never leak;
  * a row without a real price says "price not recorded" (never `$0`, never a guessed number);
  * an absent/unrecognised `pricing_model` renders NO products section — nothing is invented;
  * there is NO cart, checkout, order form or payment step anywhere (the only order path is an
    inquiry), and the page says so;
  * every interpolated value stays HTML-escaped, and the output stays byte-deterministic.
"""
from __future__ import annotations

import math

from app.models.company import CompanyConfig
from app.services import site_service

PAGES = ("index.html", "about.html", "contact.html")


def _company(**overrides) -> CompanyConfig:
    values = {
        "name": "Copper & Pine Candle Co.",
        "value_prop": "A clean-burning coconut-soy candle with a wood wick.",
        "target_market": "US women 28-45 who buy home decor.",
        "description": "Solo-founded DTC candle brand.",
        "mission": "Make an ordinary evening a ritual.",
        "website_url": "",
        "notification_email": "hello@copperpine.example",
        "sender_legal_name": "Copper & Pine LLC",
        "sender_postal_address": "1 Main Street, Winona, MN",
    }
    values.update(overrides)
    return CompanyConfig(**values)


def _pages(company: CompanyConfig, **kwargs) -> dict[str, str]:
    kwargs.setdefault("slug", "copper-pine")
    kwargs.setdefault("base_url", "http://127.0.0.1:8000/")
    return site_service.render_pages(company, **kwargs)


SKUS_MODEL = {
    "currency": "USD",
    "model": "physical_dtc",
    "skus": [
        {"sku": "CP-FIRE-8", "name": "Fireside", "retail": 28, "unit_cost": 9.4, "margin_pct": 66.4},
        {"sku": "CP-CEDAR-8", "name": "Cedar & Sage", "retail": 28, "unit_cost": 9.4, "margin_pct": 66.4},
        {"sku": "CP-VANILLA-12", "name": "Vanilla Oak (12 oz)", "retail": 38, "unit_cost": 12.1,
         "margin_pct": 68.2},
    ],
    "wholesale_tier": {"moq": 24, "unit_price": 16.8, "margin_pct": 40.0},
    "note": "unit_cost = co-packer landed cost per unit; excludes shipping",
}


def test_the_trial_skus_render_as_a_catalogue_with_real_prices():
    index = _pages(_company(pricing_model=SKUS_MODEL))["index.html"]

    assert '<section id="products">' in index
    assert "Fireside" in index and "$28" in index
    assert "Cedar &amp; Sage" in index or "Cedar & Sage" in index
    assert "Vanilla Oak (12 oz)" in index and "$38" in index
    assert "Ask about an order" in index
    assert "This is a catalogue, not a shop" in index


def test_internal_costs_and_margins_never_reach_the_public_page():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    joined = "\n".join(pages[name] for name in PAGES)
    for private in ("9.4", "12.1", "66.4", "68.2", "unit_cost", "margin_pct", "16.8"):
        assert private not in joined, f"internal number {private!r} leaked onto a public page"


def test_the_seed_plans_shape_also_renders():
    index = _pages(
        _company(pricing_model={"plans": [{"name": "Starter", "price": 49},
                                          {"name": "Pro", "price": 149}]})
    )["index.html"]

    assert '<section id="products">' in index
    assert "Starter" in index and "$49" in index
    assert "Pro" in index and "$149" in index


def test_a_currency_code_other_than_usd_is_printed_verbatim():
    index = _pages(
        _company(pricing_model={"currency": "EUR", "skus": [{"name": "Candle", "retail": 28}]})
    )["index.html"]
    assert "EUR 28" in index


def test_absent_or_unrecognised_pricing_model_renders_no_catalogue():
    for model in (None, {}, {"currency": "USD"}, {"model": "physical_dtc"}, {"skus": "not a list"},
                  {"plans": [{"note": "a plan with no name"}]}, {"skus": [{"price": 28}]}):
        index = _pages(_company(pricing_model=model))["index.html"]
        assert '<section id="products">' not in index, f"invented a catalogue for {model!r}"
        assert "price not recorded" not in index
        assert "$0" not in index


def test_a_row_without_a_real_price_is_labelled_not_fabricated():
    index = _pages(
        _company(
            pricing_model={
                "skus": [
                    {"name": "Mystery Jar"},
                    {"name": "String Price", "retail": "28"},
                    {"name": "Not Finite", "retail": math.nan},
                    {"name": "Negative", "retail": -5},
                ]
            }
        )
    )["index.html"]
    assert "Mystery Jar" in index
    assert index.count("price not recorded") == 4
    assert "$0" not in index


def test_product_names_stay_html_escaped():
    hostile = '<script>alert("x")</script>'
    index = _pages(_company(pricing_model={"skus": [{"name": hostile, "retail": 28}]}))["index.html"]
    assert "<script>" not in index
    assert "&lt;script&gt;" in index


def test_there_is_no_cart_checkout_or_order_form_anywhere():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    joined = "\n".join(pages[name] for name in PAGES).lower()
    for dead_end in ("<form", "</form>", "add to cart", "buy now", "checkout button",
                     'type="submit"', "stripe", "paypal", "card number"):
        assert dead_end not in joined
    # The ONLY order path is the inquiry the page names.
    index = pages["index.html"]
    assert "mailto:hello@copperpine.example?subject=Order%20inquiry" in index


def test_the_contact_page_says_orders_are_by_inquiry_when_a_catalogue_exists():
    pages = _pages(_company(pricing_model=SKUS_MODEL))
    contact = pages["contact.html"]
    assert "there is no cart or online checkout" in contact

    brochure = _pages(_company(pricing_model=None))
    assert "there is no cart or online checkout" not in brochure["contact.html"]


def test_a_long_catalogue_is_capped_and_says_so():
    rows = [{"name": f"Product {n}", "retail": 10 + n} for n in range(60)]
    index = _pages(_company(pricing_model={"skus": rows}))["index.html"]
    assert index.count('<li><span class="product-name">') == site_service.MAX_PRODUCTS
    assert "10 more product(s) are recorded in the company profile" in index


def test_the_caller_title_is_the_one_brand_on_every_page():
    """F-16 (wave 2): the create form's title must not leave one site with two brands."""
    pages = _pages(_company(), title="Hearth & Lather")
    brand = '<p class="brand">Hearth &amp; Lather</p>'
    assert "<title>Hearth &amp; Lather</title>" in pages["index.html"]
    assert "<title>About Hearth &amp; Lather</title>" in pages["about.html"]
    assert "<title>Contact Hearth &amp; Lather</title>" in pages["contact.html"]
    assert '<h1>Hearth &amp; Lather</h1>' in pages["index.html"]
    for name in PAGES:
        assert brand in pages[name], name
        # The company name is no longer the brand when the caller named the site.
        assert '<p class="brand">Copper &amp; Pine Candle Co.</p>' not in pages[name]


def test_with_no_title_the_company_name_is_still_the_brand_everywhere():
    pages = _pages(_company())
    brand = '<p class="brand">Copper &amp; Pine Candle Co.</p>'
    for name in PAGES:
        assert brand in pages[name], name
    assert "<title>About Copper &amp; Pine Candle Co.</title>" in pages["about.html"]
    assert "<title>Contact Copper &amp; Pine Candle Co.</title>" in pages["contact.html"]


def test_catalogue_pages_stay_byte_deterministic():
    company = _company(pricing_model=SKUS_MODEL)
    first = _pages(company)
    second = _pages(company)
    assert first == second
    assert ".product-list" in first["styles.css"]
