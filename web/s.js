const $ = (id) => document.getElementById(id);
const modes = { auto: "Auto", self: "Self-use", tou: "Peak / off-peak", manual: "Manual", stop: "Stop" };
let last = null;

function n(v, d) {
  if (v == null || Number.isNaN(v)) return "--";
  return Number(v).toFixed(d);
}

function limited(reason) {
  if (!reason) return false;
  const r = reason.toLowerCase();
  return r.includes("stopped") || r.includes("derated") || r.includes("curtailed")
    || r.includes("too hot") || r.includes("is full") || r.includes("is empty")
    || r.includes("is high") || r.includes("is low") || r.includes("minimum charge")
    || r.includes("maximum charge") || r.includes("battery full");
}

function powerWords(v) {
  const x = Number(v);
  if (Number.isNaN(x)) return "--";
  if (x > 0.05) return "discharge " + n(x, 1) + " kW";
  if (x < -0.05) return "charge " + n(-x, 1) + " kW";
  return "0 kW";
}

function say(s) {
  const pv = s.pv, load = s.load, batt = s.batt, grid = s.grid;
  if (s.mode === "stop") return "Stopped.";
  if (s.reason && (s.mode === "auto" || limited(s.reason))) return s.reason;
  if (batt < -0.08 && grid > 0.08) return `Charging the battery from the grid at ${n(-batt,1)} kW. The house is also on the grid.`;
  if (batt < -0.08) return `Solar ${n(pv,1)} kW, home ${n(load,1)} kW. Surplus is charging the battery at ${n(-batt,1)} kW.`;
  if (batt > 0.08) {
    if (pv > 0.08) return `Solar ${n(pv,1)} kW. The battery is covering the rest at ${n(batt,1)} kW.`;
    return `Mostly on battery, discharging at ${n(batt,1)} kW.`;
  }
  if (grid > 0.08) return `Battery idle. Importing ${n(grid,1)} kW from the grid.`;
  if (grid < -0.08) return `Exporting ${n(-grid,1)} kW to the grid.`;
  return "Solar and load are about even. Little grid flow.";
}

