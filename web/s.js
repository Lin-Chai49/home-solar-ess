const $ = (id) => document.getElementById(id);
const modes = { auto: "Auto", self: "Self-use", tou: "Peak / off-peak", manual: "Manual", stop: "Stop" };
let last = null;

function n(v, d) {
  if (v == null || Number.isNaN(v)) return "--";
  return Number(v).toFixed(d);
}

function say(s) {
  const pv = s.pv, load = s.load, batt = s.batt, grid = s.grid;
  if (s.mode === "stop") return "Stopped.";
  if (s.mode === "auto" && s.reason) return s.reason;
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
  $("manwrap").hidden = s.mode !== "manual";
  $("manual").disabled = s.mode !== "manual";
  const pcs = (s.cfg && s.cfg.pcs_kw) || 5;
  $("manual").min = String(-pcs);
  $("manual").max = String(pcs);
  $("manout").textContent = n(s.manual, 1) + " kW";
  const box = $("alarms");
  box.innerHTML = "";
  if (s.alarms && s.alarms.length) {
    s.alarms.forEach((m) => {
      const li = document.createElement("li");
      li.textContent = m;
      box.appendChild(li);
    });
    box.hidden = false;
  } else box.hidden = true;
  const bal = $("bal");
  if (Math.abs(s.bal) > 0.1) {
    bal.hidden = false;
    bal.textContent = "Power does not add up this tick, off by " + n(s.bal, 3) + " kW.";
  } else bal.hidden = true;
  draw(s.hist);
  cells(s.cells, s.cfg && s.cfg.balance, s.batt, s.bal_now);
  drawPlan(s.plan, s.reason);
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

function drawPlan(plan, why) {
  $("plan-why").textContent = why || "Planned from the next 12 hours of solar, load, and rates.";
  const box = $("planbars");
  if (!plan || !plan.length) { box.innerHTML = ""; return; }
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

function draw(hist) {
  const c = $("chart");
  const x = c.getContext("2d");
  const w = c.width, h = c.height;
  x.clearRect(0, 0, w, h);
  x.strokeStyle = "#c5ccc0";
  x.beginPath();
  x.moveTo(28, 8);
  x.lineTo(28, h - 18);
  x.lineTo(w - 8, h - 18);
  x.stroke();
  if (!hist || hist.length < 2) return;
  let mx = 0.5;
  hist.forEach((p) => {
    mx = Math.max(mx, p[1], p[2], Math.abs(p[3]));
  });
  const x0 = 28, y0 = h - 18, ww = w - 36, hh = h - 26;
  function X(i) { return x0 + (i / (hist.length - 1)) * ww; }
  function Y(v) { return y0 - (v / mx) * hh; }
  function line(idx, color) {
    x.strokeStyle = color;
    x.beginPath();
    hist.forEach((p, i) => {
      const y = Y(idx === 3 ? Math.abs(p[3]) : p[idx]);
      if (i === 0) x.moveTo(X(i), y);
      else x.lineTo(X(i), y);
    });
    x.stroke();
  }
  line(1, "#8a6a12");
  line(3, "#2d6a4f");
  line(2, "#2c3230");
  x.fillStyle = "#5b675f";
  x.font = "11px sans-serif";
  x.fillText(mx.toFixed(1) + " kW", 2, 14);
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

document.querySelectorAll("[data-mode]").forEach((b) => {
  b.onclick = () => post({ mode: b.dataset.mode }).then(fill);
});
$("pause").onclick = () => post({ paused: !(last && last.paused) }).then(fill);
$("speed").onchange = () => post({ speed: Number($("speed").value) }).then(fill);
$("newday").onclick = () => post({ newday: true }).then(fill);
$("balance").onchange = () => post({ cfg: { balance: $("balance").value } }).then(fill);
$("manual").oninput = () => {
  $("manout").textContent = n(Number($("manual").value), 1) + " kW";
};
$("manual").onchange = () => post({ manual: Number($("manual").value) }).then(fill);

const form = $("form");
function cfgToForm(cfg) {
  [...form.elements].forEach((el) => {
    if (!el.name) return;
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
    if (el.name === "soc") { soc = Number(el.value); return; }
    if (el.type === "checkbox") cfg[el.name] = el.checked;
    else if (el.type === "number") cfg[el.name] = Number(el.value);
    else cfg[el.name] = el.value;
  });
  post({ cfg, soc }).then(fill);
};

async function tick() {
  try {
    const s = await get();
    if (!$("speed").matches(":focus")) $("speed").value = String(s.speed);
    if (!$("manual").matches(":active")) {
      const cur = Number($("manual").value);
      if (Math.abs(cur - s.manual) > 0.05) $("manual").value = s.manual;
    }
    if (!form.dataset.ready) {
      cfgToForm(s.cfg);
      if (form.soc) form.soc.value = s.soc;
      form.dataset.ready = "1";
    }
    fill(s);
  } catch (err) {
    $("say").textContent = "No data. Check that the program is running.";
  }
}
tick();
setInterval(tick, 700);
