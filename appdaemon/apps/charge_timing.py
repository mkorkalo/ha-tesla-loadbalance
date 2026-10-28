"""Nightly spot-price charge scheduling for the fleet.

All cars share ONE circuit, so they charge concurrently in the same cheapest
block at a split current rather than queueing for separate windows. The window
is therefore sized by the slowest car.

Interaction with charge_amps_limit: this app ARMS scheduled charging; the
balancer owns current allocation once charging starts. If the balancer has to
pause a car it must clear scheduled charging to resume it (a car with a
schedule armed refuses charge_start), so this app deliberately does not re-arm
a car that is already charging.

cheapest_window() is pure and unit tested; see tests/test_charge_timing.py.
"""

import math
from datetime import datetime, timedelta

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import appdaemon.plugins.hass.hassapi as hass
    _Base = hass.Hass
except ImportError:  # pragma: no cover
    _Base = object

CHARGE_PHASES = 3
VOLTAGE = 230
MIN_AMPS = 5
DEFAULT_SHARED_MAX = 16.0
COLD_FACTOR = 0.07          # %SOC/hour lost per °C below zero
PRICE_URL = "https://api.spot-hinta.fi/TodayAndDayForward?region={region}"

# Defaults for app arguments; each can be overridden in apps.yaml.
DEFAULTS = {
    "ble_proxy_url": "http://127.0.0.1:8080",
    "teslamate_url": None,             # optional: resume TeslaMate logging at start
    "shared_max_entity": "input_number.ev_shared_max_current",
    "price_region": "FI",
    "price_adders_eur_kwh": 0.0,       # grid transfer + tax + retailer margin
    "schedule_time": "22:55:00",
    "ready_by_hour_weekday": 7,
    "ready_by_hour_weekend": 8,
    "fallback_start_hour": 23,
}


def hourly_prices(slots):
    """{'YYYY-MM-DDTHH': mean EUR/kWh} from spot-hinta.fi slots (15 or 60 min)."""
    buckets = {}
    for p in slots:
        buckets.setdefault(p["DateTime"][:13], []).append(p["PriceWithTax"])
    return {h: sum(v) / len(v) for h, v in buckets.items()}


def cheapest_window(prices, window_hours, now, ready_hour, kw, adders=0.0):
    """Cheapest start hour for a block of `window_hours` that ends by
    `ready_hour` tomorrow. Windows touching an hour without a published price
    are skipped -- treating a missing price as free would always pick them.

    Returns (start_hour 0-23, estimated cost EUR). Raises ValueError if none fits.
    """
    latest_start = ready_hour - window_hours
    best_hour, best_cost = None, float("inf")
    for h in range(now.hour + 1, 24 + latest_start + 1):
        keys = [_hour_key(now, h + i) for i in range(window_hours)]
        if any(k not in prices for k in keys):
            continue
        cost = sum((adders + prices[k]) * kw for k in keys)
        if cost < best_cost:
            best_cost, best_hour = cost, h % 24
    if best_hour is None:
        raise ValueError("no viable charging window")
    return best_hour, best_cost


def _hour_key(now, hour_offset):
    dt = now + timedelta(hours=hour_offset - now.hour)
    return dt.strftime("%Y-%m-%dT%H")


