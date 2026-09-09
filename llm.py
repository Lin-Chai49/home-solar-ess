"""大模型接入。没配密钥时不请求，调度仍用本地规则。

把 data/llm.json 按 llm.example.json 填好即可。
兼容 OpenAI 的 /v1/chat/completions（OpenAI、DeepSeek、通义、本地 vLLM 都能用）。
模型只出建议，充放功率和电芯保护仍由 server 里的硬规则截断。
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

# 模型必须只回这段 JSON
REPLY = '{"p_kw": 0, "why": "说明", "balance": "auto"}'


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
    """成功返回 {p_kw, why, balance}，未配置或失败返回 None。"""
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
                    "你给一套户用光伏储能出充放建议。放电为正、充电为负，单位 kW。"
                    "不要超过 pcs_kw。电量要留在 soc_min 和 soc_max 之间。"
                    "有余电优先入库。峰电尽量放电顶家里用电。谷电只有晚高峰会缺电才买。"
                    "只返回 JSON，不要其它文字：" + REPLY
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
    why = str(obj.get("why") or "模型建议").strip()[:60]
    bal = str(obj.get("balance") or "auto").strip()
    if bal not in ("auto", "passive", "active", "off"):
        bal = "auto"
    return {"p_kw": p, "why": why or "模型建议", "balance": bal}