function fill(s) {
  last = s;
  $("clock").textContent = s.ts;
  $("band").textContent = s.band + "  " + s.price.toFixed(2) + " /kWh";
  $("say").textContent = say(s);
  $("pv").textContent = n(s.pv, 2);
  $("load").textContent = n(s.load, 2);
  if (s.batt > 0.05) {
    $("batt-lab").textContent = "Battery discharging";
    $("batt").textContent = n(s.batt, 2);
    $("batt-u").textContent = "kW";
  } else if (s.batt < -0.05) {
    $("batt-lab").textContent = "Battery charging";
    $("batt").textContent = n(-s.batt, 2);
    $("batt-u").textContent = "kW";
  } else {
    $("batt-lab").textContent = "Battery";
    $("batt").textContent = "0.00";
    $("batt-u").textContent = "idle";
  }
  if (s.grid > 0.05) {
    $("grid-lab").textContent = "Importing";
    $("grid").textContent = n(s.grid, 2);
    $("grid-u").textContent = "kW";
  } else if (s.grid < -0.05) {
    $("grid-lab").textContent = "Exporting";
    $("grid").textContent = n(-s.grid, 2);
    $("grid-u").textContent = "kW";
  } else {
    $("grid-lab").textContent = "Grid";
    $("grid").textContent = "0.00";
    $("grid-u").textContent = "no flow";
  }
  $("soc").textContent = n(s.soc, 1);
  $("socbar").style.width = Math.max(0, Math.min(100, s.soc)) + "%";
  $("temp").textContent = "Battery " + n(s.temp, 1) + " °C";
  const t = s.today;
  $("t-pv").textContent = n(t.pv, 2) + " kWh";
  $("t-load").textContent = n(t.load, 2) + " kWh";
  $("t-chg").textContent = n(t.chg, 2) + " kWh";
  $("t-dis").textContent = n(t.dis, 2) + " kWh";
  $("t-buy").textContent = n(t.buy, 2) + " kWh";
  $("t-sell").textContent = n(t.sell, 2) + " kWh";
  $("t-cost").textContent = n(t.cost, 2);
  $("t-cost0").textContent = n(t.cost0, 2);
  const d = t.cost0 - t.cost;
  $("save").textContent = d >= 0
    ? "At your rates, the battery is about " + n(d, 2) + " less today. Export credit is not included."
    : "At your rates, this is about " + n(-d, 2) + " more than without a battery. Off-peak charging pays off when you discharge at peak.";
  document.querySelectorAll("[data-mode]").forEach((b) => {
    const on = b.dataset.mode === s.mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  $("pause").textContent = s.paused ? "Resume" : "Pause";
  $("pause").classList.toggle("on", s.paused);
  $("pause").setAttribute("aria-pressed", s.paused ? "true" : "false");
  $("manwrap").hidden = s.mode !== "manual";
  $("manual").disabled = s.mode !== "manual";
  const pcs = (s.cfg && s.cfg.pcs_kw) || 5;
  $("manual").min = String(-pcs);
  $("manual").max = String(pcs);
  if (document.activeElement !== $("manual")) $("manual").value = String(s.manual);
  $("manout").textContent = powerWords(s.manual);
  const box = $("alarms");
  const nextAlarms = (s.alarms || []).join("\n");
  if (box.dataset.msg !== nextAlarms) {
    box.dataset.msg = nextAlarms;
    box.replaceChildren();
    (s.alarms || []).forEach((m) => {
      const li = document.createElement("li");
      li.textContent = m;
      box.appendChild(li);
    });
  }
  box.hidden = !nextAlarms;
  const bal = $("bal");
  if (Math.abs(s.bal) > 0.1) {
    bal.hidden = false;
    bal.textContent = "Power does not add up this tick, off by " + n(s.bal, 3) + " kW.";
  } else bal.hidden = true;
  draw(s.hist);
  cells(s.cells, s.cfg && s.cfg.balance, s.batt, s.bal_now);
  drawPlan(s.plan, s.reason, s.mode);
  const llm = s.llm || {};
  const llmEl = $("llm-st");
  if (llm.enabled && llm.using) {
    llmEl.hidden = false;
    llmEl.textContent = "LLM connected (" + (llm.model || "") + "). Suggestions still respect charge limits and cells.";
  } else if (llm.enabled) {
    llmEl.hidden = false;
    llmEl.textContent = "LLM is configured; this tick still uses local rules. " + (llm.err ? llm.err : "");
  } else {
    llmEl.hidden = true;
    llmEl.textContent = "";
  }
}

function drawPlan(plan, why, mode) {
  const box = $("planbars");
  const fallback = "Planned from the next 12 hours of solar, load, and rates.";
  if (!plan || !plan.length) {
    box.innerHTML = "";
    $("plan-why").textContent = (mode && mode !== "auto")
      ? "The 12-hour plan is drawn in Auto."
      : (why || fallback);
    return;
  }
  $("plan-why").textContent = why || fallback;
  let mx = 0.4;
  plan.forEach((p) => { mx = Math.max(mx, Math.abs(p.p)); });
  if (box.children.length !== plan.length) {
    box.innerHTML = "";
    plan.forEach(() => {
      const el = document.createElement("i");
      el.appendChild(document.createElement("b"));
      box.appendChild(el);
    });
  }
  plan.forEach((p, i) => {
    const el = box.children[i];
    el.style.height = Math.max(6, Math.abs(p.p) / mx * 100) + "%";
    el.className = p.p < -0.05 ? "chg" : (p.p > 0.05 ? "dis" : "");
    const act = p.p < 0 ? "charge " + n(-p.p, 1) : p.p > 0 ? "discharge " + n(p.p, 1) : "idle";
    el.title = p.t + " " + p.band + "  " + act + " kW  SOC " + p.soc + "%";
    el.firstChild.textContent = p.t.slice(0, 2);
  });
}

function cells(c, bal, batt, balNow) {
  if (!c) return;
  $("vmax").textContent = n(c.vmax, 3);
  $("vmin").textContent = n(c.vmin, 3);
  $("dv").textContent = String(c.dv_mv);
  $("nbal").textContent = String(c.nbal);
  if (c.nbal && batt < -0.3 && c.dv_mv >= 40) {
    $("cell-say").textContent = `Spread ${c.dv_mv} mV. Charge current is much larger than balance current, so the spread widens first and closes near full or idle.`;
  } else if (c.nbal) {
    $("cell-say").textContent = `Spread ${c.dv_mv} mV, balancing. Charge is limited by cell ${c.hi}, discharge by cell ${c.lo}.`;
  } else if (c.odd && c.odd.length) {
    $("cell-say").textContent = `Cell ${c.odd.join(", ")} differs from the rest. High is cell ${c.hi} at ${n(c.vmax,3)} V, low is cell ${c.lo} at ${n(c.vmin,3)} V.`;
  } else {
    $("cell-say").textContent = `16-cell LFP. High cell ${c.hi}, low cell ${c.lo}, spread ${c.dv_mv} mV. Cell 7 is weaker on purpose.`;
  }
  if (bal === "auto" && balNow) {
    if (c.nbal || c.dv_mv >= 25) {
      $("cell-say").textContent += balNow === "active" ? " Using active balance." : " Using passive balance.";
    }
  }
  const box = $("cellbars");
  const items = c.items || [];
  if (box.children.length !== items.length) {
    box.innerHTML = "";
    items.forEach((it) => {
      const el = document.createElement("i");
      const lab = document.createElement("b");
      lab.textContent = String(it.i);
      el.appendChild(lab);
      box.appendChild(el);
    });
  }
  items.forEach((it, i) => {
    const el = box.children[i];
    el.style.height = Math.max(8, Math.min(100, ((it.v - 2.5) / 1.15) * 100)) + "%";
    el.title = `Cell ${it.i}  ${it.v} V  ${it.t} °C  ${it.soc}%`;
    el.className = it.flag ? "odd" : (it.bal ? "bal" : "");
  });
  const sel = $("balance");
  if (bal && !sel.matches(":focus")) sel.value = bal;
}

function hhmm(min) {
  const m = ((min % 1440) + 1440) % 1440;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return (h < 10 ? "0" : "") + h + ":" + (mm < 10 ? "0" : "") + mm;
}

function draw(hist) {
  const c = $("chart");
  const x = c.getContext("2d");
  if (!x) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = c.getBoundingClientRect();
  const w = Math.max(rect.width, 280);
  const h = Math.max(rect.height, 140);
  const bw = Math.round(w * dpr);
  const bh = Math.round(h * dpr);
  if (c.width !== bw || c.height !== bh) {
    c.width = bw;
    c.height = bh;
  }
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, w, h);
  const x0 = 36;
  const y0 = h - 22;
  const ww = w - 44;
  const hh = h - 32;
  x.strokeStyle = "#c5ccc0";
  x.lineWidth = 1;
  x.beginPath();
  x.moveTo(x0, 8);
  x.lineTo(x0, y0);
  x.lineTo(w - 8, y0);
  x.stroke();
  if (!hist || hist.length < 2) return;
  let hi = 0.5;
  let lo = 0;
  hist.forEach((p) => {
    hi = Math.max(hi, p[1], p[2], p[3]);
    lo = Math.min(lo, p[3]);
  });
  const span = (hi - lo) || 1;
  function X(i) { return x0 + (i / (hist.length - 1)) * ww; }
  function Y(v) { return y0 - ((v - lo) / span) * hh; }
  if (lo < -0.05) {
    x.beginPath();
    x.moveTo(x0, Y(0));
    x.lineTo(w - 8, Y(0));
    x.stroke();
  }
  function line(idx, color) {
    x.strokeStyle = color;
    x.lineWidth = 1.5;
    x.beginPath();
    hist.forEach((p, i) => {
      const y = Y(p[idx]);
      if (i === 0) x.moveTo(X(i), y);
      else x.lineTo(X(i), y);
    });
    x.stroke();
  }
  line(1, "#8a6a12");
  line(2, "#2c3230");
  line(3, "#2d6a4f");
  x.fillStyle = "#5b675f";
  x.font = "11px sans-serif";
  x.fillText(hi.toFixed(1), 2, 14);
  if (lo < -0.05) x.fillText(lo.toFixed(1), 2, y0);
  x.fillText(hhmm(hist[0][0]), x0, h - 6);
  x.fillText(hhmm(hist[hist.length - 1][0]), Math.max(x0 + 48, w - 44), h - 6);
}

async function get() {
  const r = await fetch("/api/state");
  if (!r.ok) throw new Error("state");
  return r.json();
}

async function post(body) {
  const r = await fetch("/api/ctrl", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error("ctrl");
  return r.json();
}

function send(body) {
  return post(body).then(fill).catch(() => {
    $("say").textContent = "No data. Check that the program is running.";
  });
}

document.querySelectorAll("[data-mode]").forEach((b) => {
  b.onclick = () => send({ mode: b.dataset.mode });
});
$("pause").onclick = () => send({ paused: !(last && last.paused) });
$("speed").onchange = () => send({ speed: Number($("speed").value) });
$("newday").onclick = () => send({ newday: true });
$("balance").onchange = () => send({ cfg: { balance: $("balance").value } });
$("manual").oninput = () => {
  $("manout").textContent = powerWords($("manual").value);
};
$("manual").onchange = () => send({ manual: Number($("manual").value) });

const form = $("form");
const socInput = form.querySelector('input[name="soc"]');
let socTouched = false;
if (socInput) socInput.addEventListener("input", () => { socTouched = true; });

function cfgToForm(cfg) {
  [...form.elements].forEach((el) => {
    if (!el.name || el.name === "soc") return;
    if (el.type === "checkbox") el.checked = !!cfg[el.name];
    else if (cfg[el.name] != null) el.value = cfg[el.name];
  });
}
form.onsubmit = (e) => {
  e.preventDefault();
  const cfg = {};
  let soc = null;
  [...form.elements].forEach((el) => {
    if (!el.name) return;
    if (el.name === "soc") {
      if (socTouched) soc = Number(el.value);
      return;
    }
    if (el.type === "checkbox") cfg[el.name] = el.checked;
    else if (el.type === "number") cfg[el.name] = Number(el.value);
    else cfg[el.name] = el.value;
  });
  const body = { cfg };
  if (soc != null && !Number.isNaN(soc)) body.soc = soc;
  post(body).then((s) => {
    cfgToForm(s.cfg);
    socTouched = false;
    if (socInput) socInput.value = s.soc;
    fill(s);
  }).catch(() => {
    $("say").textContent = "Settings were not saved. Check that the program is running.";
  });
};

async function tick() {
  try {
    const s = await get();
    if (!$("speed").matches(":focus")) {
      const speed = String(s.speed);
      if ([...$("speed").options].some((o) => o.value === speed)) $("speed").value = speed;
    }
    if (!$("manual").matches(":active")) {
      const cur = Number($("manual").value);
      if (Math.abs(cur - s.manual) > 0.05) $("manual").value = s.manual;
    }
    if (!form.dataset.ready) {
      cfgToForm(s.cfg);
      form.dataset.ready = "1";
    }
    if (socInput && document.activeElement !== socInput && !socTouched) {
      socInput.value = s.soc;
    }
    fill(s);
  } catch (err) {
    $("say").textContent = "No data. Check that the program is running.";
  }
}
let pollMs = 700;
let poll = setInterval(tick, pollMs);
function setPoll(ms) {
  if (ms === pollMs) return;
  pollMs = ms;
  clearInterval(poll);
  poll = setInterval(tick, pollMs);
}
document.addEventListener("visibilitychange", () => {
  setPoll(document.hidden ? 4000 : 700);
  if (!document.hidden) tick();
});
tick();