class ChargeTiming(_Base):

    def initialize(self):
        self.cfg = {**DEFAULTS, **self.args}
        self.cars_cfg = self.cfg["cars"]
        self.ble_base = self.cfg["ble_proxy_url"].rstrip("/") + "/api/1/vehicles"
        self.run_daily(self.calculate_schedule, self.cfg["schedule_time"])
        for key, cfg in self.cars_cfg.items():
            p = cfg["prefix"]
            self.listen_state(self._on_reschedule, f"input_button.reschedule_{p}_charge")
            self.listen_state(self._on_limit_change, f"input_number.{p}_charge_limit", car=key)
            self.listen_state(self._on_car_limit, f"sensor.{p}_charge_limit_soc", car=key)
        # Seed the schedule sensors: listen_state only fires on CHANGE, so after
        # a reload the debug sensors would otherwise not exist at all.
        self.run_in(lambda kw: self.calculate_schedule(kw, trigger="startup"), 15)

    # ---- helpers ---------------------------------------------------------
    def _f(self, entity, default=0.0):
        try:
            v = self.get_state(entity)
            return float(v) if v not in (None, "unknown", "unavailable", "") else default
        except (TypeError, ValueError):
            return default

    def _shared_max(self):
        return self._f(self.cfg["shared_max_entity"], DEFAULT_SHARED_MAX)

    def _on_reschedule(self, entity, attribute, old, new, kwargs):
        self.log("Manual reschedule triggered")
        self.calculate_schedule({}, force=True, trigger="manual")

    def _on_car_limit(self, entity, attribute, old, new, kwargs):
        """Keep the slider in sync when the limit is changed from the Tesla app."""
        p = self.cars_cfg[kwargs["car"]]["prefix"]
        try:
            car_pct = int(float(new))
            if car_pct != int(self._f(f"input_number.{p}_charge_limit")):
                self.call_service("input_number/set_value",
                                  entity_id=f"input_number.{p}_charge_limit", value=car_pct)
                self.log(f"{kwargs['car']}: charge limit synced from car: {car_pct}%")
        except (TypeError, ValueError) as e:
            self.log(f"_on_car_limit error: {e}", level="WARNING")

    def _on_limit_change(self, entity, attribute, old, new, kwargs):
        key = kwargs["car"]
        cfg = self.cars_cfg[key]
        try:
            pct = int(float(new))
            if pct == int(self._f(f"sensor.{cfg['prefix']}_charge_limit_soc", -1)):
                return  # echo from _on_car_limit, not a user action
            self._post(cfg["vin"], "set_charge_limit", {"percent": pct})
            self.log(f"{key}: charge limit set to {pct}%")
        except Exception as e:
            self.log(f"set_charge_limit failed: {e}", level="WARNING")

    def _post(self, vin, cmd, payload=None):
        url = f"{self.ble_base}/{vin}/command/{cmd}?wait=true"
        try:
            requests.post(url, json=payload, timeout=20) if payload else \
                requests.post(url, timeout=20)
        except Exception as e:
            self.log(f"BLE {cmd} failed: {e}", level="WARNING")

    # ---- scheduling ------------------------------------------------------
    def calculate_schedule(self, kwargs, force=False, trigger="auto"):
        state = {}
        for key, cfg in self.cars_cfg.items():
            p = cfg["prefix"]
            cs = self.get_state(f"sensor.{p}_charging_state")
            soc = self._f(f"sensor.{p}_battery_level")
            lim = self._f(f"input_number.{p}_charge_limit") or \
                self._f(f"sensor.{p}_charge_limit_soc", 80.0)
            state[key] = {
                "cfg": cfg, "cs": cs, "soc": soc, "limit": lim,
                "temp": self._f(f"sensor.{p}_outside_temp"),
                "needed": max(0.0, lim - soc),
                # Already charging: the balancer owns it, do not re-arm a
                # schedule underneath it.
                "charging": cs == "Charging",
                "eligible": cs not in ("Disconnected", None, "unknown") and lim - soc > 0,
            }

        eligible = [k for k, v in state.items() if v["eligible"] and not v["charging"]]
        if not eligible:
            for k, v in state.items():
                self._write_sensor(k, f"Skipped ({v['cs']})", trigger, v)
            self.log("No car eligible for scheduling")
            return

        try:
            prices = self._fetch_prices()
            share = 1.0 / len(eligible)
            hours = {}
            for k in eligible:
                v = state[k]
                rate = (v["cfg"]["soc_per_hour_16a"] + COLD_FACTOR * min(v["temp"], 0.0)) * share
                hours[k] = v["needed"] / max(rate, 0.5)
            window = max(1, math.ceil(max(hours.values())))
            now = datetime.now()
            tomorrow = now.date() + timedelta(days=1)
            ready_hour = int(self.cfg["ready_by_hour_weekday"] if tomorrow.weekday() < 5
                             else self.cfg["ready_by_hour_weekend"])
            kw = VOLTAGE * self._shared_max() * CHARGE_PHASES / 1000
            start_hour, cost = cheapest_window(prices, window, now, ready_hour, kw,
                                               float(self.cfg["price_adders_eur_kwh"]))

            # All cars start together on one circuit, so the initial amp
            # setting must already be the shared value split between them --
            # starting two cars at 16 A would briefly draw 32 A.
            amps = max(MIN_AMPS, int(self._shared_max() // len(eligible)))

            for k in eligible:
                cfg = state[k]["cfg"]
                self._post(cfg["vin"], "set_charging_amps", {"charging_amps": amps})
                self._post(cfg["vin"], "set_scheduled_charging",
                           {"enable": True, "time": start_hour * 60})
                self._write_sensor(k, f"Spot: {start_hour:02d}:00", trigger, state[k],
                                   hours_needed=round(hours[k], 1),
                                   window_hours=window, start_amps=amps,
                                   est_cost_eur=round(cost, 2))
                self._schedule_resume_logging(cfg, start_hour)
            for k in state:
                if k not in eligible:
                    self._write_sensor(k, f"Skipped ({state[k]['cs']})", trigger, state[k])
            self.log(f"Charge scheduled at {start_hour:02d}:00 for "
                     f"{', '.join(eligible)} @ {amps}A each "
                     f"({window}h window, est. EUR {cost:.2f})")
        except Exception as e:
            fb = int(self.cfg["fallback_start_hour"])
            self.log(f"Schedule failed ({e}), falling back to {fb:02d}:00", level="WARNING")
            amps = max(MIN_AMPS, int(self._shared_max() // max(1, len(eligible))))
            for k in eligible:
                cfg = state[k]["cfg"]
                self._post(cfg["vin"], "set_charging_amps", {"charging_amps": amps})
                self._post(cfg["vin"], "set_scheduled_charging", {"enable": True, "time": fb * 60})
                self._write_sensor(k, f"Fallback: {fb:02d}:00", trigger, state[k])

    def _schedule_resume_logging(self, cfg, start_hour):
        if not self.cfg["teslamate_url"] or "teslamate_id" not in cfg:
            return
        now = datetime.now()
        mins = start_hour * 60 - now.hour * 60 - now.minute - 1
        if mins > 0:
            self.run_in(lambda kw, c=cfg: self._resume_logging(c), mins * 60)

    def _resume_logging(self, cfg):
        url = self.cfg["teslamate_url"].rstrip("/")
        try:
            requests.put(f"{url}/api/car/{cfg['teslamate_id']}/logging/resume", timeout=10)
        except Exception as e:
            self.log(f"TeslaMate resume failed: {e}", level="WARNING")

    def _write_sensor(self, key, state, trigger, info, **extra):
        p = self.cars_cfg[key]["prefix"]
        self.set_state(
            f"sensor.{p}_charge_schedule_debug",
            state=state,
            attributes={
                "set_at": datetime.now().isoformat(),
                "trigger": trigger,
                "car": key,
                "battery_pct": info.get("soc"),
                "charge_limit_pct": info.get("limit"),
                "friendly_name": f"{p.capitalize()} charge schedule",
                **extra,
            },
        )

    def _fetch_prices(self):
        r = requests.get(PRICE_URL.format(region=self.cfg["price_region"]), timeout=10)
        r.raise_for_status()
        return hourly_prices(r.json())
