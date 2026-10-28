# Tesla smart charging for Home Assistant

Charge one or two Teslas at home **as fast as your electricity connection
allows, without blowing a fuse, at the cheapest hours of the night.**

- **Never overloads your main fuse.** It watches the whole house's power use
  every second and turns the cars' charging current down when the oven, sauna
  or heat pump comes on, then back up when they switch off.
- **Two cars on one circuit.** Both cars can charge from the same 16 A
  circuit. The system splits the current between them (e.g. 8 A + 8 A), and
  if there isn't enough for both, the emptier car charges first.
- **Cheapest hours.** Every evening it looks up tomorrow's spot electricity
  prices and schedules charging into the cheapest block that still finishes by
  morning (07:00 on weekdays, 08:00 at weekends).
- **No Tesla cloud subscription.** Commands go to the cars over Bluetooth
  from a small computer at home.

It is built on [Home Assistant](https://www.home-assistant.io/),
[TeslaMate](https://github.com/teslamate-org/teslamate),
[TeslaBleHttpProxy](https://github.com/wimaha/TeslaBleHttpProxy) and
[AppDaemon](https://appdaemon.readthedocs.io/). You don't need to know any of
them: everything starts with one command, and an AI assistant can do the setup
with you (see [Install it with an AI assistant](#install-it-with-an-ai-assistant)).

> **Safety first.** This software keeps your fuses from *tripping*. It does
> not replace them. Your fuses and breakers must still be correctly sized for
> the wiring, and any work inside the electrical panel must be done by a
> qualified electrician.

---

## What you need

### Hardware

| What | Why | Suggestions |
|---|---|---|
| **A small always-on computer with Bluetooth** | Runs everything, and talks to the cars over Bluetooth. | Raspberry Pi 5 (4 GB or more) with an SSD, *or* a mini PC with Bluetooth (e.g. Intel N100). Raspberry Pi OS 64-bit or Ubuntu 24.04. |
| **Placed near where the cars park** | Bluetooth range is short: aim for **under 10 m** with few walls between the computer and the cars. | In or next to the garage. If the computer has to sit further away, a USB Bluetooth adapter on a USB extension cable can help. |
| **A 3-phase energy meter on the house's main supply** | Tells the system how much current each phase is using right now. Must update every few seconds. | **Shelly Pro 3EM** or **Shelly 3EM**, installed in the main panel **by an electrician**. It must measure the *whole* house, including the car circuit. |
| **A charger for each car** | Any charger that lets the car choose its own current. | Tesla Wall Connector, Tesla Mobile Connector, or any Type 2 charger. |
| **One or two Teslas** | Must support Bluetooth commands. | Model 3, Model Y; newer Model S / X. |
| **Your Tesla key card** | Needed once per car to approve the Bluetooth key. | It came with the car. |

### Network

- A normal home network (router + Wi-Fi). The computer should preferably be
  plugged in with a network cable.
- The computer and the Shelly meter must be on the **same home network**.
- Give both a **fixed address**. In your router's settings this is usually called
  "DHCP reservation" or "static lease", so their addresses never change.
- Internet access for the computer (spot prices, car data, updates).
- **No port forwarding** and nothing opened to the internet is needed. Don't
  open any.
- The cars should have Wi-Fi or mobile coverage at home (for car data).

### Accounts and information

- Your **Tesla account** (to let TeslaMate read the cars' data).
- Each car's **VIN** (Tesla app → car → Software, or bottom of the windscreen).
- Your **main fuse size** (e.g. 3 × 25 A) and the **breaker size of the car
  circuit** (e.g. 16 A). Read them from your fuse box or ask your electrician.
- Ideally a **spot-price electricity contract**. Otherwise the cheapest-hours
  scheduling saves you nothing (the fuse protection still works).
- Spot prices are supported for Finland, the Baltics, Sweden, Norway and
  Denmark (from [spot-hinta.fi](https://spot-hinta.fi/)).

---

## Install it with an AI assistant

The easiest way: let **Claude Code**, an AI assistant that runs on the computer
itself, do the installation with you. It reads this repository's
[AGENTS.md](AGENTS.md) (a step-by-step playbook written for it), asks you
questions in plain language, runs the commands, and tells you when *you* need
to do something physical (like tapping your key card in the car).

1. **Get a Claude subscription** (Pro or higher) at
   [claude.ai](https://claude.ai).
2. **Prepare the computer.** Install the operating system (for a Raspberry Pi,
   use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and pick
   *Raspberry Pi OS (64-bit)*). Connect it to your network.
3. **Open a terminal on that computer.** Either directly with a keyboard and
   screen (*Terminal* app), or from your laptop with `ssh yourname@<computer's address>`.
4. **Install Claude Code** by pasting this and pressing Enter:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```

   (Instructions for other systems: [Claude Code docs](https://docs.claude.com/en/docs/claude-code/setup).)
5. **Download this project and start the assistant:**

   ```bash
   sudo apt-get install -y git
   git clone https://github.com/mkorkalo/ha-tesla-loadbalance.git
   cd ha-tesla-loadbalance
   claude
   ```

   The first time, it asks you to log in with your Claude account.
6. **Tell it what you want**, for example:

   > Set up this system for me. I'm not technical, so explain each step
   > simply. Follow AGENTS.md.

The assistant will ask before running anything important. Expect the whole
setup to take an afternoon, mostly waiting for downloads and for the cars to
wake up.

## Install it yourself

Follow [INSTALL.md](INSTALL.md): the same steps, written out for a person.

---

## Daily use

After installation it runs by itself. In Home Assistant you get:

- **Charge limit** per car (synced with the Tesla app both ways).
- **Desired charge amps** per car, if you want a car to charge slower.
- **Tonight's plan** per car (start time and estimated cost) and a
  **Reschedule** button.
- **EV charge allocation**: what the system is doing right now, e.g.
  `model3 8A · modely 8A`.
- A **notification** if the controller ever stops running.

To charge immediately instead of waiting for cheap hours, start charging from
the Tesla app as usual. The system will still keep the current within your
fuses.

## More

- [INSTALL.md](INSTALL.md): manual installation and troubleshooting
- [AGENTS.md](AGENTS.md): instructions for AI assistants
- [docs/DESIGN.md](docs/DESIGN.md): how the control logic works (technical)

## License

[MIT](LICENSE). Use at your own risk. See the safety note above.
