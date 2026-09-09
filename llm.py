"""LLM hook. No request is sent until an API key is set. Dispatch still uses local rules.

Copy llm.example.json to data/llm.json.
Compatible with OpenAI /v1/chat/completions (OpenAI, DeepSeek, Qwen, local vLLM).
The model only suggests. Charge power and cell protection stay in server hard rules.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_FILE = ROOT / "data" / "llm.json"
EXAMPLE = ROOT / "llm.example.json"

DEFAULTS = {
    "enabled": False,
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "timeout": 8,
}

# Model must return only this JSON
REPLY = '{"p_kw": 0, "why": "short reason", "balance": "auto"}'


def load():
    cfg = dict(DEFAULTS)
    src = CFG_FILE if CFG_FILE.exists() else EXAMPLE
    if src.exists():
        try:
            saved = json.loads(src.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in saved:
                    cfg[k] = saved[k]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    env_key = os.environ.get("ESS_LLM_KEY", "").strip()
    env_url = os.environ.get("ESS_LLM_URL", "").strip()
    env_model = os.environ.get("ESS_LLM_MODEL", "").strip()
    if env_key:
        cfg["api_key"] = env_key
        cfg["enabled"] = True
    if env_url:
        cfg["base_url"] = env_url.rstrip("/")
    if env_model:
        cfg["model"] = env_model
    cfg["enabled"] = bool(cfg.get("enabled")) and bool(str(cfg.get("api_key") or "").strip())
    cfg["base_url"] = str(cfg.get("base_url") or DEFAULTS["base_url"]).rstrip("/")
    try:
        cfg["timeout"] = max(2, min(30, int(cfg.get("timeout") or 8)))
    except (TypeError, ValueError):
        cfg["timeout"] = 8
    return cfg


def ready():
    return load()["enabled"]


def status():
    cfg = load()
    return {
        "enabled": cfg["enabled"],
        "model": cfg["model"] if cfg["enabled"] else "",
        "base_url": cfg["base_url"] if cfg["enabled"] else "",
        "from": "env" if os.environ.get("ESS_LLM_KEY") else ("data/llm.json" if CFG_FILE.exists() else ""),
    }


def payload_ok(state):
    return {
        "ts": state.get("ts"),
        "band": state.get("band"),
        "price": state.get("price"),
        "pv_kw": state.get("pv"),
        "load_kw": state.get("load"),
        "batt_kw": state.get("batt"),
        "grid_kw": state.get("grid"),
        "soc": state.get("soc"),
        "temp": state.get("temp"),
        "pcs_kw": (state.get("cfg") or {}).get("pcs_kw"),
        "soc_min": (state.get("cfg") or {}).get("soc_min"),
        "soc_max": (state.get("cfg") or {}).get("soc_max"),
        "export": (state.get("cfg") or {}).get("export"),
        "cells": {
            "vmax": (state.get("cells") or {}).get("vmax"),
            "vmin": (state.get("cells") or {}).get("vmin"),
            "dv_mv": (state.get("cells") or {}).get("dv_mv"),
            "hi": (state.get("cells") or {}).get("hi"),
            "lo": (state.get("cells") or {}).get("lo"),
        },
        "plan": (state.get("plan") or [])[:8],
        "local_why": state.get("reason"),
    }


def suggest(state):
    """Return {p_kw, why, balance} or None if not configured / failed."""
    cfg = load()
    if not cfg["enabled"]:
        return None
    body = {
        "model": cfg["model"],
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You advise a home solar-plus-storage system. Discharge is positive, charge is negative, in kW. "
                    "Do not exceed pcs_kw. Keep SOC between soc_min and soc_max. "
                    "Store surplus first. At peak, cover the house from the battery. "
                    "At off-peak, buy from the grid only if evening peak will be short. "
                    "Return JSON only, no other text: " + REPLY
                ),
            },
            {"role": "user", "content": json.dumps(payload_ok(state), ensure_ascii=False)},
        ],
    }
    url = cfg["base_url"] + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(cfg["api_key"]).strip(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    text = ""
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_reply(text)


def parse_reply(text):
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        obj = json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "p_kw" not in obj:
        return None
    try:
        p = float(obj["p_kw"])
    except (TypeError, ValueError):
        return None
    why = str(obj.get("why") or "Model suggestion").strip()[:60]
    bal = str(obj.get("balance") or "auto").strip()
    if bal not in ("auto", "passive", "active", "off"):
        bal = "auto"
    return {"p_kw": p, "why": why or "Model suggestion", "balance": bal}
