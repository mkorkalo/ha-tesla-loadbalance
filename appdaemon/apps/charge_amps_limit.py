"""Multi-vehicle EV charge current balancer.

The cars charge 3-phase from ONE shared circuit (default 16 A). There is no
hardware arbitration -- this app is the only thing keeping their sum under the
breaker. It also keeps whole-house max phase current under the main fuse limit.

Safety model (see docs/DESIGN.md):

  Layer 1 - model-free trim. Derived only from the MEASURED max phase current,
    never from car telemetry. While max_phase > limit the budget strictly
    decreases every tick until the cars are at zero. No amount of stale or
    wrong car data can defeat it.

  Layer 2 - reservations, not reports. sum(reserved) <= shared_max at ALL
    times. A reservation rises at send time and falls only after the reduction
    has settled. This is what stops a rebalance from transiently exceeding the
    shared breaker (e.g. 5+11 -> 8+8 would otherwise momentarily draw 19 A).

Car telemetry (TeslaMate) lags commands by up to ~60 s, so every input carries
an age and any unknown is assumed worst-case: an unobserved car is treated as
drawing MAX_A for circuit math and 0 A for the household decomposition.

Dynamics are deliberately asymmetric everywhere: down fast, up slow.

decide() is a pure function of a state dict -- no I/O, no AppDaemon -- so the
control law can be unit tested offline. See tests/test_charge_amps_limit.py.

All entity ids and URLs are app arguments; see apps.example.yaml.
"""

import json
import math
import queue
import threading
import time
from datetime import datetime, timezone

try:  # both absent when unit-testing the control law outside AppDaemon
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import appdaemon.plugins.hass.hassapi as hass
    _Base = hass.Hass
except ImportError:  # pragma: no cover
    _Base = object

# --- current limits -------------------------------------------------------
MIN_A = 5           # below this a Tesla must be STOPPED, not throttled
MAX_A = 16
DEFAULT_HOUSE_LIMIT = 27.0
DEFAULT_SHARED_MAX = 16.0
HOUSE_MARGIN = 2.0  # allocate against limit - margin (EM3 samples ~1-2.5 s)
EMERG_MARGIN = 2.0
HARD_MARGIN = 4.0

# --- timings (seconds, all monotonic) -------------------------------------
T_SETTLE = 20        # command sent -> assume the car has applied it
T_STOP_CONFIRM = 60  # charge_stop -> release its reservation
T_RAISE_DWELL = 45   # an increase must be justified continuously this long
RAMP_STEP = 2        # max amps added per raise event (decreases unlimited)
T_WARMUP = 60        # after (re)start: reductions only

PAUSE_BELOW = 10.0
T_PAUSE_DWELL = 20
RESUME_ABOVE = 12.0   # 2 A hysteresis band against household noise
T_RESUME_DWELL = 120
MIN_PAUSE = 300
SOC_HYST = 8          # battery-% gap required to swap which car is paused
MIN_SWAP_INTERVAL = 1800

T_CEIL_DWELL = 180    # before concluding a car under-uses its allocation
T_PROBE = 600         # ratchet a ceiling back up (+1 A) this often

TESLA_FRESH = 90      # charger current: fast-moving, trust only if recent
# TeslaMate publishes discrete states only on CHANGE, so a parked car's
# "Disconnected" can be hours old and still perfectly true. Trust those over a
# much longer horizon, but only while SOMETHING from that car is arriving --
# otherwise a dead TeslaMate would look like a fleet of idle cars.
STATE_FRESH = 1800
LIVE_FRESH = 900
METER_FRESH = 10
METER_DEGRADE = 30
METER_SAFE = 90
T_SAFE_RECOVER = 60   # continuous good data needed to leave SAFE

IDLE_STATES = ("Disconnected", "Stopped", "Complete", "NoPower")
# TeslaMate vehicle states that cannot coexist with drawing current.
NOT_CHARGING_STATES = ("asleep", "suspended", "offline")

# Defaults for app arguments; each can be overridden in apps.yaml.
DEFAULTS = {
    "ble_proxy_url": "http://127.0.0.1:8080",
    "house_limit_entity": "input_number.ev_house_max_current",
    "shared_max_entity": "input_number.ev_shared_max_current",
    "allocation_sensor": "sensor.ev_charge_allocation",
}

