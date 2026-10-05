#!/usr/bin/env python3
"""Local home solar + storage monitor. Default data is simulated, no real inverter."""
from __future__ import annotations

import json
import math
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

# Household load in kW. Rough 3-person home with evening cooking and AC, not measured.
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


# 16s 48V home LFP. OCV from a typical discharge curve; the mid band is flat.
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
    """Per-cell V/T. Pack current is limited by the worst cell. Home-style balancing."""

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
        base = max(0.0, min(100.0, float(soc)))
        for i, c in enumerate(self.cells):
            off = -4.0 if i == 6 else 2.2 if i == 2 else 0.0
            c["soc"] = max(0.0, min(100.0, base + off))
        self.refresh(0.0)

    def _vpack(self):
        return max(sum(lfp_v(c["soc"]) for c in self.cells), 40.0)

    def _current(self, p_kw, eta):
        """Pack amps. Discharge is positive. Same one-way eta as apply()."""
        eta = min(0.99, max(0.5, float(eta)))
        p_dc = p_kw / eta if p_kw >= 0 else p_kw * eta
        return (p_dc * 1000.0) / self._vpack()

    def ac_kw_for_mean_dsoc(self, dsoc, dt_h, eta):
        """AC kW that moves mean SOC by dsoc percent over dt_h hours."""
        if dt_h <= 0 or dsoc == 0:
            return 0.0
        eta = min(0.99, max(0.5, float(eta)))
        inv = 0.0
        for c in self.cells:
            inv += 1.0 / (self.cap_ah * c["cap"])
        inv /= float(N_CELL)
        if inv <= 0:
            return 0.0
        p_dc = -dsoc * self._vpack() / (1e5 * dt_h * inv)
        if p_dc >= 0:
            return p_dc * eta
        return p_dc / eta

    def _near_stop(self):
        if self.tmax() >= 44.0:
            return True
        for c in self.cells:
            if c["v"] >= V_HI - 0.04 or c["v"] <= V_LO + 0.08:
                return True
        return False

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

    def constrain(self, p_kw, eta=0.96):
        self.refresh(self._current(p_kw, eta))
        hi = max(self.cells, key=lambda c: c["v"])
        lo = min(self.cells, key=lambda c: c["v"])
        ih = self.cells.index(hi) + 1
        il = self.cells.index(lo) + 1
        if p_kw < -1e-6 and hi["v"] >= V_HI:
            return 0.0, f"Cell {ih} is full ({hi['v']:.3f} V). Charge stopped."
        if p_kw > 1e-6 and lo["v"] <= V_LO:
            return 0.0, f"Cell {il} is empty ({lo['v']:.3f} V). Discharge stopped."
        why = ""
        if p_kw < -1e-6 and hi["v"] >= V_HI_SOFT:
            p_kw *= 0.35
            why = f"Cell {ih} is high. Charge derated."
        elif p_kw > 1e-6 and lo["v"] <= V_LO_SOFT:
            p_kw *= 0.35
            why = f"Cell {il} is low. Discharge derated."
        ht = self.tmax()
        if ht >= 52:
            return 0.0, "A cell is too hot. Charge and discharge stopped."
        if ht >= 45 and p_kw != 0:
            p_kw *= 0.5
            why = (why + " " if why else "") + "Derated for heat."
        return p_kw, why

    def apply(self, p_kw, dt_s, mode, eta=0.96):
        """Integrate a requested AC kW. Returns (average kW, limit reason).

        Short slices so a one-minute step cannot run through a hard cell limit.
        Once charge or discharge is stopped, the rest of the step only cools.
        """
        if dt_s <= 0:
            self._flags()
            return 0.0, ""
        eta = min(0.99, max(0.5, float(eta)))
        for c in self.cells:
            c["bal"] = False
        left = float(dt_s)
        used = 0.0
        why = ""
        req = float(p_kw)
        while left > 1e-6:
            p_now, w = self.constrain(req, eta)
            if w:
                why = w
            if abs(p_now) < 1e-9:
                self._integrate(0.0, left, mode, eta)
                break
            slice_s = min(left, 1.0 if self._near_stop() else 5.0)
            self._integrate(p_now, slice_s, mode, eta)
            used += p_now * slice_s
            left -= slice_s
        self._flags()
        return used / float(dt_s), why

    def _integrate(self, p_kw, dt_s, mode, eta):
        dt_h = dt_s / 3600.0
        eta = min(0.99, max(0.5, float(eta)))
        vpack = self._vpack()
        p_dc = p_kw / eta if p_kw >= 0 else p_kw * eta
        i_pack = (p_dc * 1000.0) / vpack
        for c in self.cells:
            cap = self.cap_ah * c["cap"]
            c["soc"] -= i_pack * dt_h / cap * 100.0
            c["soc"] = max(0.0, min(100.0, c["soc"]))
            c["t"] += (26.6 - c["t"]) * min(1.0, 0.04 * dt_s / 60.0)
            c["t"] += (0.002 + 0.8 * c["r"]) * abs(i_pack) * dt_s / 60.0
            c["t"] = max(22.0, min(58.0, c["t"]))
        self.refresh(i_pack)
        self._balance(mode, p_kw, i_pack, dt_h)
        self.refresh(i_pack)

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
                    c["soc"] = max(0.0, min(100.0, c["soc"]))
                    c["bal"] = True
        elif mode == "active":
            hi = max(self.cells, key=lambda c: c["v"])
            if hi is lo:
                return
            i_eq = 0.8
            d_hi = i_eq * dt_h / (self.cap_ah * hi["cap"]) * 100.0
            d_lo = i_eq * dt_h / (self.cap_ah * lo["cap"]) * 100.0 * 0.9
            hi["soc"] = max(0.0, min(100.0, hi["soc"] - d_hi))
            lo["soc"] = max(0.0, min(100.0, lo["soc"] + d_lo))
            hi["bal"] = lo["bal"] = True

    def _flags(self):
        vs = [c["v"] for c in self.cells]
        ts = [c["t"] for c in self.cells]
        mv, mt = median(vs), median(ts)
        for c in self.cells:
            c["flag"] = ""
            if c["v"] >= V_HI_SOFT:
                c["flag"] = "hi"
            elif c["v"] <= V_LO_SOFT:
                c["flag"] = "lo"
            elif abs(c["v"] - mv) >= 0.040 or abs(c["t"] - mt) >= 4.0:
                c["flag"] = "off"

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
    """Look 12 hours ahead from load habit, cloud, and rates, then pick charge or discharge."""

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
            if self.llm_on:
                self.llm_err = "No reply this tick; still using the last suggestion."
            else:
                self.llm_err = self.llm_err or "Not configured or request failed."
            return
        try:
            p = max(-pcs, min(pcs, float(cmd["p_kw"])))
        except (TypeError, ValueError, KeyError):
            self.llm_on = False
            self.llm_err = "Could not read power from the model reply."
            return
        self._llm_p = p
        self._llm_why = str(cmd.get("why") or "Model suggestion")[:60]
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
        soc = house.soc
        h0 = house.now.hour + house.now.minute / 60.0
        f_pv, f_load, f_band = [], [], []
        for i in range(24):
            h = h0 + i
            f_pv.append(pv_curve(h, cfg["pv_kw"], self.cloud_hat))
            f_load.append(self.load_hat[int(h) % 24])
            f_band.append(price_of(cfg, h)[1])
        f_pv[0], f_load[0] = pv, load
        p, why = self._intent(0, h0, f_pv, f_load, f_band, soc, cfg)
        if self.llm_on and self._llm_p is not None:
            p = max(-pcs, min(pcs, self._llm_p))
            why = self._llm_why
        self.plan = self._roll(h0, soc, p, f_pv, f_load, f_band, cfg)
        return p, why

    def _intent(self, i, h0, f_pv, f_load, f_band, soc, cfg):
        pcs = cfg["pcs_kw"]
        mn, mx = cfg["soc_min"], cfg["soc_max"]
        cap = max(cfg["batt_kwh"], 0.1)
        eta = max(cfg["eta"], 0.5)
        n = len(f_pv)
        peak_need = 0.0
        pv_before_peak = 0.0
        seen_eve = False
        for j in range(i + 1, n):
            hh = (h0 + j) % 24
            eve = 17.5 <= hh < 22
            if f_band[j] == "Peak" and eve:
                seen_eve = True
                peak_need += max(f_load[j] - f_pv[j], 0.0)
            elif not seen_eve:
                pv_before_peak += max(f_pv[j] - f_load[j], 0.0)

        room = (mx - soc) / 100.0 * cap
        avail = (soc - mn) / 100.0 * cap
        net = f_load[i] - f_pv[i]
        band = f_band[i]

        if net < -0.05:
            p = max(-pcs, net, -max(room, 0.0))
            if p < -0.05:
                why = "Solar surplus. Store it in the battery first."
            else:
                why = "Battery is full. Not storing more."
        elif band == "Peak":
            if net > 0.05:
                p = min(pcs, net, max(avail, 0.0))
                if p > 0.05:
                    why = "Peak rate. Use the battery to cover the house."
                else:
                    why = "Peak rate. Battery is at its minimum, so the house stays on the grid."
            else:
                p = max(-pcs, net, -max(room, 0.0))
                why = "Peak rate. Load and solar are about even."
        elif band == "Off-peak":
            reserve = min(peak_need / eta, (mx - mn) / 100.0 * cap)
            need_grid = reserve - avail - 0.85 * pv_before_peak
            if need_grid > 0.2 and room > 0.1:
                p = -min(pcs, need_grid, room)
                why = "Off-peak, and evening peak still needs energy. Charge cheap now."
            elif pv_before_peak > 1:
                p = 0.0
                why = "Off-peak. Daytime solar can fill the battery, so skip grid charge."
            else:
                p = 0.0
                why = "Off-peak. Enough energy for evening peak. Battery stands by."
        else:
            keep = peak_need / eta
            if net > 0.05 and avail > keep + 0.3:
                p = min(pcs, net, avail - keep)
                if p > 0.05:
                    why = "Mid rate. Use the battery for the house, keep some for peak."
                else:
                    why = "Mid rate. Holding the reserve for evening peak."
            else:
                p = 0.0
                why = "Mid rate. Hold charge for peak. House on the grid."
        return max(-pcs, min(pcs, p)), why

    def _roll(self, h0, soc, p0, f_pv, f_load, f_band, cfg):
        cap = max(cfg["batt_kwh"], 0.1)
        mn, mx = cfg["soc_min"], cfg["soc_max"]
        eta = min(0.99, max(0.5, cfg["eta"]))
        out = []
        s = soc
        for i in range(12):
            p, _ = self._intent(i, h0, f_pv, f_load, f_band, s, cfg)
            if i == 0:
                p = p0
            # One bar is one hour. Clip to the energy the window can take,
            # and do not draw a discharge the export setting would refuse.
            p = _clip_hour(p, f_pv[i], f_load[i], s, cfg)
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


