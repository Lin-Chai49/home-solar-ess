#!/usr/bin/env python3
"""本地跑的家用光伏储能监视。默认是模拟数据，没有接真逆变器。"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import llm as llmapi

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
CFG_FILE = DATA / "cfg.json"
PORT = 8765

DEFAULTS = {
    "pv_kw": 6.0,
    "batt_kwh": 10.0,
    "pcs_kw": 5.0,
    "soc_min": 15.0,
    "soc_max": 95.0,
    "eta": 0.96,
    "export": True,
    "price_peak": 0.72,
    "price_flat": 0.52,
    "price_valley": 0.28,
    "peak": "8-11,18-21",
    "valley": "23-7",
    "balance": "auto",
}

# 家里一天用电，单位 kW。按三口之家、有晚饭和空调来估，不是实测。
LOAD = (
    (0, 0.32),
    (5, 0.26),
    (6.5, 1.35),
    (8.2, 0.72),
    (11.5, 1.15),
    (13.8, 0.62),
    (17.4, 1.7),
    (19.3, 2.35),
    (21.6, 1.25),
    (23.2, 0.55),
    (24, 0.32),
)


def lerp_table(table, hour):
    return lerp_xy(table, hour % 24)


def lerp_xy(table, x):
    if x <= table[0][0]:
        return table[0][1]
    if x >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        t0, v0 = table[i]
        t1, v1 = table[i + 1]
        if t0 <= x <= t1:
            k = 0 if t1 == t0 else (x - t0) / (t1 - t0)
            return v0 + (v1 - v0) * k
    return table[-1][1]


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


# 16 串户用 48V 磷酸铁锂。开路电压按常见放电曲线估，中间很平。
N_CELL = 16
LFP_OCV = (
    (0, 2.50),
    (2, 2.90),
    (5, 3.10),
    (10, 3.20),
    (20, 3.24),
    (40, 3.28),
    (60, 3.30),
    (80, 3.33),
    (90, 3.345),
    (95, 3.40),
    (98, 3.50),
    (100, 3.65),
)
V_HI = 3.55
V_HI_SOFT = 3.48
V_LO = 2.70
V_LO_SOFT = 2.95
DV_BAL = 0.025


class Pack:
    """逐芯电压/温度，整包电流被最差那节卡住。均衡按户用常见做法。"""

    def __init__(self, batt_kwh, soc):
        self.cells = []
        rng = random.Random(7)
        for i in range(N_CELL):
            cap = 1.0 * rng.uniform(0.988, 1.012)
            r = 0.0020 * rng.uniform(0.92, 1.08)
            s = soc + rng.uniform(-0.6, 0.6)
            if i == 6:
                cap = 0.94
                r = 0.0028
                s = soc - 4.0
            if i == 2:
                s = soc + 2.2
            self.cells.append({
                "soc": max(5.0, min(98.0, s)),
                "cap": cap,
                "r": r,
                "t": 26.8 + rng.uniform(-0.3, 0.3),
                "v": 3.3,
                "bal": False,
                "flag": "",
            })
        self.cap_ah = max(batt_kwh, 0.1) * 1000.0 / N_CELL / 3.2
        self.refresh(0.0)

    def set_soc(self, soc):
        base = max(5.0, min(98.0, soc))
        for i, c in enumerate(self.cells):
            c["soc"] = max(5.0, min(98.0, base + (-4.0 if i == 6 else 2.2 if i == 2 else 0.0)))
        self.refresh(0.0)

    def mean_soc(self):
        return sum(c["soc"] for c in self.cells) / N_CELL

    def vmax(self):
        return max(c["v"] for c in self.cells)

    def vmin(self):
        return min(c["v"] for c in self.cells)

    def tmax(self):
        return max(c["t"] for c in self.cells)

    def refresh(self, i_pack):
        for c in self.cells:
            c["v"] = lfp_v(c["soc"]) - i_pack * c["r"]

    def constrain(self, p_kw):
        vpack = max(sum(lfp_v(c["soc"]) for c in self.cells), 40.0)
        self.refresh((p_kw * 1000.0) / vpack)
        hi = max(self.cells, key=lambda c: c["v"])
        lo = min(self.cells, key=lambda c: c["v"])
        ih = self.cells.index(hi) + 1
        il = self.cells.index(lo) + 1
        if p_kw < -1e-6 and hi["v"] >= V_HI:
            return 0.0, f"第{ih}节到顶（{hi['v']:.3f} V），停止充电"
        if p_kw > 1e-6 and lo["v"] <= V_LO:
            return 0.0, f"第{il}节到底（{lo['v']:.3f} V），停止放电"
        why = ""
        if p_kw < -1e-6 and hi["v"] >= V_HI_SOFT:
            p_kw *= 0.35
            why = f"第{ih}节偏高，充电降额"
        elif p_kw > 1e-6 and lo["v"] <= V_LO_SOFT:
            p_kw *= 0.35
            why = f"第{il}节偏低，放电降额"
        ht = self.tmax()
        if ht >= 52:
            return 0.0, "有电芯过热，已停止充放"
        if ht >= 45 and p_kw != 0:
            p_kw *= 0.5
            why = (why + "，" if why else "") + "高温降额"
        return p_kw, why

    def apply(self, p_kw, dt_s, mode):
        dt_h = dt_s / 3600.0
        vpack = max(sum(lfp_v(c["soc"]) for c in self.cells), 40.0)
        i_pack = (p_kw * 1000.0) / vpack
        for c in self.cells:
            c["bal"] = False
            cap = self.cap_ah * c["cap"]
            c["soc"] -= i_pack * dt_h / cap * 100.0
            c["soc"] = max(0.0, min(100.0, c["soc"]))
            c["t"] += (26.6 - c["t"]) * min(1.0, 0.04 * dt_s / 60.0)
            c["t"] += (0.002 + 0.8 * c["r"]) * abs(i_pack) * dt_s / 60.0
            c["t"] = max(22.0, min(58.0, c["t"]))
        self.refresh(i_pack)
        self._balance(mode, p_kw, i_pack, dt_h)
        self.refresh(i_pack)
        self._flags()
        return i_pack

    def _balance(self, mode, p_kw, i_pack, dt_h):
        if mode == "off" or dt_h <= 0:
            return
        vs = [c["v"] for c in self.cells]
        dv = max(vs) - min(vs)
        if dv < DV_BAL:
            return
        lo = min(self.cells, key=lambda c: c["v"])
        if mode == "passive":
            if p_kw > 0.05:
                return
            for c in self.cells:
                if c["v"] - lo["v"] >= DV_BAL:
                    cap = self.cap_ah * c["cap"]
                    c["soc"] -= 0.08 * dt_h / cap * 100.0
                    c["bal"] = True
        elif mode == "active":
            hi = max(self.cells, key=lambda c: c["v"])
            if hi is lo:
                return
            i_eq = 0.8
            d_hi = i_eq * dt_h / (self.cap_ah * hi["cap"]) * 100.0
            d_lo = i_eq * dt_h / (self.cap_ah * lo["cap"]) * 100.0 * 0.9
            hi["soc"] -= d_hi
            lo["soc"] += d_lo
            hi["bal"] = lo["bal"] = True

    def _flags(self):
        vs = [c["v"] for c in self.cells]
        ts = [c["t"] for c in self.cells]
        mv, mt = median(vs), median(ts)
        for c in self.cells:
            c["flag"] = ""
            if c["v"] >= V_HI_SOFT:
                c["flag"] = "高"
            elif c["v"] <= V_LO_SOFT:
                c["flag"] = "低"
            elif abs(c["v"] - mv) >= 0.040 or abs(c["t"] - mt) >= 4.0:
                c["flag"] = "偏"

    def snapshot(self):
        vs = [c["v"] for c in self.cells]
        hi = max(range(N_CELL), key=lambda i: self.cells[i]["v"])
        lo = min(range(N_CELL), key=lambda i: self.cells[i]["v"])
        nbal = sum(1 for c in self.cells if c["bal"])
        flags = [i + 1 for i, c in enumerate(self.cells) if c["flag"]]
        return {
            "n": N_CELL,
            "vmax": round(max(vs), 3),
            "vmin": round(min(vs), 3),
            "dv_mv": round((max(vs) - min(vs)) * 1000),
            "hi": hi + 1,
            "lo": lo + 1,
            "nbal": nbal,
            "odd": flags,
            "items": [
                {
                    "i": i + 1,
                    "v": round(c["v"], 3),
                    "t": round(c["t"], 1),
                    "soc": round(c["soc"], 1),
                    "bal": c["bal"],
                    "flag": c["flag"],
                }
                for i, c in enumerate(self.cells)
            ],
        }


def lfp_v(soc):
    return lerp_xy(LFP_OCV, soc)


def pv_curve(hour, peak, cloud):
    h = hour % 24
    rise, set_ = 5.7, 19.05
    if h < rise or h > set_:
        return 0.0
    x = (h - rise) / (set_ - rise)
    return peak * math.sin(math.pi * x) * cloud


class Brain:
    """按用电习惯、云量和电价往前看十二小时，决定这一拍充还是放。"""

    def __init__(self):
        self.load_hat = [lerp_table(LOAD, h + 0.5) for h in range(24)]
        self.cloud_hat = 0.88
        self.plan = []
        self.bal = "passive"
        self.llm_on = False
        self.llm_err = ""
        self._llm_p = None
        self._llm_why = ""
        self._llm_bal = None

    def accept_llm(self, cmd, pcs):
        if not cmd:
            self.llm_on = False
            return
        try:
            p = max(-pcs, min(pcs, float(cmd["p_kw"])))
        except (TypeError, ValueError, KeyError):
            self.llm_on = False
            self.llm_err = "模型返回的功率读不出来"
            return
        self._llm_p = p
        self._llm_why = str(cmd.get("why") or "模型建议")[:60]
        self._llm_bal = cmd.get("balance")
        self.llm_on = True
        self.llm_err = ""

    def observe(self, hour, load, pv, cfg):
        ih = int(hour) % 24
        self.load_hat[ih] = 0.88 * self.load_hat[ih] + 0.12 * load
        clear = pv_curve(hour, cfg["pv_kw"], 1.0)
        if clear > 0.4:
            ratio = max(0.4, min(1.0, pv / clear))
            self.cloud_hat = 0.9 * self.cloud_hat + 0.1 * ratio

    def decide(self, house, pv, load):
        cfg = house.cfg
        pcs = cfg["pcs_kw"]
        mn, mx = cfg["soc_min"], cfg["soc_max"]
        cap = max(cfg["batt_kwh"], 0.1)
        soc = house.soc
        h0 = house.now.hour + house.now.minute / 60.0
        f_pv, f_load, f_band = [], [], []
        for i in range(24):
            h = h0 + i
            f_pv.append(pv_curve(h, cfg["pv_kw"], self.cloud_hat))
            f_load.append(self.load_hat[int(h) % 24])
            f_band.append(price_of(cfg, h)[1])
        f_pv[0], f_load[0] = pv, load

        peak_need = 0.0
        pv_before_peak = 0.0
        seen_eve = False
        for i in range(1, 24):
            hh = (h0 + i) % 24
            eve = 17.5 <= hh < 22
            if f_band[i] == "峰" and eve:
                seen_eve = True
                peak_need += max(f_load[i] - f_pv[i], 0.0)
            elif not seen_eve:
                pv_before_peak += max(f_pv[i] - f_load[i], 0.0)

        room = (mx - soc) / 100.0 * cap
        avail = (soc - mn) / 100.0 * cap
        net = load - pv
        band = f_band[0]
        eta = cfg["eta"]

        if net < -0.05:
            p = max(-pcs, net)
            why = "有光伏余电，先存进电池"
        elif band == "峰":
            if net > 0.05:
                p = min(pcs, net, max(avail, 0.0))
                why = "现在峰电，用电池顶家里的用电"
            else:
                p = max(-pcs, net)
                why = "峰时仍有余电，继续入库"
        elif band == "谷":
            reserve = min(peak_need / max(eta, 0.5), (mx - mn) / 100.0 * cap)
            need_grid = reserve - avail - 0.85 * pv_before_peak
            if need_grid > 0.2 and room > 0.1:
                p = -min(pcs, need_grid, room)
                why = "谷电，且晚高峰还缺电，现在低价充"
            elif pv_before_peak > 1:
                p = 0.0
                why = "谷电，白天光伏就能充满，现在不买市电"
            else:
                p = 0.0
                why = "谷电，晚高峰电量够了，电池待命"
        else:
            keep = peak_need / max(eta, 0.5)
            if net > 0.05 and avail > keep + 0.3:
                p = min(pcs, net, avail - keep)
                why = "平电，电池先给家里用，峰时再留一截"
            elif net < -0.05:
                p = max(-pcs, net)
                why = "平电，余电入库"
            else:
                p = 0.0
                why = "平电，电量留给峰时，家里走市电"

        p = max(-pcs, min(pcs, p))
        dv = house.pack.vmax() - house.pack.vmin()
        self.bal = "active" if dv >= 0.05 else "passive"
        if self.llm_on and self._llm_p is not None:
            p = max(-pcs, min(pcs, self._llm_p))
            why = self._llm_why
            if self._llm_bal in ("passive", "active", "off"):
                self.bal = self._llm_bal
        self.plan = self._roll(h0, soc, p, f_pv[:12], f_load[:12], f_band[:12], cfg)
        return p, why

    def _roll(self, h0, soc, p0, f_pv, f_load, f_band, cfg):
        cap = max(cfg["batt_kwh"], 0.1)
        mn, mx, pcs, eta = cfg["soc_min"], cfg["soc_max"], cfg["pcs_kw"], cfg["eta"]
        out = []
        s = soc
        for i in range(12):
            p = p0 if i == 0 else 0.0
            if i > 0:
                net = f_load[i] - f_pv[i]
                band = f_band[i]
                room = (mx - s) / 100.0 * cap
                avail = (s - mn) / 100.0 * cap
                if net < -0.05:
                    p = max(-pcs, net, -room)
                elif band == "峰" and net > 0:
                    p = min(pcs, net, max(avail, 0.0))
                elif band == "谷" and room > 0.2:
                    p = 0.0
                elif net > 0 and avail > 0.4:
                    p = min(pcs, net, avail)
                else:
                    p = 0.0
            if p > 0:
                s -= p / eta / cap * 100.0
            elif p < 0:
                s += (-p) * eta / cap * 100.0
            s = max(mn, min(mx, s))
            hh = int(h0 + i) % 24
            out.append({
                "t": f"{hh:02d}:00",
                "band": f_band[i],
                "pv": round(f_pv[i], 2),
                "load": round(f_load[i], 2),
                "p": round(p, 2),
                "soc": round(s, 1),
            })
        return out


def in_hours(hour, spec):
    h = hour % 24
    for part in spec.split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            a, b = int(a), int(b)
        except ValueError:
            continue
        if a == b:
            continue
        if a < b and a <= h < b:
            return True
        if a > b and (h >= a or h < b):
            return True
    return False


def price_of(cfg, hour):
    if in_hours(hour, cfg["peak"]):
        return cfg["price_peak"], "峰"
    if in_hours(hour, cfg["valley"]):
        return cfg["price_valley"], "谷"
    return cfg["price_flat"], "平"


def load_cfg():
    DATA.mkdir(exist_ok=True)
    cfg = dict(DEFAULTS)
    if CFG_FILE.exists():
        try:
            saved = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in saved:
                    cfg[k] = type(DEFAULTS[k])(saved[k])
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return cfg


def save_cfg(cfg):
    DATA.mkdir(exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


class House:
    def __init__(self):
        self.cfg = load_cfg()
        self.lock = threading.Lock()
        self.now = datetime.now().replace(second=0, microsecond=0)
        self.mode = "auto"
        self.brain = Brain()
        self.manual = 0.0
        self.speed = 1
        self.paused = False
        self.cloud = 0.88
        self.temp = 27.4
        self.soc = 58.0
        self.pack = Pack(self.cfg["batt_kwh"], self.soc)
        self.soc = self.pack.mean_soc()
        self.temp = self.pack.tmax()
        self.reason = ""
        self.curtail = 0.0
        self.pv = self.load = self.batt = self.grid = 0.0
        self.day = None
        self.today = {}
        self.hist = []
        self.alarms = []
        self._reset_today(self.now)

    def _energy(self):
        return self.soc / 100.0 * self.cfg["batt_kwh"]

    def _set_energy(self, kwh):
        cap = max(self.cfg["batt_kwh"], 0.1)
        self.soc = max(0.0, min(100.0, 100.0 * kwh / cap))

    def _reset_today(self, t):
        self.day = t.date()
        self.today = {
            "pv": 0.0,
            "load": 0.0,
            "chg": 0.0,
            "dis": 0.0,
            "buy": 0.0,
            "sell": 0.0,
            "cut": 0.0,
            "cost": 0.0,
            "cost0": 0.0,
        }
        self.hist = []

    def pv_avail(self, hour):
        return pv_curve(hour, self.cfg["pv_kw"], self.cloud)

    def load_kw(self, hour):
        base = lerp_table(LOAD, hour)
        return max(0.12, base * (0.92 + 0.16 * self._noise))

    def _decide(self, pv, load):
        cfg = self.cfg
        pcs = cfg["pcs_kw"]
        mn, mx = cfg["soc_min"], cfg["soc_max"]
        if self.mode == "stop":
            return 0.0, "已停机"
        if self.temp >= 52:
            return 0.0, "电池过热，已停止充放"
        if self.mode == "manual":
            p, why = self.manual, "手动"
        elif self.mode == "auto":
            p, why = self.brain.decide(self, pv, load)
        elif self.mode == "tou":
            h = self.now.hour + self.now.minute / 60.0
            if in_hours(h, cfg["valley"]):
                if self.soc < mx - 0.3:
                    p, why = -pcs, "谷电充电"
                else:
                    p, why = 0.0, "谷电已充满，待机"
            elif in_hours(h, cfg["peak"]):
                p = load - pv
                why = "峰时电池供电" if p > 0.05 else ("峰时余电充电" if p < -0.05 else "峰时自用")
            else:
                p = load - pv
                why = "余电充电" if p < -0.05 else ("电池供电" if p > 0.05 else "刚好自用")
        else:
            p = load - pv
            why = "余电充电" if p < -0.05 else ("电池供电" if p > 0.05 else "刚好自用")

        p = max(-pcs, min(pcs, p))
        if not cfg["export"]:
            p = min(p, load - pv)
        if p > 0 and self.soc <= mn:
            return 0.0, "电量到下限，停止放电"
        if p < 0 and self.soc >= mx:
            return 0.0, "电量到上限，停止充电"
        return p, why

    def step(self, dt_s):
        if dt_s <= 0:
            return
        left = dt_s
        while left > 0:
            chunk = min(left, 60.0)
            self._step(chunk)
            left -= chunk

    def _step(self, dt_s):
        t = self.now + timedelta(seconds=dt_s)
        if t.date() != self.day:
            self._reset_today(t)
        self.now = t
        hour = t.hour + t.minute / 60.0 + t.second / 3600.0
        self.cloud = min(1.0, max(0.45, self.cloud + random.uniform(-0.012, 0.012)))
        self._noise = random.random()
        pv = self.pv_avail(hour)
        load = self.load_kw(hour)
        self.brain.observe(hour, load, pv, self.cfg)
        p, why = self._decide(pv, load)
        p2, why2 = self.pack.constrain(p)
        if why2:
            p, why = p2, why2
        else:
            p = p2

        dt_h = dt_s / 3600.0
        if p > 0:
            self.today["dis"] += p * dt_h
        elif p < 0:
            self.today["chg"] += (-p) * dt_h
        self.pack.cap_ah = max(self.cfg["batt_kwh"], 0.1) * 1000.0 / N_CELL / 3.2
        bal = self.cfg.get("balance", "auto")
        if bal == "auto":
            bal = self.brain.bal
        self.pack.apply(p, dt_s, bal)
        self.soc = self.pack.mean_soc()
        self.temp = self.pack.tmax()

        grid = load - pv - p
        cut = 0.0
        if (not self.cfg["export"]) and grid < -1e-6:
            cut = -grid
            pv = max(0.0, pv - cut)
            grid = load - pv - p
            if cut > 0.02:
                why += "，余电无法上网已弃光"
        self.curtail = cut
        self.reason = why

        pr, _band = price_of(self.cfg, hour)
        buy = max(grid, 0.0)
        sell = max(-grid, 0.0)
        buy0 = max(load - pv, 0.0)
        self.today["pv"] += pv * dt_h
        self.today["load"] += load * dt_h
        self.today["buy"] += buy * dt_h
        self.today["sell"] += sell * dt_h
        self.today["cut"] += cut * dt_h
        self.today["cost"] += buy * dt_h * pr
        self.today["cost0"] += buy0 * dt_h * pr

        self.pv = pv
        self.load = load
        self.batt = p
        self.grid = grid

        if not self.hist or (t - datetime.combine(t.date(), datetime.min.time())).seconds // 60 != self.hist[-1][0]:
            minute = t.hour * 60 + t.minute
            self.hist.append([minute, round(pv, 3), round(load, 3), round(p, 3), round(grid, 3), round(self.soc, 2)])
            if len(self.hist) > 1500:
                self.hist = self.hist[-1440:]

        self._alarms()

    def _alarms(self):
        a = []
        if self.temp >= 52:
            a.append("电池过热，已停止充放")
        elif self.temp >= 45:
            a.append("电池偏热，功率已减半")
        if self.soc <= self.cfg["soc_min"] + 0.3:
            a.append("电量已到下限")
        if self.soc >= self.cfg["soc_max"] - 0.3:
            a.append("电量已到上限")
        if self.curtail > 0.05:
            a.append("余电无法上网，正在弃光")
        pk = self.pack.snapshot()
        if pk["dv_mv"] >= 40:
            a.append(f"电芯压差 {pk['dv_mv']} mV，最高第{pk['hi']}节、最低第{pk['lo']}节")
        if pk["odd"]:
            a.append("第" + "、".join(str(i) for i in pk["odd"]) + "节和其余差得比较多")
        self.alarms = a

    def snapshot(self):
        hour = self.now.hour + self.now.minute / 60.0
        pr, band = price_of(self.cfg, hour)
        bal = self.pv + self.grid + self.batt - self.load
        return {
            "ts": self.now.strftime("%Y-%m-%d %H:%M"),
            "speed": self.speed,
            "paused": self.paused,
            "mode": self.mode,
            "manual": self.manual,
            "pv": round(self.pv, 3),
            "load": round(self.load, 3),
            "batt": round(self.batt, 3),
            "grid": round(self.grid, 3),
            "cut": round(self.curtail, 3),
            "soc": round(self.soc, 2),
            "temp": round(self.temp, 1),
            "reason": self.reason,
            "band": band,
            "price": pr,
            "bal": round(bal, 3),
            "today": {k: round(v, 3) for k, v in self.today.items()},
            "alarms": self.alarms,
            "cfg": self.cfg,
            "hist": self.hist[-360:],
            "cells": self.pack.snapshot(),
            "plan": self.brain.plan,
            "bal_now": self.brain.bal if self.cfg.get("balance") == "auto" else self.cfg.get("balance"),
            "llm": {
                **llmapi.status(),
                "using": self.brain.llm_on,
                "err": self.brain.llm_err,
            },
        }


house = House()
house.pv = house.load = house.batt = house.grid = 0.0
house._step(1.0)


def worker():
    while True:
        t0 = time.time()
        with house.lock:
            if not house.paused:
                house.step(house.speed * 60.0 * 0.25)
        dt = time.time() - t0
        time.sleep(max(0.05, 0.25 - dt))


def llm_loop():
    while True:
        time.sleep(25)
        try:
            if not llmapi.ready():
                continue
            with house.lock:
                if house.mode != "auto":
                    continue
                snap = house.snapshot()
            cmd = llmapi.suggest(snap)
            with house.lock:
                house.brain.accept_llm(cmd, house.cfg["pcs_kw"])
                if cmd is None:
                    house.brain.llm_err = "请求失败或返回不是 JSON"
        except Exception:
            pass


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (WEB / "index.html").read_bytes(), MIME[".html"])
            return
        if path == "/api/state":
            with house.lock:
                self._send(200, json.dumps(house.snapshot(), ensure_ascii=False), MIME[".json"])
            return
        if path == "/api/llm":
            with house.lock:
                st = dict(llmapi.status())
                st["using"] = house.brain.llm_on
                st["err"] = house.brain.llm_err
            self._send(200, json.dumps(st, ensure_ascii=False), MIME[".json"])
            return
        rel = path.lstrip("/")
        fp = (WEB / rel).resolve()
        if fp.is_file() and str(fp).startswith(str(WEB)):
            self._send(200, fp.read_bytes(), MIME.get(fp.suffix, "application/octet-stream"))
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, '{"err":"json"}', MIME[".json"])
            return
        if path == "/api/llm":
            with house.lock:
                snap = house.snapshot()
            cmd = llmapi.suggest(snap)
            with house.lock:
                house.brain.accept_llm(cmd, house.cfg["pcs_kw"])
                if cmd is None:
                    house.brain.llm_err = "未配置或请求失败"
                st = dict(llmapi.status())
                st["using"] = house.brain.llm_on
                st["err"] = house.brain.llm_err
                st["cmd"] = cmd
            self._send(200, json.dumps(st, ensure_ascii=False), MIME[".json"])
            return
        if path != "/api/ctrl" or not isinstance(req, dict):
            self._send(404, '{"err":"no"}', MIME[".json"])
            return
        with house.lock:
            if "mode" in req and req["mode"] in ("auto", "self", "tou", "manual", "stop"):
                house.mode = req["mode"]
            if "manual" in req:
                house.manual = max(-house.cfg["pcs_kw"], min(house.cfg["pcs_kw"], float(req["manual"])))
            if "speed" in req:
                house.speed = max(1, min(30, int(req["speed"])))
            if "paused" in req:
                house.paused = bool(req["paused"])
            if req.get("newday"):
                house.now = house.now.replace(hour=0, minute=0, second=0, microsecond=0)
                house._reset_today(house.now)
            if "soc" in req:
                try:
                    house.pack.set_soc(max(5.0, min(100.0, float(req["soc"]))))
                    house.soc = house.pack.mean_soc()
                    house.temp = house.pack.tmax()
                except (TypeError, ValueError):
                    pass
            if "cfg" in req and isinstance(req["cfg"], dict):
                for k, v in req["cfg"].items():
                    if k not in DEFAULTS:
                        continue
                    try:
                        if k == "export":
                            house.cfg[k] = bool(v)
                        elif k in ("peak", "valley", "balance"):
                            house.cfg[k] = str(v)
                        else:
                            house.cfg[k] = type(DEFAULTS[k])(v)
                    except (TypeError, ValueError):
                        pass
                house.cfg["soc_min"] = min(house.cfg["soc_min"], 80)
                house.cfg["soc_max"] = max(house.cfg["soc_max"], house.cfg["soc_min"] + 5)
                if house.cfg.get("balance") not in ("off", "passive", "active", "auto"):
                    house.cfg["balance"] = "auto"
                save_cfg(house.cfg)
            house._step(1.0)
            snap = house.snapshot()
        self._send(200, json.dumps(snap, ensure_ascii=False), MIME[".json"])


def check():
    h = House()
    h.mode = "self"
    h.now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    h._reset_today(h.now)
    h.pack.set_soc(50)
    h.soc = h.pack.mean_soc()
    h.cloud = 1.0
    for _ in range(60):
        h._step(60)
        bal = h.pv + h.grid + h.batt - h.load
        if abs(bal) > 0.08:
            print("balance", bal, h.pv, h.load, h.batt, h.grid)
            return 1
    h2 = House()
    h2.mode = "self"
    h2.cfg["balance"] = "passive"
    h2.pack.set_soc(92)
    h2.pack.cells[6]["soc"] = 99.6
    h2.pack.refresh(0.0)
    p0 = -5.0
    p1, why = h2.pack.constrain(p0)
    if abs(p1) > 1e-6 or "到顶" not in why:
        print("cell limit fail", p1, why)
        return 1
    got = llmapi.parse_reply('{"p_kw": -1.5, "why": "谷充", "balance": "passive"}')
    if not got or abs(got["p_kw"] + 1.5) > 1e-6:
        print("llm parse fail", got)
        return 1
    print("ok", round(h.today["pv"], 2), round(h.soc, 1), why)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(check())
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=llm_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"打开 http://127.0.0.1:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
