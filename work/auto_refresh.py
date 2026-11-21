#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动续期流水线：收割 → 测活 → 质量筛选 → 分组 → 生成订阅 → 上传 Gist。

用法:
  python auto_refresh.py                # 默认 fast：只重收 SuperVPN（12h token），复用静态源缓存
  python auto_refresh.py --mode full    # 全部重收（SuperVPN + FastVPN + TunnelDOOM + CapVPN + ZeroPing）
  python auto_refresh.py --no-upload    # 只本地生成，不更新 Gist
  python auto_refresh.py --no-harvest   # 跳过收割，直接用现有数据重测活

退出码: 0 成功；1 有步骤失败但订阅已生成；2 订阅未生成（严重失败）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yaml

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------- 路径配置 ----------------
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)                       # clash-proxy-research
WORK = os.path.join(ROOT, "work")
REPOS = os.path.join(WORK, "repos")
REPORTS = os.path.join(ROOT, "reports")
RUNDIR = os.path.join(WORK, "run_re")
TOOLS = os.path.join(WORK, "tools")

IS_WIN = os.name == "nt"
_MIHOMO_NAME = "mihomo-windows-amd64.exe" if IS_WIN else "mihomo"
MIHOMO = (os.environ.get("MIHOMO_BIN")
          or os.path.join(TOOLS, "mihomo", _MIHOMO_NAME))

SUPERVPN = os.path.join(REPOS, "lza6__SuperVPN_Pro-APP-")
FASTVPN = os.path.join(REPOS, "lza6__FastVPN-Astro-ProxyPool")
TUNNELDOOM = os.path.join(REPOS, "lza6__MultiVPN-APK-ProxyPool")
CAPVPN = os.path.join(REPOS, "lza6__Cap-VPN-APP")
ZEROPING = os.path.join(REPOS, "lza6__ZeroPing-APP-2api")

GIST_ID = "157f84b437bf5ed29ea167200fe5c5ca"
GIST_FILE = "valid_nodes_clash.yaml"
CST = ZoneInfo("Asia/Shanghai")

YAML_OUT = os.path.join(REPORTS, "valid_nodes_clash.yaml")
CSV_OUT = os.path.join(REPORTS, "nodes_latest.csv")
SUMMARY_OUT = os.path.join(REPORTS, "latest_summary.json")
STATE_OUT = os.path.join(WORK, "auto_refresh_state.json")

SHORT = {"FastVPN-Astro": "FV", "SuperVPN-Pro": "SV", "TunnelDOOM": "TD", "CapVPN": "CAP",
         "ZeroPing": "ZP", "Public": "PUB"}
IP_API = "http://ip-api.com/json/?fields=status,country,countryCode,isp,query"
GSTATIC = "https://www.gstatic.com/generate_204"
SPEED = "https://speed.cloudflare.com/__down?bytes=262144"

LOG_PATH = os.path.join(WORK, "auto_refresh.log")
_log_fh = None


def log(msg: str) -> None:
    global _log_fh
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if _log_fh is None:
            _log_fh = open(LOG_PATH, "a", encoding="utf-8")
        _log_fh.write(line + "\n")
        _log_fh.flush()
    except Exception:
        pass


MIHOMO_NAMES = ("mihomo-windows-amd64", "mihomo", "clash-meta")


def kill_mihomo(why=""):
    """结束所有残留 mihomo 进程。它们不退出会长期占用我们的测试端口段，
    导致下一次测活绑不上端口、结果串线（曾导致 70/72 节点被误判为死）。"""
    killed = []
    if IS_WIN:
        for name in ("mihomo-windows-amd64", "mihomo", "clash-meta"):
            try:
                p = subprocess.run(["taskkill", "/F", "/IM", f"{name}.exe"], capture_output=True,
                                   text=True, timeout=30, encoding="utf-8", errors="replace")
                if p.returncode == 0:
                    killed += [l for l in (p.stdout or "").splitlines() if l.strip()]
            except Exception:
                pass
    else:
        for pat in ("mihomo-windows-amd64", "mihomo"):
            try:
                p = subprocess.run(["pkill", "-f", pat], capture_output=True, text=True, timeout=20)
                if p.returncode == 0:
                    killed.append(pat)
            except Exception:
                pass
    if killed:
        log(f"cleanup mihomo ({why}): " + " | ".join(str(k).strip() for k in killed[:4]))
    time.sleep(1.0)
    return bool(killed)


