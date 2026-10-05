# Home solar storage

A local simulator for a **household rooftop solar array plus a battery**. It is a single Python process and a one-page web UI. There is no database server, no Node build, and no cloud account.

It is meant to show, on a laptop, how a small home system would:

- use solar first
- charge and discharge against time-of-use rates
- stop at the weakest cell in a 16-cell LFP string
- optionally take a large-language-model suggestion without letting that suggestion skip safety limits

The numbers on the screen are **simulated**. No inverter, meter, or BMS is attached.

---

## What you see when it runs

Open the page and you get, in this order:

1. A one-line status (“Peak rate. Use the battery to cover the house.”)
2. Alerts, if any (cell spread, min/max charge, heat)
3. Four live powers: solar, home load, battery, grid
4. Remaining battery % and temperature
5. How it runs: Auto, Self-use, Peak/off-peak, Manual, Stop
6. Today’s energy and an estimated bill at the rates you typed
7. A 12-hour plan (green = charge, dark = discharge)
8. A power chart for the simulated day. Battery is drawn above zero when it is discharging and below zero when it is charging.
9. Sixteen cell bars
10. System size and rates, folded at the bottom

The UI is English. Battery and grid are labeled in words (charging / discharging, importing / exporting), not signed kilowatts. Internally the model still uses a signed convention; see below.

---

## Requirements

- Python 3.9 or newer
- A browser
- Nothing else. No `pip install`.

---

## Run