# (state field, entity suffix, cast) for each car, keyed off its prefix.
CAR_FIELDS = (
    ("actual", "sensor.{p}_charger_actual_current", "f"),
    ("charging_state", "sensor.{p}_charging_state", "s"),
    ("soc", "sensor.{p}_battery_level", "f"),
    ("limit_soc", "sensor.{p}_charge_limit_soc", "f"),
    ("req_max", "sensor.{p}_charge_current_request_max", "f"),
    ("car_state", "sensor.{p}_state", "s"),
    ("plugged", "binary_sensor.{p}_plugged_in", "b"),
)


# =========================================================================
# Pure control law
# =========================================================================

def new_car_ctrl():
    return {
        "commanded": None,      # last amps we sent
        "reserved": MAX_A,      # circuit reservation; worst case until observed
        "sent_at": 0.0,
        "ceiling": MAX_A,
        "paused_since": None,
        "paused_by_us": False,
        "stop_sent_at": None,
        "raise_since": None,
        "raise_target": None,
        "ceil_since": None,
        "probe_at": 0.0,
        "resume_fail": 0,
    }


def new_ctrl():
    return {
        "trim": 0.0,
        "other": 0.0,
        "under_since": None,     # max_phase continuously under limit since
        "over_since": None,      # ... over limit since
        "safe_until": 0.0,
        "started_at": 0.0,
        "last_swap": 0.0,
        "pause_since": None,     # budget continuously below PAUSE_BELOW
        "resume_since": None,
        "cars": {},
    }


def _bounds(c, cc):
    """Conservative (lo, hi) amps a car may currently be drawing.

    lo is used to work out household load (an UNDER-estimate of the car makes
    household load an OVER-estimate -> less headroom -> safe).
    hi is used for shared-circuit math (an OVER-estimate -> safe).
    """
    live = c.get("min_age", 1e9) <= LIVE_FRESH
    st_fresh = live and c.get("state_age", 1e9) <= STATE_FRESH
    cs_fresh = live and c.get("cs_age", 1e9) <= STATE_FRESH
    # A Tesla cannot sleep while charging: this is a definitive zero.
    if st_fresh and c.get("car_state") in NOT_CHARGING_STATES:
        return 0.0, 0.0
    if cs_fresh and c.get("charging_state") in IDLE_STATES:
        return 0.0, 0.0
    if c.get("actual") is not None and c.get("actual_age", 1e9) <= TESLA_FRESH:
        cmd = cc["commanded"] if cc["commanded"] is not None else c["actual"]
        return min(c["actual"], cmd), max(c["actual"], cc["reserved"])
    return 0.0, max(cc["reserved"], MAX_A)


def _active(c, cc):
    """Does this car want charge right now?

    Requires POSITIVE evidence that it is connected. Absent telemetry must not
    read as "active", or a car that is away from home gets commanded (and its
    BLE radio woken) for nothing. Circuit safety does not depend on this -- an
    unknown car is still reserved at MAX_A by the reservation logic.
    """
    if cc["resume_fail"] >= 4:
        return False
    cs = c.get("charging_state")
    if cs in ("Disconnected", "Complete"):
        return False
    if c.get("plugged") is False:
        return False
    soc, lim = c.get("soc"), c.get("limit_soc")
    if soc is not None and lim is not None and soc >= lim:
        return False
    if c.get("plugged") is not True and cs not in ("Charging", "Starting", "Stopped"):
        return False
    return True


def water_fill(budget, demands, order):
    """Equal split capped by demand, remainder redistributed. Floors to int."""
    alloc = {k: 0.0 for k in demands}
    unsat = [k for k in order if demands.get(k, 0) > 0]
    remaining = float(budget)
    while unsat and remaining > 1e-9:
        share = remaining / len(unsat)
        done = [k for k in unsat if demands[k] <= share]
        if not done:
            for k in unsat:
                alloc[k] = share
            remaining = 0.0
            break
        for k in done:
            alloc[k] = float(demands[k])
            remaining -= demands[k]
            unsat.remove(k)
    out = {k: int(math.floor(v)) for k, v in alloc.items()}
    # Hand any floored-away amp to the first car in priority order that can use it.
    spare = int(math.floor(budget)) - sum(out.values())
    for k in order:
        if spare <= 0:
            break
        room = demands.get(k, 0) - out.get(k, 0)
        take = min(spare, max(0, room))
        out[k] = out.get(k, 0) + take
        spare -= take
    return out