def _clip_hour(p, pv, load, soc, cfg):
    """Limit one plan hour to the inverter, the export rule, and the SOC window."""
    pcs = max(float(cfg.get("pcs_kw", 5)), 0.0)
    p = max(-pcs, min(pcs, float(p)))
    if not cfg.get("export", True) and p > 0:
        p = min(p, max(float(load) - float(pv), 0.0))
    cap = max(float(cfg.get("batt_kwh", 10)), 0.1)
    eta = min(0.99, max(0.5, float(cfg.get("eta", 0.96))))
    mn = float(cfg.get("soc_min", 0))
    mx = float(cfg.get("soc_max", 100))
    if p > 0:
        room = max(0.0, (float(soc) - mn) / 100.0 * cap)
        p = min(p, room * eta)
    elif p < 0:
        room = max(0.0, (mx - float(soc)) / 100.0 * cap)
        p = max(p, -(room / eta))
    return p


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
        return cfg["price_peak"], "Peak"
    if in_hours(hour, cfg["valley"]):
        return cfg["price_valley"], "Off-peak"
    return cfg["price_flat"], "Mid"


def as_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def finite(v):
    if isinstance(v, bool) or v is None:
        raise ValueError("bad number")
    x = float(v)
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("bad number")
    return x


def clean_hours(spec):
    raw = spec if isinstance(spec, str) else ""
    return "".join(ch for ch in raw[:80] if ch.isdigit() or ch in ",- ")


