"""Smoke tests — catch the kind of breakage that broke tradar on 2026-05-09.

What this tests:
  1. App boots (imports + create_app + DB init don't throw)
  2. Every Jinja template parses (catches missing filters, BuildErrors,
     undefined macros) — this would have caught the missing `dt` filter and
     the wrong `crypto.coin` endpoint name.
  3. Every GET endpoint responds with status < 500 — catches 404s from
     hardcoded paths that don't match the blueprint's url_prefix, and 500s
     from missing imports.
  4. Critical templates render with realistic context (the dashboard banner,
     halt overrides, etc.).

Skips:
  - Endpoints requiring POST (those need wiring per-test; smoke is GET-only)
  - Endpoints that hit Binance API live (those need network)
  - Endpoints requiring path params we can't fabricate (most have safe dummies)
"""
from __future__ import annotations

import pytest


def test_app_boots(app):
    """Just creating the app shouldn't throw."""
    assert app is not None
    assert app.url_map is not None


def test_url_prefix_is_tradar(app):
    """Tradar's blueprint MUST register under /tradar — this guards against
    accidental copy from stock (which uses /crypto)."""
    crypto_rules = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("crypto.")]
    assert crypto_rules, "no crypto endpoints registered at all"
    assert all(r.rule.startswith("/tradar") for r in crypto_rules), (
        f"some crypto endpoints don't use /tradar prefix: "
        f"{[r.rule for r in crypto_rules if not r.rule.startswith('/tradar')]}"
    )


def test_dt_filter_registered(app):
    """The `dt` Jinja filter must exist (used by 4 templates).
    Missing this filter caused TemplateAssertionError on /tradar/ on 2026-05-09."""
    assert "dt" in app.jinja_env.filters


def test_all_templates_compile(app):
    """Every template in webapp/crypto/templates/ must parse without
    Jinja errors (TemplateSyntaxError, TemplateAssertionError, BuildError).
    This catches: missing filters, wrong url_for endpoint names, undefined
    macros, malformed Jinja syntax."""
    import os
    from jinja2.exceptions import (
        TemplateSyntaxError, TemplateAssertionError, UndefinedError,
    )
    from werkzeug.routing import BuildError

    template_dir = os.path.join(
        os.path.dirname(__file__), "..", "webapp", "crypto", "templates"
    )
    template_files = [
        f for f in os.listdir(template_dir) if f.endswith(".html")
    ]
    assert template_files, "no templates found — paths wrong?"

    failures = []
    with app.test_request_context():
        for name in template_files:
            try:
                tpl = app.jinja_env.get_template(name)
                # Render with empty context — surfaces Jinja-level errors
                # (filter missing, BuildError) but not missing-data errors.
                tpl.render()
            except (TemplateSyntaxError, TemplateAssertionError, BuildError) as e:
                failures.append(f"{name}: {type(e).__name__}: {e}")
            except UndefinedError:
                # Expected: many templates need data context (rows, readout, etc).
                # Not a code bug; only surfaces when the actual route handler
                # doesn't pass enough data — which is a separate test below.
                pass

    assert not failures, "Template compile errors:\n" + "\n".join(failures)


# Endpoints that GET safely (return 200/302/4xx but not 500).
# Path params use safe dummy values that won't blow up DB queries.
GET_ENDPOINTS = [
    ("crypto.dashboard", {}),
    ("crypto.holdings", {}),
    ("crypto.universe", {}),
    ("crypto.journal", {}),
    ("crypto.runs", {}),
    ("crypto.settings", {}),
    ("crypto.simulation", {}),
    ("crypto.coin_detail", {"symbol": "BTCUSDT"}),
    ("crypto.api_dashboard_static", {}),
    # NOTE: crypto.api_dashboard hits Binance — skip for smoke
    # NOTE: crypto.api_journal_prices hits Binance — skip for smoke
    ("crypto.api_journal", {}),
    ("crypto.api_server_ip", {}),
]