def decide(st):
    """Return (commands, diag). Mutates st['ctrl'] in place.

    commands: list of dicts {car, kind, amps?, urgent}
              kind in {"amps", "stop", "start", "unschedule"}
    """
    now = st["now"]
    ctrl = st["ctrl"]
    cars = st["cars"]
    limit = st["limit"]
    shared_max = st["shared_max"]
    order = st["order"]
    cmds, diag = [], {}

    for k in order:
        ctrl["cars"].setdefault(k, new_car_ctrl())

    phases = st.get("phases") or {}
    max_phase = max(phases.values()) if phases else None
    meter_age = st.get("meter_age", 1e9)

    # --- 1. meter freshness gate -----------------------------------------
    if max_phase is None or meter_age > METER_SAFE:
        mode = "SAFE"
    elif meter_age > METER_DEGRADE:
        mode = "DEGRADE"
    elif meter_age > METER_FRESH:
        mode = "FREEZE"
    else:
        mode = "NORMAL"
    if mode == "SAFE":
        ctrl["safe_until"] = now + T_SAFE_RECOVER
    elif now < ctrl["safe_until"]:
        mode = "SAFE"  # latched until continuously good again
    warming = now - ctrl["started_at"] < T_WARMUP

    if mode == "SAFE":
        for k in order:
            cc = ctrl["cars"][k]
            if not cc["paused_by_us"]:
                cmds.append({"car": k, "kind": "stop", "urgent": True})
                cc["paused_by_us"] = True
                cc["paused_since"] = now
                cc["stop_sent_at"] = now
        diag = {"mode": "SAFE", "reason": "meter data stale", "budget": 0}
        return cmds, diag

    # --- 2. household load, frozen while a command is in flight ----------
    lo_hi = {k: _bounds(cars[k], ctrl["cars"][k]) for k in order}
    cars_lo = sum(v[0] for v in lo_hi.values())
    in_flight = any(now - ctrl["cars"][k]["sent_at"] < T_SETTLE for k in order)
    if not in_flight:
        raw = max(0.0, max_phase - cars_lo)
        if raw > ctrl["other"]:
            ctrl["other"] = raw                       # rises instantly
        else:
            ctrl["other"] += (raw - ctrl["other"]) * 0.1   # decays ~10 s
    other = ctrl["other"]

    # --- 3. model-free trim + budget -------------------------------------
    if max_phase > limit:
        ctrl["trim"] += (max_phase - limit) + 0.5
        ctrl["under_since"] = None
        ctrl["over_since"] = ctrl["over_since"] or now
    else:
        ctrl["over_since"] = None
        if max_phase < limit - 2.0:
            ctrl["under_since"] = ctrl["under_since"] or now
            if now - ctrl["under_since"] >= 30:
                ctrl["trim"] = max(0.0, ctrl["trim"] - 1.0)
                ctrl["under_since"] = now
        else:
            ctrl["under_since"] = None

    house_headroom = max(0.0, limit - HOUSE_MARGIN - other)
    budget = max(0.0, min(house_headroom, shared_max) - ctrl["trim"])

    # --- 4. emergency lane ------------------------------------------------
    over_5s = ctrl["over_since"] and now - ctrl["over_since"] >= 5
    if max_phase > limit + HARD_MARGIN and over_5s and now - ctrl["over_since"] >= 10:
        for k in order:
            cc = ctrl["cars"][k]
            if not cc["paused_by_us"]:
                cmds.append({"car": k, "kind": "stop", "urgent": True})
                cc["paused_by_us"] = True
                cc["paused_since"] = now
                cc["stop_sent_at"] = now
        diag = {"mode": "EMERGENCY-STOP", "reason": f"max_phase {max_phase:.1f} > {limit + HARD_MARGIN:.1f}",
                "budget": 0, "trim": round(ctrl["trim"], 1)}
        return cmds, diag
    if max_phase > limit + EMERG_MARGIN or over_5s:
        # Biggest consumer first: largest reduction per BLE round-trip.
        for k in sorted(order, key=lambda x: -lo_hi[x][1]):
            cc = ctrl["cars"][k]
            if cc["paused_by_us"] or cc["commanded"] == MIN_A:
                continue
            cmds.append({"car": k, "kind": "amps", "amps": MIN_A, "urgent": True})
            cc["commanded"] = MIN_A
            cc["reserved"] = max(MIN_A, cc["reserved"])  # lowered only on settle
            cc["sent_at"] = now
        if cmds:
            diag = {"mode": "EMERGENCY", "reason": f"max_phase {max_phase:.1f} > {limit:.1f}",
                    "budget": round(budget, 1), "trim": round(ctrl["trim"], 1)}
            return cmds, diag

    # --- settle reservations down (only once the reduction has landed) ----
    # A reservation may ONLY fall here. Everywhere else it rises at send time.
    for k in order:
        cc, c = ctrl["cars"][k], cars[k]
        live = c.get("min_age", 1e9) <= LIVE_FRESH
        cs_fresh = live and c.get("cs_age", 1e9) <= STATE_FRESH
        st_fresh = live and c.get("state_age", 1e9) <= STATE_FRESH
        idle = ((cs_fresh and c.get("charging_state") in IDLE_STATES)
                or (st_fresh and c.get("car_state") in NOT_CHARGING_STATES))
        rep, rep_fresh = c.get("actual"), c.get("actual_age", 1e9) <= TESLA_FRESH

        if cc["paused_by_us"] and cc["stop_sent_at"] is not None:
            stopped = cs_fresh and c.get("charging_state") in IDLE_STATES
            if stopped or now - cc["stop_sent_at"] >= T_STOP_CONFIRM:
                cc["reserved"] = 0
                cc["stop_sent_at"] = None
        elif idle and not cc["paused_by_us"]:
            cc["reserved"] = 0            # confirmed not drawing: release share
        elif cc["commanded"] is not None:
            if cc["reserved"] > cc["commanded"]:
                settled = now - cc["sent_at"] >= T_SETTLE
                confirmed = rep is not None and rep_fresh and rep <= cc["commanded"] + 0.5
                if settled or confirmed:
                    cc["reserved"] = cc["commanded"]
        elif rep is not None and rep_fresh:
            # No command issued yet (cold start): reserve what it actually draws.
            cc["reserved"] = int(math.ceil(rep))
        else:
            # Draw unknown. The meter is ground truth: a car cannot be drawing
            # more than the whole house is. Without this bound an unknown car
            # holds a full MAX_A reservation and starves the other one forever.
            cc["reserved"] = int(min(MAX_A, math.ceil(max_phase)))

    # --- 5. demand --------------------------------------------------------
    active = [k for k in order if _active(cars[k], ctrl["cars"][k])]
    demands = {}
    for k in active:
        c, cc = cars[k], ctrl["cars"][k]
        # Lower the learned ceiling only on strong evidence of under-use.
        if (cc["commanded"] is not None
                and now - cc["sent_at"] >= T_CEIL_DWELL
                and c.get("actual_age", 1e9) <= TESLA_FRESH
                and c.get("actual_stable_for", 0) >= 90
                and c.get("charging_state") == "Charging"
                and c.get("actual") is not None
                and c["actual"] <= cc["commanded"] - 2
                and (c.get("limit_soc") or 100) - (c.get("soc") or 0) > 3):
            cc["ceiling"] = max(MIN_A, int(c["actual"]) + 1)
        # Ratchet back up slowly, and only into headroom that is already free.
        if now >= cc["probe_at"]:
            cc["probe_at"] = now + T_PROBE
            if cc["ceiling"] < MAX_A and budget > sum(
                    ctrl["cars"][j]["reserved"] for j in active):
                cc["ceiling"] += 1
        req_max = c.get("req_max")
        d = min(c.get("desired", MAX_A), MAX_A, cc["ceiling"])
        if req_max:
            d = min(d, int(req_max))
        demands[k] = max(0, int(d))

    # --- 6/7. pause & allocate -------------------------------------------
    # Priority: lower battery % keeps charging. Swapping requires a real gap
    # and a long dwell, else the charging car's SoC crosses the paused car's
    # within minutes and they ping-pong.
    def soc_of(k):
        v = cars[k].get("soc")
        return v if v is not None else 100.0

    prio = sorted(active, key=soc_of)  # lowest SoC first = highest priority

    running = [k for k in active if not ctrl["cars"][k]["paused_by_us"]]
    paused = [k for k in active if ctrl["cars"][k]["paused_by_us"]]

    if len(active) >= 2 and budget < PAUSE_BELOW:
        ctrl["pause_since"] = ctrl["pause_since"] or now
    else:
        ctrl["pause_since"] = None
    if budget >= RESUME_ABOVE:
        ctrl["resume_since"] = ctrl["resume_since"] or now
    else:
        ctrl["resume_since"] = None

    # pause the lowest-priority (highest SoC) running car
    if (len(running) >= 2 and ctrl["pause_since"]
            and now - ctrl["pause_since"] >= T_PAUSE_DWELL):
        victim = max(running, key=soc_of)
        cc = ctrl["cars"][victim]
        # Park at MIN_A first: a replug or self-start then begins at 5 A, not 16.
        cmds.append({"car": victim, "kind": "amps", "amps": MIN_A, "urgent": False})
        cmds.append({"car": victim, "kind": "stop", "urgent": False})
        cc["commanded"] = MIN_A
        cc["sent_at"] = now
        cc["paused_by_us"] = True
        cc["paused_since"] = now
        cc["stop_sent_at"] = now
        running.remove(victim)
        paused.append(victim)
        ctrl["pause_since"] = None

    # resume a paused car when there is durable room
    if (paused and mode == "NORMAL" and not warming
            and ctrl["resume_since"] and now - ctrl["resume_since"] >= T_RESUME_DWELL):
        cand = min(paused, key=soc_of)
        cc = ctrl["cars"][cand]
        may_swap = True
        if running:
            best_running = min(running, key=soc_of)
            if soc_of(cand) > soc_of(best_running) - SOC_HYST:
                may_swap = False
            if now - ctrl["last_swap"] < MIN_SWAP_INTERVAL:
                may_swap = False
        if (cc["paused_since"] and now - cc["paused_since"] >= MIN_PAUSE
                and may_swap
                and sum(ctrl["cars"][j]["reserved"] for j in running) + MIN_A <= shared_max):
            # scheduled charging must be off or the car refuses / re-stops
            cmds.append({"car": cand, "kind": "unschedule", "urgent": False})
            cmds.append({"car": cand, "kind": "amps", "amps": MIN_A, "urgent": False})
            cmds.append({"car": cand, "kind": "start", "urgent": False})
            cc["commanded"] = MIN_A
            cc["reserved"] = MIN_A
            cc["sent_at"] = now
            cc["paused_by_us"] = False
            cc["paused_since"] = None
            ctrl["last_swap"] = now
            running.append(cand)
            paused.remove(cand)
            ctrl["resume_since"] = None

    # single active car that cannot even get the minimum -> stop it too
    if len(running) == 1 and budget < MIN_A:
        k = running[0]
        cc = ctrl["cars"][k]
        if ctrl["pause_since"] is None:
            ctrl["pause_since"] = now
        if now - ctrl["pause_since"] >= T_PAUSE_DWELL:
            cmds.append({"car": k, "kind": "amps", "amps": MIN_A, "urgent": False})
            cmds.append({"car": k, "kind": "stop", "urgent": True})
            cc["commanded"] = MIN_A
            cc["sent_at"] = now
            cc["paused_by_us"] = True
            cc["paused_since"] = now
            cc["stop_sent_at"] = now
            running = []

    alloc = water_fill(budget, {k: demands.get(k, 0) for k in running}, prio)

    # --- dispatch amp changes, honouring the shared-circuit invariant -----
    for k in prio:
        if k not in running:
            continue
        cc = ctrl["cars"][k]
        target = max(MIN_A, min(MAX_A, alloc.get(k, 0)))
        cur = cc["commanded"]
        # With no command yet, what the car has reserved is the effective setting.
        eff = cur if cur is not None else cc["reserved"]

        if cur is not None and target == eff:
            continue                            # already there

        if target <= eff:
            # Reduction, or a first command at/below present draw: send at once.
            # reserved is NOT lowered here - that happens on settle.
            cmds.append({"car": k, "kind": "amps", "amps": target, "urgent": False})
            cc["commanded"] = target
            cc["sent_at"] = now
            cc["raise_since"] = None
            cc["raise_target"] = None
            continue

        # Increase: slow, dwelled, ramp-limited, and never past the circuit cap.
        if warming or mode != "NORMAL":
            continue
        if cc["raise_target"] != target:
            cc["raise_target"] = target
            cc["raise_since"] = now
        if cur is not None and now - (cc["raise_since"] or now) < T_RAISE_DWELL:
            continue
        step = target if cur is None else min(target, cur + RAMP_STEP)
        # Take whatever the shared circuit still allows rather than nothing --
        # skipping entirely would starve this car while the other holds a
        # reservation it may not even be using.
        others = sum(ctrl["cars"][j]["reserved"] for j in order if j != k)
        room = int(shared_max) - others
        if room < MIN_A:
            continue
        step = min(step, room)
        cmds.append({"car": k, "kind": "amps", "amps": step, "urgent": False})
        cc["commanded"] = step
        cc["reserved"] = step                   # rises at send time
        cc["sent_at"] = now
        cc["raise_since"] = None
        cc["raise_target"] = None

    diag = {
        "mode": mode,
        "budget": round(budget, 1),
        "other": round(other, 1),
        "trim": round(ctrl["trim"], 1),
        "max_phase": round(max_phase, 1),
        "house_headroom": round(house_headroom, 1),
        "shared_reserved": sum(ctrl["cars"][k]["reserved"] for k in order),
        "alloc": {k: alloc.get(k, 0) for k in running},
        "paused": paused,
    }
    return cmds, diag