def run(cmd, cwd=None, timeout=900, label=""):
    """运行子进程，返回 (ok, output)。"""
    log(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}" + (f"   [cwd={cwd}]" if cwd else ""))
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            log(f"  ! {label or 'step'} exit={p.returncode}\n  {out.strip()[-1200:]}")
            return False, out
        tail = "\n".join(out.strip().splitlines()[-6:])
        log(f"  ok {label}\n  {tail}")
        return True, out
    except subprocess.TimeoutExpired:
        log(f"  ! {label} TIMEOUT after {timeout}s")
        return False, "timeout"
    except Exception as e:
        log(f"  ! {label} EXC {type(e).__name__}: {e}")
        return False, str(e)


# ---------------- 收割 ----------------
def harvest_supervpn() -> bool:
    kill_mihomo("before SuperVPN harvest")
    ok1, _ = run([sys.executable, "-u", "tools/harvest.py"], cwd=SUPERVPN, timeout=420, label="SuperVPN harvest")
    if not ok1:
        return False
    ok2, _ = run([sys.executable, "-u", "tools/mihomo_pool.py", "12"], cwd=SUPERVPN, timeout=420,
                 label="SuperVPN livetest")
    kill_mihomo("after SuperVPN livetest")   # mihomo_pool 有意不退出进程，必须手动清理
    if not ok2:
        return False
    ok3, _ = run([sys.executable, os.path.join(BASE, "build_supervpn_alive.py")], timeout=120,
                 label="SuperVPN build alive")
    return ok3


def harvest_fastvpn() -> bool:
    outdir = os.path.join(RUNDIR, "fastvpn_auto")
    os.makedirs(outdir, exist_ok=True)
    ok, _ = run([sys.executable, "-u", os.path.join(FASTVPN, "tools", "harvest_fastvpn.py"), outdir],
                timeout=240, label="FastVPN harvest")
    if not ok:
        return False
    ok2, _ = run([sys.executable, "-u", os.path.join(BASE, "test_clash_nodes.py"),
                  "--servers", os.path.join(outdir, "servers_decrypted.json"),
                  "--outdir", os.path.join(outdir, "test"), "--base-port", "31000"],
                 timeout=600, label="FastVPN test")
    return ok2


def harvest_tunneldoom() -> bool:
    os.makedirs(os.path.join(TUNNELDOOM, "data"), exist_ok=True)
    ok, _ = run([sys.executable, "-u", "tools/decrypt_tunneldoom.py", "--fetch"], cwd=TUNNELDOOM,
                timeout=240, label="TunnelDOOM decrypt")
    if not ok:
        return False
    ok2, _ = run([sys.executable, os.path.join(BASE, "build_tunneldoom.py")], timeout=120, label="TunnelDOOM build")
    if not ok2:
        return False
    ok3, _ = run([sys.executable, "-u", os.path.join(BASE, "test_proxies.py"),
                  "--proxies", os.path.join(RUNDIR, "tunneldoom", "proxies.json"),
                  "--outdir", os.path.join(RUNDIR, "tunneldoom", "test_auto"), "--base-port", "32000"],
                 timeout=600, label="TunnelDOOM test")
    return ok3


def harvest_capvpn() -> bool:
    ok, _ = run([sys.executable, "-u", "tools/harvest.py"], cwd=CAPVPN, timeout=240, label="CapVPN harvest")
    if not ok:
        return False
    ok2, _ = run([sys.executable, "-u", "tools/export_links.py", os.path.join(CAPVPN, "dist")],
                 cwd=CAPVPN, timeout=180, label="CapVPN export")
    if not ok2:
        return False
    ok3, _ = run([sys.executable, "-u", os.path.join(BASE, "build_capvpn.py")], timeout=120, label="CapVPN build")
    if not ok3:
        return False
    ok4, _ = run([sys.executable, "-u", os.path.join(BASE, "test_proxies.py"),
                  "--proxies", os.path.join(RUNDIR, "capvpn", "proxies.json"),
                  "--outdir", os.path.join(RUNDIR, "capvpn", "test"), "--base-port", "30000"],
                 timeout=600, label="CapVPN test")
    return ok4


