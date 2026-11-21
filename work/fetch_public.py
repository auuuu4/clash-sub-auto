#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开订阅源接入：抓取 → 解析为 Clash proxy → 送测活。

这些源每天自动更新，是大池子；配合本地测活可以稳定产出可用节点，
不会像单个 VPN App 的节点池那样整批死亡。
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, urllib.request
import yaml

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "work", "run_re", "public")
os.makedirs(OUTDIR, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) clash-sub/1.0"

# (name, url, kind_hint, max_nodes)
SOURCES = [
    ("Barabama-merged", "https://raw.githubusercontent.com/Barabama/FreeNodes/refs/heads/main/nodes/merged.yaml", "clash", 600),
    ("free18", "https://raw.githubusercontent.com/free18/v2ray/refs/heads/main/c.yaml", "clash", 400),
    ("cbusi-clashmeta", "https://raw.githubusercontent.com/cbusifabcap/daily_free_vpn/refs/heads/main/sub/ClashMeta.yml", "clash", 400),
    ("nomorewalls", "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.meta.yml", "clash", 300),
    ("ermao", "https://blog.ermao.net/sub/clash/ermao.net", "clash", 300),
    ("zhuhaiuk", "https://raw.githubusercontent.com/zhuhaiuk/free-nodes/main/clash_config.yaml", "clash", 100),
]


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sanitize(p: dict, tag: str, idx: int) -> dict | None:
    """只保留 mihomo 必须字段，避免源配置里的非法字段导致整个配置加载失败。"""
    t = p.get("type")
    if not t or not p.get("server") or not p.get("port"):
        return None
    out = {"name": f"{tag}-{idx:03d}", "type": t, "server": p["server"], "port": int(p["port"]), "udp": True}
    for k in ("cipher", "password", "uuid", "alterId", "network", "tls", "servername", "sni",
              "skip-cert-verify", "client-fingerprint", "flow", "plugin", "obfs", "obfs-password",
              "up", "down", "auth", "auth-str"):
        v = p.get(k)
        if v not in (None, "", {}):
            out[k] = v
    for k in ("ws-opts", "grpc-opts", "reality-opts", "plugin-opts", "h2-opts", "xhttp-opts"):
        v = p.get(k)
        if isinstance(v, dict) and v:
            out[k] = v
    # 必须可被 mihomo 识别
    if t not in ("ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "anytls", "socks5", "http"):
        return None
    if out["type"] in ("ss", "ssr") and not out.get("cipher"):
        return None
    if out["type"] in ("vmess", "vless") and not out.get("uuid"):
        return None
    if out["type"] in ("trojan", "hysteria2", "anytls") and not out.get("password"):
        return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-per-source", type=int, default=0, help="0=用内置上限")
    ap.add_argument("--out", default=os.path.join(OUTDIR, "public_proxies.json"))
    args = ap.parse_args()

    all_proxies, report = [], []
    for name, url, kind, cap in SOURCES:
        try:
            text = fetch(url)
        except Exception as e:
            print(f"{name}: FETCH FAIL {type(e).__name__}: {str(e)[:80]}")
            report.append({"source": name, "url": url, "error": str(e)[:120]})
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except Exception as e:
            print(f"{name}: YAML FAIL {type(e).__name__}: {str(e)[:80]}")
            report.append({"source": name, "url": url, "error": "yaml:" + str(e)[:100]})
            continue
        raw = doc.get("proxies") or []
        keep = []
        for i, p in enumerate(raw):
            if not isinstance(p, dict):
                continue
            sp = sanitize(p, name, i)
            if sp:
                keep.append(sp)
        cap = args.limit_per_source or cap
        keep = keep[:cap]
        all_proxies.extend(keep)
        print(f"{name}: raw={len(raw)} kept={len(keep)}")
        report.append({"source": name, "url": url, "raw": len(raw), "kept": len(keep)})

    # 端点去重
    seen, uniq = set(), []
    for p in all_proxies:
        k = (p["type"], p["server"], p["port"], p.get("uuid") or p.get("password") or p.get("cipher"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    json.dump(uniq, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(report, open(os.path.join(OUTDIR, "sources.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    from collections import Counter
    print(f"\ntotal unique: {len(uniq)} {dict(Counter(p['type'] for p in uniq))}")
    print("->", args.out)


if __name__ == "__main__":
    main()
