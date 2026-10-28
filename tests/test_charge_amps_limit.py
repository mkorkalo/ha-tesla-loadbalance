"""Offline tests for the two-car charge allocator control law.

decide() is pure, so the nasty cases (stale telemetry, transient breaker
overrun, SoC ping-pong, meter loss, reload-while-paused) can be exercised
without AppDaemon, Home Assistant or two real cars.

Run:  python3 tests/test_charge_amps_limit.py   (or `pytest tests/`)
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "appdaemon", "apps"))

from charge_amps_limit import (  # noqa: E402
    MAX_A, MIN_A, PAUSE_BELOW, T_RAISE_DWELL, T_SETTLE, T_WARMUP, decide, new_car_ctrl, new_ctrl, water_fill,
)

NOW = 10_000.0


def car(actual=0.0, cs="Charging", soc=50.0, lim=80.0, desired=16,
        plugged=True, car_state="online", req_max=16.0, age=1.0, stable=300.0):
    return {"min_age": age, "actual": actual, "actual_age": age,
            "actual_stable_for": stable,
            "charging_state": cs, "cs_age": age, "car_state": car_state,
            "state_age": age, "soc": soc, "limit_soc": lim, "req_max": req_max,
            "plugged": plugged, "desired": desired}


def mk(cars, phases=(3.0, 3.0, 3.0), limit=27.0, shared=16.0, meter_age=1.0,
       now=NOW, ctrl=None, warm=True, **kw):
    c = ctrl or new_ctrl()
    c["started_at"] = now - (T_WARMUP + 10) if warm else now
    for k in cars:
        c["cars"].setdefault(k, new_car_ctrl())
    st = {"now": now, "ctrl": c, "cars": cars, "order": list(cars),
          "phases": {"l1": phases[0], "l2": phases[1], "l3": phases[2]},
          "meter_age": meter_age, "limit": limit, "shared_max": shared}
    st.update(kw)
    return st


def amps(cmds, k):
    return next((c["amps"] for c in cmds if c["car"] == k and c["kind"] == "amps"), None)


def settle(st, ticks=3, dt=T_SETTLE + 1):
    """Advance time so in-flight commands settle, re-running decide()."""
    out = []
    for _ in range(ticks):
        st["now"] += dt
        out, _ = decide(st)
    return out


# --- basic allocation ----------------------------------------------------

def test_water_fill_equal_split():
    assert water_fill(16, {"a": 16, "b": 16}, ["a", "b"]) == {"a": 8, "b": 8}


def test_water_fill_reclaims_unused():
    # 'a' can only take 5 -> 'b' gets the rest, not a flat half.
    assert water_fill(16, {"a": 5, "b": 16}, ["a", "b"]) == {"a": 5, "b": 11}


def test_water_fill_floors_never_rounds_up():
    # 13 A over two cars: 6.5 each -> floored, spare amp to the priority car.
    got = water_fill(13, {"a": 16, "b": 16}, ["a", "b"])
    assert sum(got.values()) <= 13 and got == {"a": 7, "b": 6}


def test_two_cars_split_5050():
    st = mk({"a": car(soc=50), "b": car(soc=50)})
    cmds, diag = decide(st)
    assert amps(cmds, "a") == 8 and amps(cmds, "b") == 8
    assert diag["shared_reserved"] <= 16


def test_single_car_gets_full_budget():
    st = mk({"a": car(), "b": car(cs="Disconnected", plugged=False)})
    cmds, _ = decide(st)
    assert amps(cmds, "a") == MAX_A


# --- the safety invariant ------------------------------------------------

def test_shared_circuit_never_exceeded_during_rebalance():
    """5+11 -> 8+8 must never transiently reserve 19 A on a 16 A circuit."""
    st = mk({"a": car(actual=5, soc=50, desired=5), "b": car(actual=11, soc=50)})
    decide(st)
    settle(st, ticks=4)
    st["cars"]["a"]["desired"] = 16          # 'a' now wants its full share back
    for _ in range(40):
        st["now"] += 5
        decide(st)
        total = sum(st["ctrl"]["cars"][k]["reserved"] for k in ("a", "b"))
        assert total <= 16, f"shared circuit exceeded: {total} A"


def test_stale_car_telemetry_cannot_inflate_headroom():
    """A car reporting 16 A while drawing 0 must not create phantom headroom."""
    fresh = car(actual=0.0, cs="Charging", soc=50)
    stale = car(actual=16.0, cs="Charging", soc=50, age=500.0)   # way past TESLA_FRESH
    st = mk({"a": fresh, "b": stale}, phases=(20.0, 20.0, 20.0), limit=27.0)
    _, diag = decide(st)
    # 'b' is unknown -> assumed 16 A reserved, and contributes 0 to the
    # household decomposition, so 'other' must be the full measured 20 A.
    assert diag["other"] >= 19.0, diag
    assert diag["budget"] <= 6.0, diag


def test_trim_forces_budget_down_while_over_limit():
    """Model-free layer: over limit -> budget strictly decreases every tick."""
    st = mk({"a": car(actual=8), "b": car(actual=8)}, phases=(30.0, 30.0, 30.0), limit=27.0)
    prev = None
    for _ in range(12):
        st["now"] += 1
        _, diag = decide(st)
        if prev is not None:
            assert diag["budget"] <= prev, "budget must not rise while over limit"
        prev = diag["budget"]
    assert prev == 0.0, f"trim failed to drive budget to zero (got {prev})"


def test_emergency_reduces_to_minimum_immediately():
    st = mk({"a": car(actual=8), "b": car(actual=8)}, phases=(35.0, 35.0, 35.0), limit=27.0)
    cmds, diag = decide(st)
    assert diag["mode"].startswith("EMERGENCY")
    assert all(c["urgent"] for c in cmds)


def test_meter_loss_stops_everything_and_latches():
    st = mk({"a": car(actual=8), "b": car(actual=8)}, meter_age=200.0)
    cmds, diag = decide(st)
    assert diag["mode"] == "SAFE"
    assert {c["kind"] for c in cmds} == {"stop"}
    # Latched: still SAFE on the very next tick even with fresh data again.
    st["meter_age"] = 1.0
    st["now"] += 1
    _, diag2 = decide(st)
    assert diag2["mode"] == "SAFE", "SAFE must latch until data is good for a while"


# --- pause / resume ------------------------------------------------------

def test_pauses_higher_soc_car_when_budget_too_small():
    """Below 10 A both cars cannot hold 5 A -> the fuller car is paused."""
    st = mk({"a": car(actual=4, soc=75), "b": car(actual=4, soc=30)},
            phases=(24.0, 24.0, 24.0), limit=27.0)   # -> budget 9 A, below the 10 A floor
    for _ in range(5):
        st["now"] += 10
        cmds, diag = decide(st)
        if diag.get("paused"):
            break
    assert diag["budget"] < PAUSE_BELOW
    assert diag["paused"] == ["a"], f"expected the 80% car paused, got {diag['paused']}"


def test_no_soc_pingpong_when_levels_cross():
    """The charging car's SoC crossing the paused car's must not trigger a swap."""
    st = mk({"a": car(actual=4, soc=61), "b": car(actual=4, soc=60)},
            phases=(24.0, 24.0, 24.0), limit=27.0)   # -> budget 9 A, forces a pause
    for _ in range(5):
        st["now"] += 10
        _, diag = decide(st)
    assert diag["paused"] == ["a"]
    swaps = 0
    prev = list(diag["paused"])
    for i in range(300):                      # ~50 min, SoC crossing mid-way
        st["now"] += 10
        st["cars"]["b"]["soc"] = 60 + i * 0.02   # b climbs past a
        _, diag = decide(st)
        if diag["paused"] != prev:
            swaps += 1
            prev = list(diag["paused"])
    assert swaps == 0, f"paused car swapped {swaps} times on an SoC crossing"


def test_reload_keeps_a_deliberately_paused_car_paused():
    ctrl = new_ctrl()
    ctrl["cars"]["a"] = new_car_ctrl()
    ctrl["cars"]["a"].update({"paused_by_us": True, "paused_since": NOW, "reserved": 0})
    ctrl["cars"]["b"] = new_car_ctrl()
    st = mk({"a": car(cs="Stopped", soc=70), "b": car(actual=8, soc=30)},
            ctrl=ctrl, warm=False)
    cmds, diag = decide(st)
    assert "a" in diag["paused"]
    assert not any(c["kind"] == "start" for c in cmds), "must not un-pause during warmup"


def test_resume_needs_scheduled_charging_disabled_first():
    ctrl = new_ctrl()
    ctrl["cars"]["a"] = new_car_ctrl()
    ctrl["cars"]["a"].update({"paused_by_us": True, "paused_since": NOW - 9999,
                              "reserved": 0, "commanded": MIN_A})
    ctrl["cars"]["b"] = new_car_ctrl()
    ctrl["resume_since"] = NOW - 9999
    ctrl["last_swap"] = 0.0
    st = mk({"a": car(cs="Stopped", soc=20), "b": car(cs="Disconnected", plugged=False)},
            ctrl=ctrl)
    cmds, _ = decide(st)
    kinds = [c["kind"] for c in cmds if c["car"] == "a"]
    assert "start" in kinds, kinds
    assert kinds.index("unschedule") < kinds.index("start"), \
        "scheduled charging must be cleared before charge_start or the car re-stops"


def test_pause_parks_car_at_minimum_before_stopping():
    """An idle car remembers its amps; park at 5 A so a replug can't start at 16."""
    st = mk({"a": car(actual=4, soc=75), "b": car(actual=4, soc=30)},
            phases=(24.0, 24.0, 24.0), limit=27.0)   # -> budget 9 A, below the 10 A floor
    for _ in range(5):
        st["now"] += 10
        cmds, diag = decide(st)
        if diag.get("paused"):
            break
    seq = [c["kind"] for c in cmds if c["car"] == "a"]
    assert seq.index("amps") < seq.index("stop")
    assert amps(cmds, "a") == MIN_A


