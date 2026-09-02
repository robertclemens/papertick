"""Nasdaq as the keyless backstop: equities and ETFs only, last in the chain."""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import get_settings
from app.services.market_data import (
    MarketDataError,
    NasdaqProvider,
    SymbolNotSupported,
    market_data,
)


def test_nasdaq_never_asks_for_mutual_funds():
    """The omission is the safety mechanism.

    Nasdaq's `mutualfunds` series is unadjusted for splits — FCNTX on
    2018-08-08 comes back as $138.17 against a true $13.82. Never requesting
    that asset class is what stops a fund being priced an order of magnitude
    wrong; a fund simply finds no data here."""
    assert "mutualfunds" not in NasdaqProvider.ASSET_CLASSES
    assert set(NasdaqProvider.ASSET_CLASSES) == {"stocks", "etf"}


def test_an_unsupported_symbol_does_not_put_the_provider_in_cool_down(monkeypatch):
    """A provider declining a symbol it never covered is not a provider that
    is broken. Marking it down took Nasdaq out of the chain for the equities
    it serves perfectly well, every time a fund was priced."""
    marked = []
    monkeypatch.setattr(market_data, "_mark_down", lambda p: marked.append(p.name))
    monkeypatch.setattr(market_data, "_available", lambda p: True)

    class Fussy:
        name = "fussy"
        def quote(self, ticker):
            raise SymbolNotSupported("not my asset class")

    class Works:
        name = "works"
        def quote(self, ticker):
            return "served"

    monkeypatch.setattr(market_data, "_chain", lambda: [Fussy(), Works()])
    assert market_data._try_chain(lambda p: p.quote("X")) == "served"
    assert marked == [], "declining a symbol must not trigger a cool-down"


def test_a_broken_provider_still_gets_cooled_down(monkeypatch):
    marked = []
    monkeypatch.setattr(market_data, "_mark_down", lambda p: marked.append(p.name))
    monkeypatch.setattr(market_data, "_available", lambda p: True)

    class Broken:
        name = "broken"
        def quote(self, ticker):
            raise httpx.ConnectError("down")

    class Works:
        name = "works"
        def quote(self, ticker):
            return "served"

    monkeypatch.setattr(market_data, "_chain", lambda: [Broken(), Works()])
    assert market_data._try_chain(lambda p: p.quote("X")) == "served"
    assert marked == ["broken"]


def test_nasdaq_sits_last_behind_yahoo(monkeypatch):
    """It serves daily closes, not live prints, so it is a backstop not a peer."""
    s = get_settings()
    monkeypatch.setattr(s, "market_data_provider", "auto", raising=False)
    monkeypatch.setattr(s, "nasdaq_fallback", True, raising=False)
    names = [p.name for p in market_data._chain()]
    assert names[-1] == "nasdaq"
    assert names.index("yahoo") < names.index("nasdaq")


def test_the_fallback_can_be_switched_off(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "market_data_provider", "auto", raising=False)
    monkeypatch.setattr(s, "nasdaq_fallback", False, raising=False)
    assert "nasdaq" not in [p.name for p in market_data._chain()]


def test_synthetic_is_still_not_in_the_chain(monkeypatch):
    """Adding a fallback must not have reopened the door to invented prices."""
    s = get_settings()
    monkeypatch.setattr(s, "market_data_provider", "auto", raising=False)
    assert "synthetic" not in [p.name for p in market_data._chain()]


def test_equity_fixtures_exist_so_an_equity_only_provider_can_be_verified():
    """Nasdaq reaches no fund fixture, so without an equity fixture that
    separates dividend adjustment it could never be proven — and an
    unverifiable provider that is still used is the gap the probe closes."""
    from app.services.convention import FIXTURES

    funds = {"VWELX"}
    equity_only = [f for f in FIXTURES if f.ticker not in funds]
    covered = set()
    for f in equity_only:
        covered |= f.discriminates()
    assert {"raw", "total_return"} <= covered, \
        "equity fixtures alone must rule out both wrong conventions"