def clamp_cfg(cfg):
    """Keep saved settings inside the range the physics can run."""
    out = dict(DEFAULTS)
    src = cfg if isinstance(cfg, dict) else {}
    for k in DEFAULTS:
        if k in src:
            out[k] = src[k]

    def num(key, lo, hi):
        try:
            v = finite(out[key])
        except (TypeError, ValueError):
            v = float(DEFAULTS[key])
        out[key] = max(lo, min(hi, v))

    num("pv_kw", 0.0, 30.0)
    num("batt_kwh", 1.0, 100.0)
    num("pcs_kw", 0.2, 30.0)
    num("eta", 0.5, 0.99)
    num("price_peak", 0.0, 10.0)
    num("price_flat", 0.0, 10.0)
    num("price_valley", 0.0, 10.0)
    num("soc_min", 0.0, 80.0)
    num("soc_max", 0.0, 100.0)
    if out["soc_max"] < out["soc_min"] + 5:
        out["soc_max"] = min(100.0, out["soc_min"] + 5)
    out["export"] = as_bool(out.get("export"))
    out["peak"] = clean_hours(out.get("peak"))
    out["valley"] = clean_hours(out.get("valley"))
    bal = str(out.get("balance") or "auto")
    out["balance"] = bal if bal in ("off", "passive", "active", "auto") else "auto"
    return out