def harvest_zeroping() -> bool:
    ok, _ = run([sys.executable, os.path.join(BASE, "build_zeroping.py")], timeout=120, label="ZeroPing build")
    if not ok:
        return False
    ok2, _ = run([sys.executable, "-u", os.path.join(BASE, "test_proxies.py"),
                  "--proxies", os.path.join(RUNDIR, "zeroping", "proxies.json"),
                  "--outdir", os.path.join(RUNDIR, "zeroping", "test_auto"), "--base-port", "36000"],
                 timeout=900, label="ZeroPing test")
    return ok2


PUBLIC_DIR = os.path.join(RUNDIR, "public")


def harvest_public(sample=0) -> bool:
    """公开订阅源：每天自动更新的大池子，保证基础供给不会断。
    只做抓取 + TCP 预筛，完整测活交给统一的 evaluate 阶段（避免重复测两遍）。"""
    ok, _ = run([sys.executable, "-u", os.path.join(BASE, "fetch_public.py")], timeout=420, label="public fetch")
    if not ok:
        return False
    args = [sys.executable, "-u", os.path.join(BASE, "prefilter_tcp.py"),
            "--timeout", "4", "--workers", "48", "--max-keep", "350"]
    if sample:
        args += ["--sample", str(sample)]
    ok2, _ = run(args, timeout=420, label="public tcp prefilter")
    return ok2


# ---------------- 采集候选 ----------------
STATIC_SOURCES = [
    ("FastVPN-Astro", [
        os.path.join(RUNDIR, "fastvpn_auto", "test", "clash_alive.yaml"),
        os.path.join(RUNDIR, "fastvpn_astro_20261119", "test", "clash_alive.yaml"),
        os.path.join(RUNDIR, "fastvpn_astro", "test", "clash_alive.yaml"),
    ], "static"),
    ("TunnelDOOM", [
        os.path.join(RUNDIR, "tunneldoom", "test_auto", "clash_alive.yaml"),
        os.path.join(RUNDIR, "tunneldoom", "test_20261119", "clash_alive.yaml"),
        os.path.join(RUNDIR, "tunneldoom", "test", "clash_alive.yaml"),
    ], "cfg"),
    ("CapVPN", [
        os.path.join(RUNDIR, "capvpn", "test", "clash_alive.yaml"),
    ], "static"),
    ("ZeroPing", [
        os.path.join(RUNDIR, "zeroping", "test_auto", "clash_alive.yaml"),
        os.path.join(RUNDIR, "zeroping", "test", "clash_alive.yaml"),
    ], "static"),
]
SUPERVPN_ALIVE = os.path.join(SUPERVPN, "recon", "pool", "clash_alive.yaml")


def first_existing(paths):
    for p in paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def sup_token_issued(password: str):
    m = re.search(r"@(\d{10})@", password or "")
    return datetime.fromtimestamp(int(m.group(1)), timezone.utc) if m else None


def collect_candidates(include_static: bool = True, include_public: bool = True) -> list[dict]:
    """返回 [{proxy, source, source_index, validity_type, issued_at, expires_at}]"""
    out = []
    # SuperVPN（12h token）
    if os.path.exists(SUPERVPN_ALIVE):
        doc = yaml.safe_load(open(SUPERVPN_ALIVE, encoding="utf-8")) or {}
        for i, p in enumerate(doc.get("proxies") or []):
            issued = sup_token_issued(p.get("password", ""))
            out.append({
                "proxy": dict(p), "source": "SuperVPN-Pro", "source_index": i,
                "validity_type": "12h-token",
                "issued_at": issued.isoformat() if issued else None,
                "expires_at": (issued + timedelta(hours=12)).isoformat() if issued else None,
            })
    if include_static:
        for source, paths, vtype in STATIC_SOURCES:
            path = first_existing(paths)
            if not path:
                log(f"  - static source unavailable: {source}")
                continue
            doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
            n_before = len(out)
            for i, p in enumerate(doc.get("proxies") or []):
                out.append({
                    "proxy": dict(p), "source": source, "source_index": i,
                    "validity_type": vtype, "issued_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": None,
                })
            log(f"  - {source}: {len(out)-n_before} from {os.path.relpath(path, ROOT)}")

    if include_public:
        pub = os.path.join(PUBLIC_DIR, "public_tcp_ok.json")
        if os.path.exists(pub) and os.path.getsize(pub) > 0:
            items = json.load(open(pub, encoding="utf-8"))
            for i, p in enumerate(items):
                out.append({
                    "proxy": dict(p), "source": "Public", "source_index": i,
                    "validity_type": "public", "issued_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": None,
                })
            log(f"  - Public: {len(items)} from work/run_re/public/public_tcp_ok.json")
        else:
            log("  - Public: no prefiltered list, skipped")
    log(f"candidates: {len(out)} " + str(dict(Counter(c['source'] for c in out))))
    return out


