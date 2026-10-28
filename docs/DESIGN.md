# Design: how the charge control works

Technical reference. You do not need this to install the system.

## Electrical model

```
            main fuse: input_number.ev_house_max_current (e.g. 25 A)
            measured as max(L1, L2, L3) by the house energy meter
                               |
              +----------------+----------------+
              |                                 |
        household load                  shared EV circuit
                                        16 A, 3-phase
                                        (input_number.ev_shared_max_current)
                                           |          |
                                         car 1      car 2
```

Two hard limits apply at once:

1. `max(L1, L2, L3) <= house limit`: the main fuse.
2. `sum(car amps) <= shared max`: the EV circuit breaker.

If both cars hang off one circuit with no load-sharing hardware, **the software
is the only thing keeping their sum under that breaker.** The breaker still
protects the wiring; the software only keeps it from tripping.

## Components

| Piece | Role |
|---|---|
| `charge_amps_limit.py` | 1 Hz balancer. Reads phase currents + car telemetry, decides per-car amps, sends BLE commands. |
| `charge_timing.py` | Once a night picks the cheapest spot-price window and arms each car's scheduled charging. |
| TeslaMate → MQTT → HA | Car telemetry: battery %, charging state, actual current, plugged in. |
| TeslaBleHttpProxy | Sends commands (`set_charging_amps`, `charge_start/stop`, `set_scheduled_charging`) over Bluetooth. No Tesla cloud API needed for commands. |
| energy meter | Per-phase current of the whole house, updated every 1–3 s. The ground truth. |

## Allocation

Budget: `min(house_headroom, shared_max) - trim`, where
`house_headroom = limit - 2 A margin - estimated household load`.

Split by **water-filling**: equal shares capped by each car's demand, with the
remainder redistributed. Two cars wanting everything get 8 A/8 A; if one can
only take 5 A the other gets 11 A. Values are **floored**, never rounded up.

Demand per car is `min(desired_amps, 16, learned_ceiling, charge_current_request_max)`.
The ceiling drops only on strong evidence of genuine under-use (3 min at an
unchanged command, fresh settled telemetry, not tapering) and ratchets back up
+1 A per 10 min, only into headroom that is already free.

**Priority when the budget cannot give both cars 5 A:** the car with the lower
battery % keeps charging; the fuller one is paused via `charge_stop`. Swapping
which car is paused requires an 8-point SoC gap and 30 minutes since the last
swap. Otherwise the charging car's SoC crosses the paused car's within minutes
and they ping-pong.

## Safety model

**Layer 1: model-free trim.** Uses only the measured `max_phase`, never car
telemetry. While over the limit the trim grows every tick, so the budget
strictly decreases until the cars are at zero. Stale or wrong car data cannot
defeat it.

**Layer 2: reservations, not reports.** `sum(reserved) <= shared_max` at all
times. A reservation rises at *send* time and falls only after the reduction has
settled (20 s) or the car confirms. Without this, rebalancing 5+11 → 8+8 would
momentarily reserve 19 A on a 16 A breaker.

**Freshness.** Every input carries an age, and unknowns are worst-cased: an
unobserved car is treated as drawing 16 A for circuit math and 0 A for the
household decomposition. Discrete states (`charging_state`, `plugged_in`) are
trusted for 30 min because TeslaMate publishes them only on change, but only
while *something* from that car is still arriving (15 min liveness). Otherwise
a dead TeslaMate would look like a fleet of idle cars. Live charger current is
trusted for 90 s only. An unknown car's reservation is bounded by the meter: it
cannot be drawing more than the whole house is.

**Dynamics are asymmetric, down fast and up slow:** instant reductions vs a
45 s raise dwell and +2 A ramp; 20 s pause dwell vs 120 s resume dwell and a
300 s minimum pause; instant household-load rise vs ~10 s decay.

**Escalation:** throttle both cars → pause the fuller car → emergency drop to
5 A (`> limit + 2`) → `charge_stop` all cars (`> limit + 4` for 10 s).
If the meter goes stale: >10 s no increases, >30 s force 5 A, >90 s stop
everything, latched until 60 s of good data.

## BLE command path

The proxy holds **one BLE connection at a time**. A command for a different VIN
costs a ~2–3 s reconnect. The balancer therefore sends commands from a
dedicated dispatcher thread through a coalescing queue: a newer amps command
for a car supersedes the pending one rather than queueing behind it.

The dispatcher must be a real thread: AppDaemon pins an app's callbacks to one
thread, so a blocking BLE call made from a callback would stall the 1 Hz
evaluator.

All cars are enrolled against the proxy's **single** key.

## Scheduling

`charge_timing.py` runs at 22:55 (configurable). All plugged-in cars share one
cheapest window at a split current rather than queueing for separate windows,
because they share one circuit. The window is sized by the slowest car
(`soc_per_hour_16a`, reduced in sub-zero temperatures). Hourly prices are the
mean of the 15-minute spot slots; windows touching unpublished hours are
skipped.

It arms scheduled charging and sets a **safe initial current**
(`shared_max / number of cars`). Starting both at 16 A would briefly draw 32 A.

A car with scheduled charging armed refuses `charge_start`, so the balancer
clears the schedule before resuming a car it paused. `charge_timing` will not
re-arm a car that is already charging.

## Operational notes

- **AppDaemon hot-reload does not pick up `apps.yaml` changes** reliably.
  Restart it after editing: `docker compose restart appdaemon`.
- `sensor.ev_charge_allocation` carries the full decision state. AppDaemon
  strips zero/false attributes, so the authoritative persisted state is the
  `ctrl_json` attribute. It is how the controller survives its own restarts.
- At startup the balancer logs any missing entity it expects.
- Tests: `python3 tests/test_charge_amps_limit.py && python3 tests/test_charge_timing.py`
  (no Home Assistant or car needed).

## Known gaps

- **No fast actuator.** Nothing can cut car load faster than BLE (~10 s, and
  only if the car is awake). A contactor on the EV circuit would give a hard
  stop.
- **Meter coverage is assumed.** If the meter does not measure the EV circuit,
  the household subtraction is wrong in the over-allocating direction. Verify:
  when a car goes 0 → N A, the phase currents must rise by about N within 10 s.
- **Priority uses raw battery %**, so a car at 79 % with an 80 % limit outranks
  one at 75 % with a 90 % limit.