# --- raise behaviour -----------------------------------------------------

def test_increases_are_ramped_not_jumped():
    st = mk({"a": car(actual=5, soc=50, desired=5), "b": car(actual=5, soc=50)})
    decide(st)
    settle(st, ticks=4)
    st["cars"]["a"]["desired"] = 16
    seen = []
    for _ in range(60):
        st["now"] += 10
        cmds, _ = decide(st)
        v = amps(cmds, "b")
        if v is not None:
            seen.append(v)
    assert seen, "expected 'b' to be raised"
    for prev, nxt in zip(seen, seen[1:]):
        assert nxt - prev <= 2, f"raise jumped {prev} -> {nxt}, must ramp"


def test_no_commands_in_steady_state():
    """Idle chatter costs BLE radio time; steady state must be silent."""
    st = mk({"a": car(actual=8, soc=50), "b": car(actual=8, soc=50)})
    decide(st)
    settle(st, ticks=4)
    quiet = 0
    for _ in range(30):
        st["now"] += 1
        cmds, _ = decide(st)
        quiet += len(cmds)
    assert quiet == 0, f"{quiet} commands emitted in steady state"


def test_unknown_car_is_not_commanded():
    """Regression: AppDaemon initialize() can run before HA finishes starting,
    so every seed read returns None. A car we know nothing about must not be
    treated as active and woken over BLE for nothing."""
    unknown = {"min_age": 1e9, "actual": None, "actual_age": 1e9, "actual_stable_for": 0,
               "charging_state": None, "cs_age": 1e9, "car_state": None,
               "state_age": 1e9, "soc": None, "limit_soc": None,
               "req_max": None, "plugged": None, "desired": 16}
    st = mk({"a": unknown, "b": car(actual=6, soc=40)}, phases=(8.0, 8.0, 8.0))
    cmds, diag = decide(st)
    assert not any(c["car"] == "a" for c in cmds), "must not command an unknown car"
    # ...while the other car is still served normally.
    assert amps(cmds, "b") is not None


