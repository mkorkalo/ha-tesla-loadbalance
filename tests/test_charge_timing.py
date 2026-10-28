"""Offline tests for the nightly cheapest-window search.

Run:  python3 tests/test_charge_timing.py   (or `pytest tests/`)
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "appdaemon", "apps"))

from charge_timing import cheapest_window, hourly_prices  # noqa: E402

NOW = datetime(2026, 1, 14, 22, 55)    # Wednesday evening -> Thursday ready-by


def flat_night(price=0.10, **overrides):
    """Hourly prices from 23:00 today to 08:00 tomorrow."""
    keys = ["2026-01-14T23"] + [f"2026-01-15T{h:02d}" for h in range(0, 9)]
    prices = {k: price for k in keys}
    prices.update(overrides)
    return prices


def test_hourly_prices_average_quarter_slots():
    slots = [{"DateTime": f"2026-01-15T02:{m:02d}:00+02:00", "PriceWithTax": p}
             for m, p in ((0, 0.01), (15, 0.02), (30, 0.03), (45, 0.10))]
    assert abs(hourly_prices(slots)["2026-01-15T02"] - 0.04) < 1e-9


def test_picks_cheapest_block():
    prices = flat_night(**{"2026-01-15T03": 0.01, "2026-01-15T04": 0.01})
    start, _ = cheapest_window(prices, 2, NOW, ready_hour=7, kw=11.0)
    assert start == 3


def test_window_must_finish_by_ready_hour():
    # 06:00 is cheapest, but a 2 h block starting there would end at 08:00.
    prices = flat_night(**{"2026-01-15T06": 0.0, "2026-01-15T07": 0.0})
    start, _ = cheapest_window(prices, 2, NOW, ready_hour=7, kw=11.0)
    assert start + 2 <= 7


def test_unpublished_hours_are_not_free():
    """A missing price must not be treated as 0 EUR."""
    prices = flat_night()
    del prices["2026-01-15T01"]
    start, _ = cheapest_window(prices, 1, NOW, ready_hour=7, kw=11.0)
    assert start != 1


def test_no_prices_raises_so_caller_falls_back():
    try:
        cheapest_window({}, 2, NOW, ready_hour=7, kw=11.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_price_adders_included_in_cost():
    _, base = cheapest_window(flat_night(0.10), 1, NOW, ready_hour=7, kw=10.0)
    _, more = cheapest_window(flat_night(0.10), 1, NOW, ready_hour=7, kw=10.0, adders=0.05)
    assert abs((more - base) - 0.5) < 1e-9


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in fns:
        try:
            f()
            print(f"  PASS  {n}")
        except Exception as e:
            bad += 1
            print(f"  FAIL  {n}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