# =========================================================================
# AppDaemon adapter
# =========================================================================

class ChargeAmpsLimit(_Base):

    def initialize(self):
        cfg = {**DEFAULTS, **self.args}
        self.cars_cfg = cfg["cars"]
        self.order = list(self.cars_cfg.keys())
        self.ble_base = cfg["ble_proxy_url"].rstrip("/") + "/api/1/vehicles"
        self.house_limit_entity = cfg["house_limit_entity"]
        self.shared_max_entity = cfg["shared_max_entity"]
        self.alloc_sensor = cfg["allocation_sensor"]
        phase_entities = cfg["phase_current_sensors"]
        if len(phase_entities) != 3:
            raise ValueError("phase_current_sensors must list exactly 3 entities (L1, L2, L3)")
        self.lock = threading.RLock()
        self.q = queue.Queue()
        self.urgent = queue.Queue()
        self.meas = {}
        self.ctrl = new_ctrl()
        self.ctrl["started_at"] = time.monotonic()
        self._restore()

        for k, car in self.cars_cfg.items():
            p = car["prefix"]
            self.meas[k] = {}
            for field, ent, kind in CAR_FIELDS:
                self.listen_state(self._on_car, ent.format(p=p),
                                  car=k, field=field, cast=kind)

        self.phases = {}
        self.phase_ts = 0.0
        for ent, ph in zip(phase_entities, ("l1", "l2", "l3")):
            try:
                self.phases[ph] = float(self.get_state(ent))
            except (TypeError, ValueError):
                pass
            self.listen_state(self._on_phase, ent, phase=ph)

        self.worker = threading.Thread(target=self._dispatch_loop, daemon=True)
        self.worker.start()
        self._refresh()
        self.run_every(self._refresh, "now+20", 20)
        self.run_every(self._evaluate, "now+2", 1)
        # HA may still be starting when AppDaemon initializes; check later.
        self.run_in(self._check_entities, 60, required=self._required_entities(phase_entities))
        self.log(f"ChargeAmpsLimit started for {', '.join(self.order)}")

    def _required_entities(self, phase_entities):
        ents = list(phase_entities) + [self.house_limit_entity, self.shared_max_entity]
        for cfg in self.cars_cfg.values():
            p = cfg["prefix"]
            ents += [ent.format(p=p) for _, ent, _ in CAR_FIELDS]
            ents.append(f"input_number.{p}_desired_charge_amps")
        return ents

    def _check_entities(self, kwargs):
        missing = [e for e in kwargs["required"] if not self.entity_exists(e)]
        if missing:
            self.log(f"Missing entities (fix the HA package or apps.yaml): {', '.join(missing)}",
                     level="WARNING")
        else:
            self.log("All required entities present")

    # ---- state plumbing -------------------------------------------------
    def _refresh(self, kwargs=None):
        """Re-read every car entity and derive its age from Home Assistant's
        own last_reported.

        listen_state only fires on CHANGE, so a parked car whose telemetry is
        steady would otherwise look indefinitely stale and get treated as an
        unknown (worst-case) consumer. HA knows when the value was last
        reported even when it did not change, so ask it.
        """
        now = time.monotonic()
        for k, cfg in self.cars_cfg.items():
            p = cfg["prefix"]
            for field, ent, kind in CAR_FIELDS:
                obj = self.get_state(ent.format(p=p), attribute="all")
                if not isinstance(obj, dict):
                    continue
                val = self._cast(obj.get("state"), kind)
                if val is None:
                    continue
                age = self._age(obj.get("last_reported") or obj.get("last_updated"))
                with self.lock:
                    m = self.meas[k]
                    if field == "actual" and m.get("actual") != val:
                        m["actual_changed"] = now
                    m[field] = val
                    m[field + "_ts"] = now - age

    @staticmethod
    def _age(ts):
        """Seconds since an HA ISO timestamp; large number if unparseable."""
        if not ts:
            return 1e9
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
        except (TypeError, ValueError):
            return 1e9

    @staticmethod
    def _cast(v, kind):
        if v in (None, "unknown", "unavailable", ""):
            return None
        if kind == "f":
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        if kind == "b":
            return v == "on"
        return str(v)

    def _on_car(self, entity, attribute, old, new, kwargs):
        val = self._cast(new, kwargs["cast"])
        if val is None:
            return
        k, f = kwargs["car"], kwargs["field"]
        with self.lock:
            m = self.meas[k]
            if f == "actual" and m.get("actual") != val:
                m["actual_changed"] = time.monotonic()
            m[f] = val
            m[f + "_ts"] = time.monotonic()

    def _on_phase(self, entity, attribute, old, new, kwargs):
        val = self._cast(new, "f")
        if val is None:
            return
        with self.lock:
            self.phases[kwargs["phase"]] = val
            self.phase_ts = time.monotonic()

    def _build_state(self):
        now = time.monotonic()
        cars = {}
        for k in self.order:
            m = self.meas[k]
            ages = [now - m[f + "_ts"] for f, _, _ in CAR_FIELDS if f + "_ts" in m]
            cars[k] = {
                "min_age": min(ages) if ages else 1e9,
                "actual": m.get("actual"),
                "actual_age": now - m.get("actual_ts", -1e9),
                "actual_stable_for": now - m.get("actual_changed", now),
                "charging_state": m.get("charging_state"),
                "cs_age": now - m.get("charging_state_ts", -1e9),
                "car_state": m.get("car_state"),
                "state_age": now - m.get("car_state_ts", -1e9),
                "soc": m.get("soc"),
                "limit_soc": m.get("limit_soc"),
                "req_max": m.get("req_max"),
                "plugged": m.get("plugged"),
                "desired": int(self._num(
                    f"input_number.{self.cars_cfg[k]['prefix']}_desired_charge_amps", MAX_A)),
            }
        return {
            "now": now,
            "ctrl": self.ctrl,
            "cars": cars,
            "order": self.order,
            "phases": dict(self.phases),
            "meter_age": now - self.phase_ts if self.phase_ts else 1e9,
            "limit": self._num(self.house_limit_entity, DEFAULT_HOUSE_LIMIT),
            "shared_max": self._num(self.shared_max_entity, DEFAULT_SHARED_MAX),
        }

    def _num(self, entity, default):
        try:
            return float(self.get_state(entity))
        except (TypeError, ValueError):
            return float(default)

    # ---- evaluator ------------------------------------------------------
    def _evaluate(self, kwargs):
        with self.lock:
            st = self._build_state()
            try:
                cmds, diag = decide(st)
            except Exception as e:  # never let the loop die
                self.log(f"decide() failed: {e}", level="ERROR")
                return
            for c in cmds:
                (self.urgent if c.get("urgent") else self.q).put(c)
            self._publish(diag)

    def _publish(self, diag):
        parts = [f"{k} {v}A" for k, v in (diag.get("alloc") or {}).items()]
        parts += [f"{k} paused" for k in diag.get("paused", [])]
        state = " · ".join(parts) if parts else f"idle ({diag.get('mode','?')})"
        # AppDaemon's set_state drops zero/false/None attributes and coerces
        # bools to strings, which would silently corrupt the persisted state
        # (reserved: 0 would simply vanish). Persist it as a JSON string.
        persist = {k: {kk: v[kk] for kk in
                       ("commanded", "reserved", "ceiling", "paused_by_us")}
                   for k, v in self.ctrl["cars"].items()}
        attrs = {"friendly_name": "EV charge allocation", **diag}
        attrs["ctrl"] = persist          # human-readable (lossy, display only)
        attrs["ctrl_json"] = json.dumps(persist)   # lossless, used by _restore
        attrs["shared_reserved_str"] = str(diag.get("shared_reserved"))
        self.set_state(self.alloc_sensor, state=state[:255], attributes=attrs)

    def _restore(self):
        raw = self.get_state(self.alloc_sensor, attribute="ctrl_json")
        try:
            prev = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            prev = None
        if not isinstance(prev, dict):
            return
        for k, v in prev.items():
            cc = new_car_ctrl()
            cc.update({kk: v[kk] for kk in ("commanded", "reserved", "ceiling",
                                            "paused_by_us") if kk in v})
            # An unobserved car is assumed to be drawing MAX_A until proven otherwise.
            cc["reserved"] = max(cc.get("reserved") or 0, MAX_A) if cc["commanded"] is None \
                else cc["reserved"]
            # A car we had deliberately paused must STAY paused across a reload.
            if cc["paused_by_us"]:
                cc["paused_since"] = time.monotonic()
            self.ctrl["cars"][k] = cc
        self.log(f"Restored controller state for {list(prev)}")

    # ---- dispatcher (own thread: AppDaemon pins app callbacks to one
    #      thread, so a blocking BLE call here would stall the evaluator) --
    def _dispatch_loop(self):
        last_vin = None
        while True:
            try:
                try:
                    cmd = self.urgent.get(timeout=0.25)
                except queue.Empty:
                    try:
                        cmd = self.q.get(timeout=0.75)
                    except queue.Empty:
                        continue
                # Coalesce: a newer amps command for the same car supersedes.
                cmd = self._coalesce(cmd)
                vin = self.cars_cfg[cmd["car"]]["vin"]
                if last_vin and vin != last_vin:
                    time.sleep(0.5)  # let the proxy close the old connection
                self._send(cmd, vin)
                last_vin = vin
            except Exception as e:  # pragma: no cover
                self.log(f"dispatch error: {e}", level="WARNING")

    def _coalesce(self, cmd):
        if cmd["kind"] != "amps":
            return cmd
        drained = []
        while True:
            try:
                nxt = self.q.get_nowait()
            except queue.Empty:
                break
            if nxt["kind"] == "amps" and nxt["car"] == cmd["car"]:
                cmd = nxt          # newest wins; stale increase is discarded
            else:
                drained.append(nxt)
        for d in drained:
            self.q.put(d)
        return cmd

    def _send(self, cmd, vin):
        kind = cmd["kind"]
        base = f"{self.ble_base}/{vin}/command"
        try:
            if kind == "amps":
                requests.post(f"{base}/set_charging_amps?wait=true",
                              json={"charging_amps": int(cmd["amps"])}, timeout=20)
                self.log(f"{cmd['car']} -> {cmd['amps']}A{' (urgent)' if cmd.get('urgent') else ''}")
            elif kind == "stop":
                requests.post(f"{base}/charge_stop?wait=true", timeout=20)
                self.log(f"{cmd['car']} -> charge_stop")
            elif kind == "start":
                requests.post(f"{base}/charge_start?wait=true", timeout=20)
                self.log(f"{cmd['car']} -> charge_start")
            elif kind == "unschedule":
                requests.post(f"{base}/set_scheduled_charging?wait=true",
                              json={"enable": False, "time": 0}, timeout=20)
        except Exception as e:
            self.log(f"{cmd['car']} {kind} failed: {e}", level="WARNING")