def test_disconnected_car_is_not_commanded():
    st = mk({"a": car(cs="Disconnected", plugged=False), "b": car(actual=6, soc=40)})
    cmds, _ = decide(st)
    assert not any(c["car"] == "a" for c in cmds)


def test_parked_car_with_old_discrete_state_is_still_idle():
    """TeslaMate publishes discrete states only on CHANGE, so a parked car's
    'Disconnected' is routinely ~10 min old. It must still release its share."""
    parked = car(cs="Disconnected", plugged=False, car_state="suspended", age=665.0)
    parked["min_age"] = 60.0          # something from this car arrived recently
    st = mk({"a": parked, "b": car(actual=6, soc=40)}, phases=(8.0, 8.0, 8.0))
    decide(st)
    assert st["ctrl"]["cars"]["a"]["reserved"] == 0, \
        "a confirmed-idle car must not hold a reservation"


def test_dead_teslamate_makes_cars_unknown_not_idle():
    """If NOTHING is arriving for a car, an old 'Disconnected' must not be
    trusted -- the car could have been plugged in since."""
    gone = car(cs="Disconnected", plugged=False, car_state="suspended", age=5000.0)
    gone["min_age"] = 5000.0
    st = mk({"a": gone, "b": car(actual=6, soc=40)}, phases=(8.0, 8.0, 8.0))
    decide(st)
    assert st["ctrl"]["cars"]["a"]["reserved"] > 0, \
        "a car with no telemetry at all must be treated as an unknown consumer"


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