# ---------------- 评估 ----------------
def wait_port(port, timeout=35):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def probe_port(port, timeout=12):
    r = {"ip_ok": False, "ip_rtt_ms": None, "exit_ip": None, "country": None, "country_code": None,
         "isp": None, "https_ok": False, "https_status": None, "https_ms": None,
         "speed_kbps": None, "error": None}
    ses = requests.Session()
    ses.trust_env = False
    px = {"http": f"socks5h://127.0.0.1:{port}", "https": f"socks5h://127.0.0.1:{port}"}
    try:
        t0 = time.time()
        resp = ses.get(IP_API, proxies=px, timeout=timeout)
        r["ip_rtt_ms"] = int((time.time() - t0) * 1000)
        info = resp.json() if resp.status_code == 200 else {}
        if info.get("status") == "success" and info.get("query"):
            r.update(ip_ok=True, exit_ip=info["query"], country=info.get("country"),
                     country_code=info.get("countryCode"), isp=(info.get("isp") or "")[:60])
        else:
            r["error"] = f"ip-api:{str(info)[:60]}"
    except Exception as e:
        r["error"] = "ip:" + type(e).__name__ + ":" + str(e)[:60]
    if r["ip_ok"]:
        try:
            t0 = time.time()
            resp = ses.get(GSTATIC, proxies=px, timeout=timeout)
            r["https_ms"] = int((time.time() - t0) * 1000)
            r["https_status"] = resp.status_code
            r["https_ok"] = resp.status_code == 204
        except Exception as e:
            r["error"] = (r["error"] + " | " if r["error"] else "") + "https:" + type(e).__name__
        try:
            t0 = time.time()
            resp = ses.get(SPEED, proxies=px, timeout=18)
            ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200 and ms > 0:
                r["speed_kbps"] = round(len(resp.content) / ms, 1)
        except Exception:
            pass
    return r