def load_cfg():
    DATA.mkdir(exist_ok=True)
    cfg = dict(DEFAULTS)
    if CFG_FILE.exists():
        try:
            saved = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            saved = None
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved:
                    cfg[k] = saved[k]
    return clamp_cfg(cfg)


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
            self.brain.plan = []
            return 0.0, "Stopped."
        # The 12-hour bars are the Auto lookahead. Clear them before the heat
        # return, or a hot pack in another mode keeps the last Auto plan.
        if self.mode != "auto":
            self.brain.plan = []
        if self.temp >= 52:
            return 0.0, "Battery too hot. Charge and discharge stopped."
        if self.mode == "manual":
            p, why = self.manual, "Manual"
        elif self.mode == "auto":
            p, why = self.brain.decide(self, pv, load)
        elif self.mode == "tou":
            h = self.now.hour + self.now.minute / 60.0
            if in_hours(h, cfg["valley"]):
                if self.soc < mx - 0.3:
                    p, why = -pcs, "Off-peak charging"
                else:
                    p, why = 0.0, "Off-peak, battery full. Standby."
            elif in_hours(h, cfg["peak"]):
                p = load - pv
                why = "Peak: battery covering the house" if p > 0.05 else ("Peak: storing surplus" if p < -0.05 else "Peak: self-use")
            else:
                p = load - pv
                why = "Storing surplus" if p < -0.05 else ("Battery covering the house" if p > 0.05 else "Load matches solar")
        else:
            p = load - pv
            why = "Storing surplus" if p < -0.05 else ("Battery covering the house" if p > 0.05 else "Load matches solar")

        p = max(-pcs, min(pcs, p))
        # Export off stops a discharge from pushing power out. It does not
        # turn a discharge command into a charge; leftover solar is curtailed.
        if not cfg["export"] and p > 0:
            p = min(p, max(load - pv, 0.0))
        if p > 0 and self.soc <= mn:
            return 0.0, "At the minimum charge. Discharge stopped."
        if p < 0 and self.soc >= mx:
            return 0.0, "At the maximum charge. Charge stopped."
        return p, why

    def _clamp_window(self, p, dt_s, eta):
        """Limit this step so mean SOC does not cross the window."""
        dt_h = dt_s / 3600.0
        if dt_h <= 0 or p == 0:
            return p
        mn, mx = self.cfg["soc_min"], self.cfg["soc_max"]
        if p > 0:
            room = mn - self.soc
            if room >= -1e-6:
                return 0.0
            cap_p = self.pack.ac_kw_for_mean_dsoc(room, dt_h, eta)
            if cap_p <= 0:
                return 0.0
            return min(p, cap_p)
        room = mx - self.soc
        if room <= 1e-6:
            return 0.0
        cap_p = self.pack.ac_kw_for_mean_dsoc(room, dt_h, eta)
        if cap_p >= 0:
            return 0.0
        return max(p, cap_p)

    def _balance_mode(self):
        choice = self.cfg.get("balance", "auto")
        if choice in ("off", "passive", "active"):
            self.brain.bal = choice
            return choice
        if (
            self.mode == "auto"
            and self.brain.llm_on
            and self.brain._llm_bal in ("passive", "active", "off")
        ):
            self.brain.bal = self.brain._llm_bal
            return self.brain._llm_bal
        dv = self.pack.vmax() - self.pack.vmin()
        self.brain.bal = "active" if dv >= 0.05 else "passive"
        return self.brain.bal

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
        eta = min(0.99, max(0.5, float(self.cfg.get("eta", 0.96))))
        self.pack.cap_ah = max(self.cfg["batt_kwh"], 0.1) * 1000.0 / N_CELL / 3.2
        p = self._clamp_window(p, dt_s, eta)
        p, why2 = self.pack.apply(p, dt_s, self._balance_mode(), eta)
        if why2:
            why = why2
        self.soc = self.pack.mean_soc()
        self.temp = self.pack.tmax()

        dt_h = dt_s / 3600.0
        if p > 0:
            self.today["dis"] += p * dt_h
        elif p < 0:
            self.today["chg"] += (-p) * dt_h

        pv_resource = pv
        grid = load - pv - p
        cut = 0.0
        if (not self.cfg["export"]) and grid < -1e-6:
            cut = -grid
            pv = max(0.0, pv - cut)
            grid = load - pv - p
            if cut > 0.02:
                why += " Surplus cannot be exported, so solar is curtailed."
        self.curtail = cut
        self.reason = why

        pr, _band = price_of(self.cfg, hour)
        buy = max(grid, 0.0)
        sell = max(-grid, 0.0)
        buy0 = max(load - pv_resource, 0.0)
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
            a.append("Battery too hot. Charge and discharge stopped.")
        elif self.temp >= 45:
            a.append("Battery is warm. Power is halved.")
        if self.soc <= self.cfg["soc_min"] + 0.3:
            a.append("At the minimum charge.")
        if self.soc >= self.cfg["soc_max"] - 0.3:
            a.append("At the maximum charge.")
        if self.curtail > 0.05:
            a.append("Surplus cannot be exported. Solar is being curtailed.")
        pk = self.pack.snapshot()
        if pk["dv_mv"] >= 40:
            a.append(f"Cell spread {pk['dv_mv']} mV. High is cell {pk['hi']}, low is cell {pk['lo']}.")
        if pk["odd"]:
            a.append("Cell " + ", ".join(str(i) for i in pk["odd"]) + " differs from the rest.")
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
            "hist": self.hist[-1440:],
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
        except Exception:
            with house.lock:
                house.brain.accept_llm(None, house.cfg["pcs_kw"])