@pytest.mark.parametrize("endpoint,params", GET_ENDPOINTS)
def test_get_endpoint_doesnt_500(client, app, endpoint, params):
    """Every GET endpoint should return < 500.
    402/404/400 are acceptable (e.g. holdings without API keys redirects).
    A 500 means our code blew up — the kind of bug that would surface as
    'Failed to load: Unexpected token <' in the browser."""
    with app.test_request_context():
        from flask import url_for
        url = url_for(endpoint, **params)
    resp = client.get(url, follow_redirects=False)
    assert resp.status_code < 500, (
        f"GET {url} returned {resp.status_code}\n"
        f"Response (first 500 chars): {resp.get_data(as_text=True)[:500]}"
    )


def test_root_redirects_to_dashboard(client):
    """`/` must redirect somewhere under /tradar/ (not 404 or to /crypto/)."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.location.startswith("/tradar") or resp.location.startswith("http://localhost.test/tradar"), (
        f"root redirect went to wrong place: {resp.location}"
    )


def test_dashboard_works_in_paper_mode(client, app):
    """In paper mode, dashboard must not 500 even with NO Binance keys.
    This is the regression fixed on 2026-05-09 (account_value: null bug).
    The old code bailed to nulls when keys were missing; now it synthesizes
    usdt_free from trade history."""
    with app.app_context():
        from webapp.models import Setting, db
        # Force paper mode + no Binance keys
        for key, value in [
            ("crypto_trading_mode", "paper"),
            ("binance_api_key", ""),
            ("binance_api_secret", ""),
            ("crypto_starting_capital_usd", "200.29"),
        ]:
            row = Setting.query.get(key)
            if row:
                row.value = value
            else:
                db.session.add(Setting(key=key, value=value))
        db.session.commit()

    # Dashboard page should render
    resp = client.get("/tradar/")
    assert resp.status_code in (200, 302), f"dashboard returned {resp.status_code}"

    # The LIVE dashboard endpoint must return non-null usdt_free in paper mode.
    # /api/dashboard/static intentionally returns nulls (design — JS overlays
    # live values from /api/dashboard). The bug we're guarding against is
    # /api/dashboard returning nulls in paper mode (which it did pre-fix).
    resp = client.get("/tradar/api/dashboard")
    assert resp.status_code == 200
    data = resp.get_json()
    # In paper mode with no trades and starting_capital=200.29:
    # usdt_free should equal starting capital (no buys yet to reduce it)
    assert data["account"]["usdt_free"] is not None, (
        f"usdt_free is None in paper mode — regression!\nResponse: {data}"
    )
    assert data["account"]["account_value"] is not None, (
        f"account_value is None in paper mode — regression!\nResponse: {data}"
    )


def test_dashboard_doesnt_500_in_live_mode_without_keys(client, app):
    """In live mode WITHOUT keys, dashboard should still render (returns the
    null skeleton, which is the documented expected behavior). Just shouldn't
    crash."""
    with app.app_context():
        from webapp.models import Setting, db
        for key, value in [
            ("crypto_trading_mode", "live"),
            ("binance_api_key", ""),
            ("binance_api_secret", ""),
        ]:
            row = Setting.query.get(key)
            if row:
                row.value = value
            else:
                db.session.add(Setting(key=key, value=value))
        db.session.commit()

    resp = client.get("/tradar/")
    assert resp.status_code in (200, 302)
    resp = client.get("/tradar/api/dashboard/static")
    assert resp.status_code == 200


def test_required_endpoints_exist(app):
    """Endpoints that frontend templates reference via url_for must exist.
    This guards against typos like 'crypto.coin' (wrong) vs 'crypto.coin_detail'."""
    required = [
        "crypto.dashboard", "crypto.api_dashboard", "crypto.api_dashboard_static",
        "crypto.coin_detail", "crypto.api_journal", "crypto.api_journal_prices",
        "crypto.api_manual_buy", "crypto.api_sell_position",
        "crypto.api_partial_sell_position", "crypto.api_sell_all",
        "crypto.api_halts_halt_now", "crypto.api_halts_override",
        "crypto.api_halts_end_and_rearm", "crypto.settings", "crypto.holdings",
        "crypto.universe", "crypto.journal", "crypto.simulation",
        "crypto.api_server_ip", "crypto.api_deposits_refresh",
        "crypto.sync_balances", "crypto.run_loop_now",
    ]
    registered = {r.endpoint for r in app.url_map.iter_rules()}
    missing = [ep for ep in required if ep not in registered]
    assert not missing, f"endpoints referenced in templates but not registered: {missing}"
