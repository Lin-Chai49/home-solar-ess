const $ = (id) => document.getElementById(id);
const modes = { auto: "自动", self: "自发自用", tou: "谷充峰放", manual: "手动", stop: "停机" };
let last = null;

function n(v, d) {
  if (v == null || Number.isNaN(v)) return "--";
  return Number(v).toFixed(d);
}

function say(s) {
  const pv = s.pv, load = s.load, batt = s.batt, grid = s.grid;
  if (s.mode === "auto" && s.reason) return s.reason + "。";
  if (s.mode === "stop") return "已经停机。";
  if (batt < -0.08 && grid > 0.08) return `正在从电网给电池充电 ${n(-batt,1)} kW，家里用电也走市电。`;
  if (batt < -0.08) return `光伏 ${n(pv,1)} kW，家里用 ${n(load,1)} kW，余电在给电池充 ${n(-batt,1)} kW。`;
  if (batt > 0.08) {
    if (pv > 0.08) return `光伏 ${n(pv,1)} kW，不够的部分电池在补 ${n(batt,1)} kW。`;
    return `晚上主要靠电池，正在放电 ${n(batt,1)} kW。`;
  }
  if (grid > 0.08) return `电池没出电，正在从电网买 ${n(grid,1)} kW。`;
  if (grid < -0.08) return `余电卖到电网 ${n(-grid,1)} kW。`;
  return "发电和用电差不多，电网几乎不走电。";
}

function fill(s) {
  last = s;
  $("clock").textContent = s.ts;
  $("band").textContent = s.band + "  " + s.price.toFixed(2) + " 元/kWh";
  $("say").textContent = say(s);
  $("pv").textContent = n(s.pv, 2);
  $("load").textContent = n(s.load, 2);
  $("batt").textContent = n(s.batt, 2);
  $("grid").textContent = n(s.grid, 2);
  $("soc").textContent = n(s.soc, 1);
  $("socbar").style.width = Math.max(0, Math.min(100, s.soc)) + "%";
  $("temp").textContent = "电池 " + n(s.temp, 1) + " ℃";
  $("reason").textContent = s.reason || "";
  const t = s.today;
  $("t-pv").textContent = n(t.pv, 2) + " kWh";
  $("t-load").textContent = n(t.load, 2) + " kWh";
  $("t-chg").textContent = n(t.chg, 2) + " kWh";
  $("t-dis").textContent = n(t.dis, 2) + " kWh";
  $("t-buy").textContent = n(t.buy, 2) + " kWh";
  $("t-sell").textContent = n(t.sell, 2) + " kWh";
  $("t-cost").textContent = n(t.cost, 2) + " 元";
  $("t-cost0").textContent = n(t.cost0, 2) + " 元";
  const d = t.cost0 - t.cost;
  $("save").textContent = d >= 0
    ? "按你填的电价估，今天电池大约少花 " + n(d, 2) + " 元。卖电没算进收益。"
    : "按你填的电价估，今天比不用电池多花 " + n(-d, 2) + " 元。夜里充进电池的电，要等到峰时用出来才划算。";
  document.querySelectorAll("[data-mode]").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === s.mode);
  });
  $("pause").textContent = s.paused ? "继续" : "暂停";
  $("pause").classList.toggle("on", s.paused);
  $("manual").disabled = s.mode !== "manual";
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
    bal.textContent = "这一拍功率对不上，差 " + n(s.bal, 3) + " kW。";
  } else bal.hidden = true;
  draw(s.hist);
  cells(s.cells, s.cfg && s.cfg.balance, s.batt, s.bal_now);
  drawPlan(s.plan, s.reason);
  const llm = s.llm || {};
  if (llm.enabled && llm.using) $("llm-st").textContent = "大模型：" + (llm.model || "") + " 正在出建议，功率仍受电芯和电量上下限限制。";
  else if (llm.enabled) $("llm-st").textContent = "大模型已配置，但这一拍还在用本地规则。" + (llm.err ? " " + llm.err : "");
  else $("llm-st").textContent = "大模型未接入。复制 llm.example.json 为 data/llm.json，填入密钥即可。";
}

function drawPlan(plan, why) {
  $("plan-why").textContent = why || "按后面十二小时的光伏、用电和电价来排。";
  const box = $("planbars");
  box.innerHTML = "";
  if (!plan || !plan.length) return;
  let mx = 0.4;
  plan.forEach((p) => { mx = Math.max(mx, Math.abs(p.p)); });
  plan.forEach((p) => {
    const el = document.createElement("i");
    el.style.height = Math.max(6, Math.abs(p.p) / mx * 100) + "%";
    el.className = p.p < -0.05 ? "chg" : (p.p > 0.05 ? "dis" : "");
    el.title = p.t + " " + p.band + " 电池 " + p.p + " kW  电量 " + p.soc + "%";
    const lab = document.createElement("b");
    lab.textContent = p.t.slice(0, 2);
    el.appendChild(lab);
    box.appendChild(el);
  });
}

function cells(c, bal, batt, balNow) {
  if (!c) return;
  $("vmax").textContent = n(c.vmax, 3);
  $("vmin").textContent = n(c.vmin, 3);
  $("dv").textContent = String(c.dv_mv);
  $("nbal").textContent = String(c.nbal);
  if (c.nbal && batt < -0.3 && c.dv_mv >= 40) {
    $("cell-say").textContent = `压差 ${c.dv_mv} mV。充电电流比均衡电流大得多，压差会先拉开，要到快满或停下时才慢慢收回来。`;
  } else if (c.nbal) {
    $("cell-say").textContent = `压差 ${c.dv_mv} mV，正在均衡。充电看最高第${c.hi}节，放电看最低第${c.lo}节。`;
  } else if (c.odd && c.odd.length) {
    $("cell-say").textContent = `第${c.odd.join("、")}节和其余差得比较多。最高第${c.hi}节 ${n(c.vmax,3)} V，最低第${c.lo}节 ${n(c.vmin,3)} V。`;
  } else {
    $("cell-say").textContent = `16 串磷酸铁锂。最高第${c.hi}节，最低第${c.lo}节，压差 ${c.dv_mv} mV。第 7 节容量低一点，是故意设的。`;
  }
  if (bal === "auto" && balNow) {
    $("cell-say").textContent += balNow === "active" ? " 当前自动用主动均衡。" : " 当前自动用被动均衡。";
  }
  const box = $("cellbars");
  box.innerHTML = "";
  (c.items || []).forEach((it) => {
    const el = document.createElement("i");
    const h = Math.max(8, Math.min(100, ((it.v - 2.5) / 1.15) * 100));
    el.style.height = h + "%";
    el.title = `第${it.i}节  ${it.v} V  ${it.t} ℃  ${it.soc}%`;
    if (it.flag) el.className = "odd";
    else if (it.bal) el.className = "bal";
    const lab = document.createElement("b");
    lab.textContent = String(it.i);
    el.appendChild(lab);
    box.appendChild(el);
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
    $("say").textContent = "读不到数据，看一下是不是程序没开。";
  }
}
tick();
setInterval(tick, 700);