_REJECTED = object()


def _parse_ctrl(req):
    out = {}
    if "mode" in req:
        if req["mode"] not in ("auto", "self", "tou", "manual", "stop"):
            raise ValueError("mode")
        out["mode"] = req["mode"]
    if "manual" in req:
        out["manual"] = finite(req["manual"])
    if "speed" in req:
        out["speed"] = max(1, min(30, int(finite(req["speed"]))))
    if "paused" in req:
        out["paused"] = as_bool(req["paused"])
    if req.get("newday"):
        out["newday"] = True
    if "soc" in req:
        out["soc"] = max(0.0, min(100.0, finite(req["soc"])))
    if "cfg" in req:
        if not isinstance(req["cfg"], dict):
            raise ValueError("cfg")
        out["cfg"] = req["cfg"]
    return out


def _apply_ctrl(target, parsed):
    if "cfg" in parsed:
        merged = dict(target.cfg)
        for k, v in parsed["cfg"].items():
            if k in DEFAULTS:
                merged[k] = v
        target.cfg = clamp_cfg(merged)
        save_cfg(target.cfg)
    if "mode" in parsed:
        target.mode = parsed["mode"]
    pcs = target.cfg["pcs_kw"]
    target.manual = max(-pcs, min(pcs, target.manual))
    if "manual" in parsed:
        target.manual = max(-pcs, min(pcs, parsed["manual"]))
    if "speed" in parsed:
        target.speed = parsed["speed"]
    if "paused" in parsed:
        target.paused = parsed["paused"]
    if parsed.get("newday"):
        target.now = target.now.replace(hour=0, minute=0, second=0, microsecond=0)
        target._reset_today(target.now)
    if "soc" in parsed:
        target.pack.set_soc(parsed["soc"])
        target.soc = target.pack.mean_soc()
        target.temp = target.pack.tmax()


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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        raw_n = self.headers.get("Content-Length")
        if raw_n is None or raw_n == "":
            n = 0
        else:
            try:
                n = int(raw_n)
            except ValueError:
                self._send(400, '{"err":"length"}', MIME[".json"])
                return _REJECTED
        if n < 0 or n > 65536:
            self._send(413, '{"err":"too large"}', MIME[".json"])
            return _REJECTED
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, '{"err":"json"}', MIME[".json"])
            return _REJECTED
        return {} if req is None else req

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
        if not rel or ".." in Path(rel).parts:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        fp = (WEB / rel).resolve()
        try:
            fp.relative_to(WEB.resolve())
        except ValueError:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if fp.is_file():
            self._send(200, fp.read_bytes(), MIME.get(fp.suffix, "application/octet-stream"))
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        req = self._read_json()
        if req is _REJECTED:
            return
        if path == "/api/llm":
            with house.lock:
                snap = house.snapshot()
            cmd = llmapi.suggest(snap)
            with house.lock:
                house.brain.accept_llm(cmd, house.cfg["pcs_kw"])
                if cmd is None:
                    house.brain.llm_err = "Not configured or request failed."
                st = dict(llmapi.status())
                st["using"] = house.brain.llm_on
                st["err"] = house.brain.llm_err
                st["cmd"] = cmd
            self._send(200, json.dumps(st, ensure_ascii=False), MIME[".json"])
            return
        if path != "/api/ctrl" or not isinstance(req, dict):
            self._send(404, '{"err":"no"}', MIME[".json"])
            return
        try:
            parsed = _parse_ctrl(req)
        except (TypeError, ValueError):
            self._send(400, '{"err":"number"}', MIME[".json"])
            return
        with house.lock:
            _apply_ctrl(house, parsed)
            # Paused means the simulated clock and the pack stay put.
            # The status and the plan still follow the mode just selected.
            if not house.paused:
                house._step(1.0)
            else:
                hour = house.now.hour + house.now.minute / 60.0
                _p, why = house._decide(house.pv_avail(hour), house.load_kw(hour))
                house.reason = why
            snap = house.snapshot()
        self._send(200, json.dumps(snap, ensure_ascii=False), MIME[".json"])


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


def _fresh(mode):
    h = House()
    h.cfg = dict(DEFAULTS)
    h.mode = mode
    return h


def _check_balance():
    h = _fresh("self")
    h.now = datetime(2026, 6, 21, 12, 0, 0)
    h._reset_today(h.now)
    h.pack.set_soc(50)
    h.soc = h.pack.mean_soc()
    h.cloud = 1.0
    for _ in range(60):
        h._step(60)
        bal = h.pv + h.grid + h.batt - h.load
        if abs(bal) > 0.08:
            print("balance", bal, h.pv, h.load, h.batt, h.grid)
            return 1, None
    return 0, h


