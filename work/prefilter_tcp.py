#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开源 TCP 预筛：先做廉价 socket 连接，把完整测活的量压到可接受范围。"""
from __future__ import annotations
import argparse, json, os, random, socket, sys, time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "work", "run_re", "public")


def tcp_ok(host, port, timeout):
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(OUTDIR, "public_proxies.json"))
    ap.add_argument("--out", default=os.path.join(OUTDIR, "public_tcp_ok.json"))
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--max-keep", type=int, default=400)
    ap.add_argument("--sample", type=int, default=0, help=">0 时随机抽样这么多条再筛")
    args = ap.parse_args()

    proxies = json.load(open(args.inp, encoding="utf-8"))
    if args.sample and args.sample < len(proxies):
        random.seed(42)
        proxies = random.sample(proxies, args.sample)
    print(f"input {len(proxies)}")

    def job(p):
        return p, tcp_ok(p["server"], p["port"], args.timeout)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(job, proxies))
    ok = [p for p, good in results if good]
    elapsed = int(time.time() - t0)
    print(f"tcp reachable {len(ok)}/{len(proxies)} in {elapsed}s")

    if len(ok) > args.max_keep:
        random.seed(7)
        ok = random.sample(ok, args.max_keep)
        print(f"trimmed to {args.max_keep}")
    json.dump(ok, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from collections import Counter
    print(dict(Counter(p["type"] for p in ok)))
    print("->", args.out)


if __name__ == "__main__":
    main()