def evaluate(candidates: list[dict], base_port=51000, workers=12, batch=150) -> list[dict]:
    """分批测活：mihomo 单实例挂太多 listeners 会不稳，且端口段有限。"""
    if not candidates:
        return []
    from concurrent.futures import ThreadPoolExecutor

    evaldir = os.path.join(RUNDIR, "auto_eval")
    os.makedirs(evaldir, exist_ok=True)
    results: list[dict] = []
    total_ok = 0
    t_all = time.time()

    for start in range(0, len(candidates), batch):
        chunk = candidates[start:start + batch]
        kill_mihomo(f"before evaluate batch {start//batch + 1}")
        proxies = []
        for c in chunk:
            p = dict(c["proxy"])
            p["name"] = f"{c['source']}-{c['source_index']:03d}"
            proxies.append(p)
        cfg = {"log-level": "warning", "mode": "global",
               "profile": {"store-selected": False, "store-fake-ip": False},
               "proxies": proxies, "listeners": [], "rules": ["MATCH,DIRECT"]}
        for i, p in enumerate(proxies):
            cfg["listeners"].append({"name": f"li-{i}", "type": "socks", "listen": "127.0.0.1",
                                     "port": base_port + i, "proxy": p["name"], "udp": True})
        cfg_path = os.path.join(evaldir, f"eval_{start//batch}.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        log_path = os.path.join(evaldir, f"mihomo_{start//batch}.log")
        logf = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen([MIHOMO, "-d", evaldir, "-f", cfg_path], stdout=logf,
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if not wait_port(base_port):
                log(f"! batch {start//batch+1}: mihomo failed to bind {base_port}")
                txt = open(log_path, encoding="utf-8", errors="replace").read()
                log(txt[-800:])
                continue
            # 校验 mihomo 真的加载了配置（防止端口被别的进程占用造成假阳性）
            time.sleep(1.5)
            mlog = ""
            try:
                mlog = open(log_path, encoding="utf-8", errors="replace").read()
            except Exception:
                pass
            if "Initial configuration complete" not in mlog and "configuration file" not in mlog:
                log(f"! batch {start//batch+1}: mihomo log does not show successful init; skip batch")
                log(mlog[-600:])
                continue

            def job(pair):
                i, c = pair
                r = probe_port(base_port + i)
                r.update({k: c.get(k) for k in
                          ("source", "source_index", "validity_type", "issued_at", "expires_at")})
                r["proxy"] = c["proxy"]
                return r

            with ThreadPoolExecutor(max_workers=workers) as pool:
                part = list(pool.map(job, enumerate(chunk)))
            results.extend(part)
            ok = sum(1 for r in part if r["ip_ok"] and r["https_ok"])
            total_ok += ok
            log(f"  batch {start//batch+1}: {ok}/{len(part)} passed "
                f"(running total {len(results)}/{len(candidates)})")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except Exception:
                proc.kill()
            logf.close()
            kill_mihomo("after evaluate batch")

    log(f"evaluated {len(results)} proxies: {total_ok} passed in {int(time.time()-t_all)}s")
    return results


# ---------------- 筛选与生成 ----------------
def validity_label(rec):
    if rec["source"] == "SuperVPN-Pro" and rec.get("expires_at"):
        return "exp " + datetime.fromisoformat(rec["expires_at"]).astimezone(CST).strftime("%m-%d %H:%M")
    if rec["source"] == "Public":
        return "pub"
    return "static"


def score(r):
    sp = r.get("speed_kbps")
    sp = 15 if sp is None else sp
    return sp * 2 - (r.get("ip_rtt_ms") or 9999) / 20 - (r.get("https_ms") or 9999) / 50


def finalize(results, max_rtt=3000, min_speed=25.0, max_nodes=120):
    reasons = Counter()
    valid = []
    for r in results:
        if not (r.get("ip_ok") and r.get("https_ok")):
            reasons["dead_or_https"] += 1
            continue
        if not r.get("country_code"):
            reasons["no_country"] += 1
            continue
        if (r.get("ip_rtt_ms") or 99999) > max_rtt:
            reasons["latency"] += 1
            continue
        if r.get("speed_kbps") is not None and r["speed_kbps"] < min_speed:
            reasons["speed"] += 1
            continue
        if r.get("expires_at"):
            try:
                if datetime.fromisoformat(r["expires_at"]) <= datetime.now(timezone.utc):
                    reasons["token_expired"] += 1
                    continue
            except Exception:
                pass
        valid.append(r)

    by_ip = defaultdict(list)
    for r in valid:
        by_ip[r.get("exit_ip") or ""].append(r)
    deduped = []
    for _, group in by_ip.items():
        group.sort(key=score, reverse=True)
        deduped.append(group[0])
        reasons["dup_exit_ip"] += max(0, len(group) - 1)

    final, seen_ep = [], set()
    for r in sorted(deduped, key=score, reverse=True):
        p = r["proxy"]
        ep = (p.get("server"), p.get("port"), p.get("uuid") or p.get("password"))
        if ep in seen_ep:
            reasons["dup_endpoint"] += 1
            continue
        seen_ep.add(ep)
        final.append(r)
    if len(final) > max_nodes:
        reasons["over_max"] += len(final) - max_nodes
        final = final[:max_nodes]
    return final, reasons


def write_outputs(final, reasons, cand_total, out_dir=None):
    out_dir = out_dir or REPORTS
    os.makedirs(out_dir, exist_ok=True)
    yaml_path = os.path.join(out_dir, "valid_nodes_clash.yaml")
    csv_path = os.path.join(out_dir, "nodes_latest.csv")
    summary_path = os.path.join(out_dir, "latest_summary.json")
    now = datetime.now(CST)
    proxies, rows, used = [], [], set()
    for r in final:
        p = dict(r["proxy"])
        cc = r.get("country_code") or "XX"
        src = SHORT.get(r["source"], r["source"])
        idx = int(r.get("source_index", 0)) + 1
        label = validity_label(r)
        name = f"[{cc}] {src}-{idx:02d} {label}"
        n = 1
        base = name
        while name in used:
            n += 1
            name = f"{base} #{n}"
        used.add(name)
        p["name"] = name
        proxies.append(p)
        rows.append({
            "name": name, "source": r["source"], "type": p.get("type"),
            "server": p.get("server"), "port": p.get("port"),
            "country": r.get("country"), "country_code": cc, "isp": r.get("isp"),
            "exit_ip": r.get("exit_ip"), "latency_ms": r.get("ip_rtt_ms"),
            "https_ms": r.get("https_ms"), "speed_kbps": r.get("speed_kbps"),
            "validity_type": r.get("validity_type"), "issued_at": r.get("issued_at"),
            "expires_at": r.get("expires_at"), "validity_label": label,
            "tested_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        })

    exp_names = [row["name"] for row in rows if row["expires_at"]]
    static_names = [row["name"] for row in rows if not row["expires_at"]]
    pub_names = [row["name"] for row in rows if row["validity_type"] == "public"]
    rev_names = [row["name"] for row in rows if row["validity_type"] != "public"]
    country_map = defaultdict(list)
    countries = []
    for row in rows:
        if row["country_code"] not in countries:
            countries.append(row["country_code"])
        country_map[row["country_code"]].append(row["name"])
    all_names = [p["name"] for p in proxies]

    def ut(name, names):
        return {"name": name, "type": "url-test", "url": GSTATIC, "interval": 300,
                "tolerance": 50, "lazy": False, "proxies": names}

    groups = [ut("AUTO", all_names)]
    if exp_names:
        groups.append(ut("AUTO-EXP", exp_names))
    if static_names:
        groups.append(ut("AUTO-STATIC", static_names))
    for cc in countries:
        groups.append(ut(f"AUTO-{cc}", country_map[cc]))
    if pub_names:
        groups.append(ut("AUTO-PUB", pub_names))
    if rev_names:
        groups.append(ut("AUTO-REV", rev_names))
    if exp_names:
        groups.append({"name": "EXPIRING", "type": "select", "proxies": exp_names})
    if static_names:
        groups.append({"name": "STATIC", "type": "select", "proxies": static_names})
    # PROXY 只能引用真实存在的组（AUTO-EXP / AUTO-PUB 等在无对应节点时不会生成）
    existing = {g["name"] for g in groups}
    preferred = ["AUTO", "AUTO-PUB", "AUTO-REV", "AUTO-EXP", "AUTO-STATIC"]
    preferred += [f"AUTO-{cc}" for cc in countries]
    preferred += ["EXPIRING", "STATIC"]
    main_members = [m for m in dict.fromkeys(preferred) if m in existing]
    main_members += all_names
    groups.append({"name": "PROXY", "type": "select", "proxies": main_members})

    doc = {"log-level": "warning", "mode": "rule", "proxies": proxies,
           "proxy-groups": groups, "rules": ["MATCH,PROXY"]}
    os.makedirs(REPORTS, exist_ok=True)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
        w.writeheader()
        w.writerows(rows)

    summary = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "candidates": cand_total,
        "final": len(proxies),
        "filtered": dict(reasons),
        "by_source": dict(Counter(r["source"] for r in final)),
        "by_country": dict(Counter(r["country_code"] for r in final)),
        "by_type": dict(Counter(r["proxy"].get("type") for r in final)),
        "expiring": len(exp_names),
        "static": len(static_names),
        "public": len(pub_names),
        "reverse": len(rev_names),
        "groups": [g["name"] for g in groups],
        "token_expiry_min": (min(r["expires_at"] for r in final if r.get("expires_at"))
                             if exp_names else None),
        "token_expiry_max": (max(r["expires_at"] for r in final if r.get("expires_at"))
                             if exp_names else None),
        "next_refresh_due": (datetime.now(CST) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M %Z"),
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def validate_yaml(path):
    ok, out = run([MIHOMO, "-t", "-f", path], timeout=90, label="mihomo -t")
    return ok


def upload_gist(yaml_path=None):
    yaml_path = yaml_path or YAML_OUT
    try:
        r = subprocess.run(["gh", "gist", "edit", GIST_ID, "--filename", GIST_FILE, yaml_path,
                            "--desc", f"Clash auto-refresh: {datetime.now(CST).strftime('%Y-%m-%d %H:%M')} CST"],
                           capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            log(f"! gist upload failed: {(r.stderr or r.stdout).strip()[:400]}")
            return False
        log("gist updated")
        return True
    except Exception as e:
        log(f"! gist upload exc: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["fast", "full"], default="fast")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--no-harvest", action="store_true")
    ap.add_argument("--no-public", action="store_true", help="跳过公开订阅源（只跑逆向源）")
    ap.add_argument("--public-only", action="store_true", help="只跑公开源，不碰逆向源")
    ap.add_argument("--with-supervpn", action="store_true",
                    help="fast 模式也跑 SuperVPN 收割（节点池当前已基本失效，默认跳过）")
    ap.add_argument("--max-rtt", type=int, default=3000)
    ap.add_argument("--min-speed", type=float, default=25.0)
    ap.add_argument("--max-nodes", type=int, default=150)
    ap.add_argument("--out-dir", default=None,
                    help="订阅输出目录（服务器部署时指向 HTTP 服务的公开目录）")
    ap.add_argument("--no-gist", action="store_true", help="不更新 Gist（服务器自建订阅时用）")
    args = ap.parse_args()

    t_start = time.time()
    log("=" * 70)
    log(f"auto_refresh start mode={args.mode} no_upload={args.no_upload} no_harvest={args.no_harvest} "
        f"public={not args.no_public}")

    errors = []
    if not args.no_harvest:
        if not args.public_only:
            # SuperVPN 的节点池目前基本全灭（2026-11-21 实测 83 IP 仅 3 个可达），
            # 收割+测活要花 ~95s 却产出接近 0，因此 fast 模式默认跳过，只在 full 模式跑。
            run_supervpn = args.mode == "full" or args.with_supervpn
            if run_supervpn:
                if not harvest_supervpn():
                    errors.append("supervpn")
                    log("! SuperVPN chain failed (token nodes may be stale; static nodes still used)")
            else:
                log("· SuperVPN skipped in fast mode (node pool dead); use --with-supervpn or --mode full")
            if args.mode == "full":
                for name, fn in (("fastvpn", harvest_fastvpn), ("tunneldoom", harvest_tunneldoom),
                                 ("capvpn", harvest_capvpn), ("zeroping", harvest_zeroping)):
                    if not fn():
                        errors.append(name)
                        log(f"! {name} harvest failed, keeping cached files if any")
        if not args.no_public:
            if not harvest_public():
                errors.append("public")
                log("! public source fetch failed, will reuse previous public_tcp_ok.json if present")

    cands = collect_candidates(include_static=not args.public_only,
                               include_public=not args.no_public)
    if not cands:
        log("! no candidates at all")
        return 2

    results = evaluate(cands)
    if not results:
        log("! evaluation produced nothing")
        return 2

    final, reasons = finalize(results, max_rtt=args.max_rtt, min_speed=args.min_speed, max_nodes=args.max_nodes)
    if not final:
        log("! all candidates filtered out — keeping previous subscription untouched")
        return 2

    summary = write_outputs(final, reasons, len(cands), out_dir=args.out_dir)
    log("summary: " + json.dumps({k: summary[k] for k in ("candidates", "final", "expiring", "static")}, ensure_ascii=False))

    pub_yaml = os.path.join(args.out_dir or REPORTS, "valid_nodes_clash.yaml")
    if not validate_yaml(pub_yaml):
        log("! generated yaml failed mihomo -t; not uploading")
        return 1

    uploaded = False
    if not args.no_upload:
        if args.out_dir:
            # 服务器部署：订阅目录就是 HTTP 根，Gist 只是可选备份
            if not args.no_gist:
                uploaded = upload_gist(pub_yaml)
        else:
            uploaded = upload_gist(pub_yaml)
        if not uploaded and args.no_gist:
            uploaded = True   # 明确禁用 gist 时不算错误
        elif not uploaded:
            errors.append("gist")

    state = {
        "last_run": datetime.now(CST).isoformat(),
        "mode": args.mode,
        "final": summary["final"],
        "expiring": summary["expiring"],
        "static": summary["static"],
        "next_token_expiry": summary.get("token_expiry_min"),
        "uploaded": uploaded,
        "errors": errors,
        "duration_s": int(time.time() - t_start),
    }
    with open(STATE_OUT, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"done in {state['duration_s']}s, errors={errors}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