def _check_cell():
    h2 = _fresh("self")
    h2.cfg["balance"] = "passive"
    h2.pack.set_soc(92)
    h2.pack.cells[6]["soc"] = 99.6
    h2.pack.refresh(0.0)
    p1, why = h2.pack.constrain(-5.0)
    if abs(p1) > 1e-6 or "full" not in why:
        print("cell limit fail", p1, why)
        return 1, ""
    return 0, why


def _check_llm():
    got = llmapi.parse_reply('{"p_kw": -1.5, "why": "off-peak charge", "balance": "passive"}')
    if not got or abs(got["p_kw"] + 1.5) > 1e-6:
        print("llm parse fail", got)
        return 1
    fenced = llmapi.parse_reply('note ```json\n{"p_kw": 1.25, "why": "peak", "balance": "sideways"}\n```')
    if not fenced or abs(fenced["p_kw"] - 1.25) > 1e-9 or fenced["balance"] != "auto":
        print("llm fence", fenced)
        return 1
    if llmapi.parse_reply("no json here") is not None:
        print("llm junk")
        return 1
    return 0


def _check_valley():
    h3 = _fresh("auto")
    h3.cfg["pv_kw"] = 0
    h3.cfg["pcs_kw"] = 1.0
    h3.now = h3.now.replace(hour=23, minute=0, second=0, microsecond=0)
    h3.pack.set_soc(30)
    h3.soc = h3.pack.mean_soc()
    h3.cloud = 0
    h3.brain.cloud_hat = 0
    h3._noise = 0.5
    pv = h3.pv_avail(23)
    load = h3.load_kw(23)
    h3.brain.observe(23, load, pv, h3.cfg)
    p, why = h3._decide(pv, load)
    if p > -0.2:
        print("auto valley charge fail", p, why)
        return 1
    if not h3.brain.plan or h3.brain.plan[1]["p"] > -0.05:
        print("plan valley fail", h3.brain.plan[:3] if h3.brain.plan else None)
        return 1
    return 0


def _check_hours():
    cfg = dict(DEFAULTS)
    expect = (
        (8, "Peak"),
        (10.9, "Peak"),
        (11, "Mid"),
        (18, "Peak"),
        (21, "Mid"),
        (23, "Off-peak"),
        (0, "Off-peak"),
        (6.5, "Off-peak"),
        (7, "Mid"),
        (12, "Mid"),
    )
    for hour, band in expect:
        got = price_of(cfg, hour)[1]
        if got != band:
            print("band", hour, got, band)
            return 1
    if in_hours(12, "") or in_hours(12, "nope"):
        print("hours junk")
        return 1
    overlap = dict(DEFAULTS)
    overlap["peak"] = "0-5"
    overlap["valley"] = "0-5"
    if price_of(overlap, 1)[1] != "Peak":
        print("peak should win overlap")
        return 1
    return 0


def _check_clamp():
    c = clamp_cfg({
        "pv_kw": -1,
        "batt_kwh": 1000,
        "pcs_kw": 0,
        "soc_min": 99,
        "soc_max": 1,
        "eta": 2,
        "export": "false",
        "price_peak": -3,
        "price_flat": "x",
        "price_valley": 1,
        "peak": "8-11,<script>",
        "valley": "23-7",
        "balance": "nope",
    })
    if c["pv_kw"] != 0 or c["batt_kwh"] != 100 or abs(c["pcs_kw"] - 0.2) > 1e-9:
        print("clamp size", c["pv_kw"], c["batt_kwh"], c["pcs_kw"])
        return 1
    if c["soc_min"] != 80 or c["soc_max"] < 85:
        print("clamp soc", c["soc_min"], c["soc_max"])
        return 1
    if abs(c["eta"] - 0.99) > 1e-9 or c["export"] or c["price_peak"] != 0:
        print("clamp eta export price", c["eta"], c["export"], c["price_peak"])
        return 1
    if "<" in c["peak"] or c["balance"] != "auto":
        print("clamp text", c["peak"], c["balance"])
        return 1
    if abs(c["price_flat"] - DEFAULTS["price_flat"]) > 1e-9:
        print("clamp flat", c["price_flat"])
        return 1
    if clamp_cfg({"export": "true"})["export"] is not True:
        print("export true")
        return 1
    return 0


def _check_window():
    h = _fresh("manual")
    h.cfg["soc_min"] = 30
    h.cfg["soc_max"] = 90
    h.cfg["pcs_kw"] = 5
    h.manual = 5
    h.pack.set_soc(31)
    h.soc = h.pack.mean_soc()
    h.now = datetime(2026, 6, 21, 3, 0, 0)
    h._reset_today(h.now)
    h.cloud = 0.5
    for _ in range(8):
        h._step(60)
        bal = h.pv + h.grid + h.batt - h.load
        if abs(bal) > 0.08:
            print("window balance", bal, h.pv, h.load, h.batt, h.grid)
            return 1
    if h.soc < 29.95 or h.soc > 30.4:
        print("soc min window", h.soc, h.batt, h.reason)
        return 1
    h.manual = -1
    h.cfg["soc_max"] = 60
    h.pack.set_soc(59)
    h.soc = h.pack.mean_soc()
    for _ in range(10):
        h._step(60)
        if h.soc > 60.08:
            print("soc max window", h.soc, h.batt, h.reason)
            return 1
    if h.soc < 59.5:
        print("soc max not reached", h.soc, h.batt, h.reason)
        return 1
    return 0