```bash
python3 server.py
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The server binds to localhost only.

Sanity check for the physics and a few dispatch cases:

```bash
python3 server.py check
```

That command should print a line starting with `ok` and exit 0. It checks power balance, the SOC window, cell cutoff, off-peak charging, export curtailment, hour ranges, and the local HTTP paths. The same check runs on Python 3.9, 3.12, and 3.14 in GitHub Actions.

---

## Default house (change this on the page)

| Item | Default | Notes |
|---|---|---|
| Solar | 6 kW | Clear-sky sine between about 05:40 and 19:00, times a moving cloud factor 0.45–1.0 |
| Battery | 10 kWh | 16 series LFP cells, nominal 3.2 V each (~51 V pack) |
| Inverter | 5 kW | Charge and discharge are clipped to this |
| Round-trip path | η = 0.96 | Charge stores `P × η`; discharge draws `P / η` from the pack. Editable as Efficiency. |
| SOC window | 15%–95% | Hard stop at the window, even in Auto. One step cannot run past it. |
| Export | on | If you uncheck it, the battery will not discharge into the grid. Leftover solar is curtailed. A discharge command is not turned into a charge. |
| Peak hours | `8-11,18-21` | 08:00–11:00 and 18:00–21:00, **not including** the end hour (21:00 is Mid) |
| Off-peak hours | `23-7` | 23:00–07:00, wrapping midnight |
| Peak / mid / off-peak rates | 0.72 / 0.52 / 0.28 per kWh | Whatever currency you use; the page does not convert FX |

Load is a fixed daily shape for a small household (low at night, bumps at breakfast and evening), plus a little noise. It is not a measured load.

---

## Power sign (internal vs screen)

Inside the model:

| Quantity | Positive | Negative |
|---|---|---|
| Battery power `batt` | discharging to the house | charging |
| Grid power `grid` | import (buying) | export (selling) |

Every tick:

```
solar + grid + battery = home load
```

If that residual is more than 0.1 kW, the page shows it. The four tiles on the page **do not** show the minus sign; they say “Battery charging 2.1 kW” instead of “−2.1”.

---

## Run modes

| Mode | What it does |
|---|---|
| **Auto** (default) | Looks ~24 hours ahead at solar, load, and rates, then picks this tick’s charge/discharge. Same rules fill the 12-hour plan. |
| **Self-use** | Drive battery power toward `load − solar` so the grid stays near zero. |
| **Peak / off-peak** | Charge from the grid in off-peak hours until the max SOC; cover the house from the battery in peak hours. |
| **Manual** | A slider sets battery kW (range follows inverter kW). |
| **Stop** | Battery kW = 0. The house takes solar and grid only. The 12-hour plan is cleared. |

Auto is not a neural net. It is a small lookahead:

- Surplus solar is stored first.
- At **peak**, the battery covers the house if it has energy.
- At **off-peak**, it buys from the grid **only if** the coming evening peak (about 17:30–22:00) would still be short after leftover daytime solar. If daytime sun can fill the pack, it does **not** buy cheap grid power at 1 a.m.
- At **mid** rate, it may use the battery for the house but keeps a reserve for evening peak.

If an LLM is configured, Auto may use the model’s `p_kw` for this tick. The SOC window, inverter limit, and cell limits still apply after that.

Demo speed:

| Setting | Meaning |
|---|---|
| Slow | About 1 simulated minute per real second |
| Medium | Faster; a day is watchable |
| Fast | A day compresses to a few minutes |

**Restart today** sets the clock to 00:00 of the simulated day and zeros today’s energy counters. It does **not** wipe cell imbalance.

Saving system size or rates does not rewind the battery. The “Charge now” box is sent only after you edit it.

---

## 16-cell pack

The pack is **16 cells in series**, lithium iron phosphate, household 48 V class.

- Open-circuit voltage follows a typical LFP curve: steep at the bottom, **almost flat from ~20–80%**, then a knee near full. Voltage-based balancing therefore does little in the mid band and shows up near the top.
- Pack current is the same through every series cell. A weaker cell’s SOC moves faster.
- **Cell 7** is given lower capacity (~0.94×) and higher resistance on purpose, so the UI has something to show. Cell 3 starts a little higher.
- Terminal voltage includes IR drop: during charge a high-resistance cell looks **higher** in volts even if its SOC is behind. That drop uses the DC pack current, after the one-way efficiency.

**Limits (per cell)**

| | Voltage | Action |
|---|---|---|
| Hard high | 3.55 V | Charge to 0 |
| Soft high | 3.48 V | Charge × 0.35 |
| Soft low | 2.95 V | Discharge × 0.35 |
| Hard low | 2.70 V | Discharge to 0 |
| Heat | ≥ 45 °C derate, ≥ 52 °C stop | Hottest cell |

Charge is limited by the **highest** cell; discharge by the **lowest**. That is the usual BMS rule: the pack is only as strong as the worst cell.

**Balancing**

| Mode | Behaviour |
|---|---|
| Auto | Passive while spread is modest; active if spread ≥ 50 mV |
| Passive | ~80 mA bleed on high cells, mainly while charging or idle. Too small to fight kilowatts of charge current — spread often **grows** during fast charge and closes near full or rest. That is realistic for a ~200 Ah cell. |
| Active | ~0.8 A from the highest cell to the lowest |
| Off | No balancing |

Threshold to start balancing: about **25 mV**. After balancing, SOC is clamped to 0–100%.

There is **no** thermal-runaway probability, no gas sensor, and no made-up “82% risk” meter.

---

## Bill estimate

Today’s “Est. bill” is:

```
sum(grid import in each tick × rate in force at that hour)
```

“Without battery” uses `max(load − solar, 0)` at the same rates. Solar there is the resource before curtailment, so turning export off does not make the no-battery bill look worse. Export **credit is not added**. If the battery charged a lot at off-peak and has not yet discharged at peak, the estimate can look **worse** than no battery for that day. The page says so.

Rates are whatever you type. Defaults look like a residential TOU tariff; they are not a live utility feed.

Hour strings: `8-11,18-21` means from hour 8 up to **but not including** hour 11, and 18 up to 21. So 21:00 is Mid, not Peak. Overnight `23-7` wraps midnight.

---

## HTTP

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | UI |
| GET | `/api/state` | Full snapshot (powers, SOC, cells, plan, alarms, config) |
| POST | `/api/ctrl` | `{mode, speed, paused, manual, newday, soc, cfg}` |
| GET | `/api/llm` | Whether an LLM is configured |
| POST | `/api/llm` | Ask the model once |

POST bodies larger than 64 KB are rejected, as is a Content-Length that is not a number. Static files cannot leave the `web/` folder. The process listens on `127.0.0.1` only. If that port is already taken, the process says so and exits.

---

## LLM hook (optional)

The scheduler works with **no key**. If you want a model in the loop:

1. Copy `llm.example.json` to `data/llm.json`
2. Set `"enabled": true` and fill `api_key` / `model`
3. Or export `ESS_LLM_KEY`, `ESS_LLM_URL`, `ESS_LLM_MODEL`

The client speaks OpenAI `POST {base_url}/chat/completions`. DeepSeek, Qwen, and local vLLM work if they expose that shape.

The model must return **only** JSON:

```json
{"p_kw": -1.2, "why": "Off-peak and evening peak still needs energy", "balance": "auto"}
```

| Field | Meaning |
|---|---|
| `p_kw` | Battery kW, discharge positive, charge negative, within inverter kW |
| `why` | Short reason, shown on the page |
| `balance` | `auto` / `passive` / `active` / `off` |

If a call fails, the last good suggestion is kept. The model **cannot** close contactors, raise the SOC window, or override cell voltage limits. No request is sent when no key is set. `data/` is gitignored so keys stay on the machine.

---

## Layout of the repo

```
server.py          Simulation, dispatch, 16-cell pack, HTTP
llm.py             Optional OpenAI-compatible client
llm.example.json   Template for data/llm.json
web/index.html     Page
web/s.css          Styles
web/s.js           Poll /api/state about every 0.7 s, slower while the tab is hidden
notes.txt          Short run notes
.github/workflows/check.yml   Python 3.9 / 3.12 / 3.14 sanity check
LICENSE            MIT
```

`data/cfg.json` and `data/llm.json` are created locally and are not committed.

---

## What this is not

- Not a replacement for a certified BMS or inverter
- Not wired to Modbus, CAN, or a real meter
- Not a trained thermal-runaway or SOH neural net
- Not a multi-site or commercial peak-demand (kVA) product

Those can be added later by swapping the simulated telemetry for a gateway; the page and the safety order (suggest → SOC window → cell limits → hardware) are meant to stay.

---

## License

MIT. See `LICENSE`.
