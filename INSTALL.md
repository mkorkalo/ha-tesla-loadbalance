# Installation

Step by step. Commands go into a terminal on the computer that will run the
system. `<IP>` means that computer's address on your home network (find it
with `hostname -I`, first number).

Check the hardware, network and accounts in the [README](README.md#what-you-need)
first.

## 1. Prepare the computer

1. Install the operating system (Raspberry Pi OS 64-bit or Ubuntu 24.04) and
   connect it to your network.
2. In your router, give it a fixed address (DHCP reservation). Do the same
   for the Shelly energy meter.
3. Install Docker, git and Bluetooth tools:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   sudo apt-get install -y git bluez
   ```

   Log out and back in, so the `docker` command works without `sudo`.
4. Check Bluetooth works. This should print one controller:

   ```bash
   bluetoothctl list
   ```

## 2. Download and start

```bash
git clone https://github.com/mkorkalo/ha-tesla-loadbalance.git
cd ha-tesla-loadbalance
cp .env.example .env
```

Edit `.env` (`nano .env`; save with Ctrl+O, exit with Ctrl+X). Set your time
zone and fill both secrets with random text from `openssl rand -base64 32`.

```bash
docker compose up -d
```

The first start downloads a few gigabytes and can take 10–20 minutes.
`docker compose ps` should then list six running services.

## 3. Home Assistant

1. Open `http://<IP>:8123` and create your user.
2. **MQTT:** Settings → Devices & services → Add integration → **MQTT**.
   Broker `localhost`, port `1883`, no username or password.
3. **Energy meter:**
   - *Shelly Pro 3EM:* it is usually discovered automatically. Add it under
     Settings → Devices & services. Note the names of its three
     *phase current* sensors (e.g. `sensor.shellypro3em_phase_a_current`).
   - *Shelly 3EM (older model):* enable MQTT on the Shelly's own web page
     (Internet & Security → Advanced → MQTT, server `<IP>:1883`). Then use
     *option A* in the package file (step 6).
4. **Access token for AppDaemon:** click your name (bottom left) → Security →
   *Long-lived access tokens* → Create. Copy it somewhere for step 6.
5. **Enable packages:** add these lines to `data/homeassistant/configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

## 4. TeslaMate (car data)

1. Open `http://<IP>:4000`.
2. Sign in with Tesla **API tokens**. Create them with a token app such as
   *Auth app for Tesla* (iOS) or *Tesla Tokens* (Android).
3. Your cars appear in the list. Note each car's number: the first car added
   is `1`, the second `2`. Open a car and the number is in the address bar.

## 5. Bluetooth key for each car

Do this with the car **parked at home**, within Bluetooth range.

1. Open `http://<IP>:8080/dashboard`.
2. Generate a key (once).
3. For each car: enter its VIN and send the key to the car. Then, **inside
   the car, tap your key card on the card reader** behind the cup holders
   and confirm on the screen.
4. The proxy's page should then show the car as connected. If the page looks
   different, follow the proxy's own guide:
   <https://github.com/wimaha/TeslaBleHttpProxy>.

## 6. Configure

```bash
cp appdaemon/secrets.example.yaml appdaemon/secrets.yaml
cp examples/apps.yaml appdaemon/apps/apps.yaml
cp examples/ev_charging.yaml homeassistant/packages/ev_charging.yaml
```

Edit each:

- **`appdaemon/secrets.yaml`:** paste the access token from step 3, and set
  your time zone.
- **`appdaemon/apps/apps.yaml`:** your cars' VINs, TeslaMate numbers, a
  short name (`prefix`) for each car, and the three phase-current sensor
  names. Also set your price area and extra costs. Comments in the file explain
  each line.
- **`homeassistant/packages/ev_charging.yaml`:**
  - Set `ev_house_max_current` to your **main fuse size** and
    `ev_shared_max_current` to the **car circuit breaker size**.
  - Rename `model3` / `modely` to the prefixes you chose, keeping the
    capitalisation pattern (`Model3` in names, `model3` in ids).
  - Set the car numbers in the `teslamate/cars/N/...` topics.
  - One car only: delete the "car 2" blocks.

Apply:

```bash
docker compose restart homeassistant appdaemon
```

## 7. Check it works

1. After a minute, check the AppDaemon log:

   ```bash
   docker compose logs --tail 50 appdaemon
   ```

   You want to see `ChargeAmpsLimit started for ...` and, a minute later,
   `All required entities present`. A `Missing entities` line names exactly
   what is wrong (usually a typo in a prefix or sensor name).
2. In Home Assistant → Developer tools → States, search `ev_charge_allocation`.
   Its value should change every second or so.
3. Add the dashboard card from [examples/dashboard.yaml](examples/dashboard.yaml).
4. **Live test:** plug in a car and start charging from the Tesla app. Within
   a minute or two, the allocation should show the car's amps. Then switch on
   something big (oven, kettle, sauna) and watch the car's current go down.
5. **Meter check:** when a car starts charging at N amps, the phase
   currents in Home Assistant must rise by about N. If they don't, the meter
   is not measuring the car circuit: get your electrician to fix it before
   relying on the system.

## Updating

```bash
cd ha-tesla-loadbalance
git pull
docker compose pull
docker compose up -d
docker compose restart appdaemon
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing entities` in the AppDaemon log | A name in `apps.yaml` doesn't match Home Assistant. Compare with Developer tools → States. |
| Car entities show `unknown` | MQTT integration not added, wrong car number in topics, or TeslaMate not signed in. |
| Allocation stuck at `idle (SAFE)` | The energy meter stopped updating. Check the Shelly is online and its sensors change every few seconds. |
| Commands fail (`set_charging_amps failed` in the log) | Car out of Bluetooth range, or key not approved. Move the computer closer or add a USB Bluetooth adapter. |
| "EV charge balancer is not running" notification | AppDaemon stopped. `docker compose logs appdaemon`, then `docker compose restart appdaemon`. |
| Charging doesn't start at the planned time | Check *Tonight's plan* in Home Assistant, and that the car is plugged in before 22:55. |