def _check_export():
    h = _fresh("manual")
    h.cfg["export"] = False
    h.manual = 1
    h.pack.set_soc(50)
    h.soc = h.pack.mean_soc()
    p, why = h._decide(5, 1)
    if p < -0.05 or p > 0.05:
        print("manual surplus", p, why)
        return 1
    h.manual = -1
    p, why = h._decide(5, 1)
    if p > -0.5:
        print("manual charge", p, why)
        return 1
    h.mode = "self"
    p, why = h._decide(5, 1)
    if p > -0.5:
        print("self surplus", p, why)
        return 1
    return 0


def _check_curtail():
    h = _fresh("stop")
    h.cfg["export"] = False
    h.cloud = 1.0
    h.now = datetime(2026, 6, 21, 12, 0, 0)
    h._reset_today(h.now)
    h._step(3600)
    if h.today["cut"] <= 0.2:
        print("curtail", h.today, h.pv, h.load, h.batt)
        return 1
    if h.today["cost0"] > 0.05:
        print("cost0", h.today, h.pv, h.load)
        return 1
    if h.grid < -0.05:
        print("export leaked", h.grid)
        return 1
    bal = h.pv + h.grid + h.batt - h.load
    if abs(bal) > 0.08:
        print("curtail balance", bal, h.pv, h.grid, h.batt, h.load)
        return 1
    return 0


def _check_soc_track():
    h = _fresh("stop")
    h.pack.cap_ah = h.cfg["batt_kwh"] * 1000.0 / N_CELL / 3.2
    eta = h.cfg["eta"]
    dt = 4.0
    dt_h = dt / 3600.0
    for p in (2.0, -2.0):
        h.pack.set_soc(50)
        h.pack.refresh(0.0)
        vpack = h.pack._vpack()
        inv = sum(1.0 / (h.pack.cap_ah * c["cap"]) for c in h.pack.cells) / N_CELL
        p_dc = p / eta if p >= 0 else p * eta
        i_pack = p_dc * 1000.0 / vpack
        dsoc = -i_pack * dt_h * 100.0 * inv
        back = h.pack.ac_kw_for_mean_dsoc(dsoc, dt_h, eta)
        if abs(back - p) > 0.02:
            print("soc track power", p, back, dsoc)
            return 1
        before = h.pack.mean_soc()
        h.pack.apply(p, dt, "off", eta)
        after = h.pack.mean_soc()
        if abs((after - before) - dsoc) > 0.002:
            print("soc track", p, before, after, dsoc)
            return 1
    return 0


def _check_plan():
    h = _fresh("auto")
    h.now = datetime(2026, 6, 21, 19, 0, 0)
    h._reset_today(h.now)
    h.pack.set_soc(15)
    h.soc = h.pack.mean_soc()
    h._noise = 0.5
    p, why = h._decide(0.0, 1.2)
    if p > 0.05 or "minimum" not in why:
        print("floor why", p, why, h.soc)
        return 1
    if not h.brain.plan or h.brain.plan[0]["p"] > 0.05:
        print("floor plan", h.brain.plan[:1] if h.brain.plan else None)
        return 1
    h.mode = "self"
    h.temp = 60
    p, why = h._decide(0.0, 1.2)
    if h.brain.plan or "hot" not in why.lower():
        print("hot plan", p, why, h.brain.plan[:1] if h.brain.plan else None)
        return 1
    h.temp = 30
    h.mode = "auto"
    h.pack.set_soc(95)
    h.soc = h.pack.mean_soc()
    h.now = datetime(2026, 6, 21, 12, 0, 0)
    p, why = h._decide(4.0, 0.4)
    if p < -0.05 or "Store it" in why or "full" not in why.lower():
        print("full why", p, why, h.soc)
        return 1
    h.pack.set_soc(16)
    h.soc = h.pack.mean_soc()
    h.now = datetime(2026, 6, 21, 19, 0, 0)
    h.brain.llm_on = True
    h.brain._llm_p = 5.0
    h.brain._llm_why = "dump the pack"
    p, why = h._decide(0.0, 1.2)
    bar = h.brain.plan[0]["p"] if h.brain.plan else None
    if bar is None or bar > 0.2 or bar < 0:
        print("llm plan", h.soc, p, why, bar)
        return 1
    h.mode = "self"
    h._step(60)
    if h.brain.plan:
        print("stale plan", len(h.brain.plan), h.reason)
        return 1

    off = dict(DEFAULTS)
    off["export"] = False
    got = _clip_hour(3, 0.2, 0.5, 50, off)
    if got < 0 or got > 0.3 + 1e-9:
        print("export clip", got)
        return 1
    on = dict(DEFAULTS)
    got = _clip_hour(3, 0.2, 0.5, 50, on)
    if abs(got - 3) > 1e-9:
        print("export on", got)
        return 1
    if _clip_hour(5, 0.0, 2.0, 15, on) > 0.05:
        print("soc floor clip", _clip_hour(5, 0.0, 2.0, 15, on))
        return 1
    if _clip_hour(-5, 0.0, 2.0, 95, on) < -0.05:
        print("soc ceil clip", _clip_hour(-5, 0.0, 2.0, 95, on))
        return 1
    return 0


def _check_manual_limit():
    backup = CFG_FILE.read_bytes() if CFG_FILE.exists() else None
    try:
        h = _fresh("manual")
        h.manual = 4
        _apply_ctrl(h, {"cfg": {"pcs_kw": 1}})
        if abs(h.manual - 1) > 1e-9 or abs(h.cfg["pcs_kw"] - 1) > 1e-9:
            print("manual pcs", h.manual, h.cfg["pcs_kw"])
            return 1
        _apply_ctrl(h, {"manual": -3})
        if abs(h.manual + 1) > 1e-9:
            print("manual low", h.manual)
            return 1
        return 0
    finally:
        if backup is None:
            if CFG_FILE.exists():
                CFG_FILE.unlink()
        else:
            CFG_FILE.write_bytes(backup)


def _check_http():
    import http.client

    prev_mode = house.mode
    prev_manual = house.manual
    prev_paused = house.paused
    httpd = Server(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        status = resp.status
        conn.close()
        return status, payload

    try:
        status, payload = call("GET", "/../server.py")
        if status != 404 or payload != b"not found":
            print("traversal", status, payload[:40])
            return 1
        status, payload = call("GET", "/s.css")
        if status != 200 or b"--bg" not in payload:
            print("css", status)
            return 1
        status, payload = call("GET", "/api/state")
        if status != 200 or b'"soc"' not in payload or b'"hist"' not in payload:
            print("state", status, payload[:80])
            return 1
        status, _payload = call(
            "POST",
            "/api/ctrl",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "nope"},
        )
        if status != 400:
            print("bad length", status)
            return 1
        status, _payload = call(
            "POST",
            "/api/ctrl",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "999999"},
        )
        if status != 413:
            print("too large", status)
            return 1
        house.manual = 1.5
        status, _payload = call(
            "POST",
            "/api/ctrl",
            body=b'{"manual":"nope"}',
            headers={"Content-Type": "application/json"},
        )
        if status != 400 or abs(house.manual - 1.5) > 1e-9:
            print("bad manual", status, house.manual)
            return 1
        moved_from = house.now
        status, payload = call(
            "POST",
            "/api/ctrl",
            body=b'{"mode":"stop"}',
            headers={"Content-Type": "application/json"},
        )
        if status != 200 or b'"mode": "stop"' not in payload and b'"mode":"stop"' not in payload:
            print("mode stop", status, payload[:120])
            return 1
        if house.now == moved_from:
            print("clock stuck", house.now)
            return 1
        house.paused = True
        house.mode = "auto"
        house.brain.plan = [{"t": "00:00", "p": 1}]
        held = house.now
        status, payload = call(
            "POST",
            "/api/ctrl",
            body=b'{"mode":"self"}',
            headers={"Content-Type": "application/json"},
        )
        if status != 200 or house.now != held or house.brain.plan:
            print("paused", status, house.now, held, house.brain.plan[:1] if house.brain.plan else None)
            return 1
        if b'"mode": "self"' not in payload and b'"mode":"self"' not in payload:
            print("paused mode", payload[:120])
            return 1
        return 0
    finally:
        house.mode = prev_mode
        house.manual = prev_manual
        house.paused = prev_paused
        httpd.shutdown()
        httpd.server_close()


def check():
    code, noon = _check_balance()
    if code:
        return code
    code, why = _check_cell()
    if code:
        return code
    for part in (
        _check_llm,
        _check_valley,
        _check_hours,
        _check_clamp,
        _check_window,
        _check_export,
        _check_curtail,
        _check_soc_track,
        _check_plan,
        _check_manual_limit,
        _check_http,
    ):
        code = part()
        if code:
            return code
    print("ok", round(noon.today["pv"], 2), round(noon.soc, 1), why)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(check())
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=llm_loop, daemon=True).start()
    try:
        httpd = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use.", file=sys.stderr)
        sys.exit(1)
    print(f"Open http://127.0.0.1:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
