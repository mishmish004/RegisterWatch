"""Open-loop load at a fixed rate (plan.md T9.4.a, P10.6). Stdlib and httpx.

  python scripts/verify/load.py --rps 50 --duration 120 \\
      --path '/v1/registers/gb_ukgc/tables/licences/rows?limit=1000'

  python scripts/verify/load.py --rps 100 --duration 120 --clients 40 \\
      --mix 'rows=70:/v1/registers/gb_ukgc/tables/licences/rows' \\
      --mix 'domains=20:/v1/domains/bet365.com' --mix 'search=10:/v1/search?q=betway'

Requests fall due on a fixed schedule whatever the server does: a slow answer
never delays the next one, and each latency counts from when its request was
due, not from when a connection was free to send it (no coordinated omission).
At most `--concurrency` are in flight, on as many keep-alive connections; one
due beyond that waits for a free one, and its wait is in its latency. The cap
keeps an overloaded server from being buried under ever more connections (and
the client under ever more tasks).

`--mix NAME=WEIGHT:PATH` (repeatable) spreads the requests over several paths
by weight, interleaved evenly rather than at random, so every run sends the
same sequence; entries sharing a NAME are one class in the report. `--clients N`
sends request i as client i mod N, named by `X-Forwarded-For: 198.18.x.y` (the
benchmarking range, RFC 2544): the server believes it only from a proxy it
trusts (FORWARDED_ALLOW_IPS), and then counts each client's rate limits apart.
The result is one JSON line:

  {"rps": 50, "duration_s": 120, "elapsed_s": 120.1, "sent": 6000, "statuses": {"200": 6000},
   "errors": {}, "p50_ms": .., "p95_ms": .., "p99_ms": .., "max_ms": .., "late_starts": 0,
   "concurrency": 64, "clients": 1, "classes": {"rows": {"sent": .., "statuses": .., "p50_ms": ..}}}

`elapsed_s` runs to the last answer: well past `duration_s`, the server fell
behind. `errors` counts requests with no HTTP answer, by exception. `late_starts` counts
requests sent more than 100 ms after they were due: every connection was busy,
or the client itself fell behind. `classes` is there with `--mix`. Exits 1 when
a request got no answer or any answer was a 5xx.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from collections import Counter, defaultdict

import httpx

GOLDEN = 0.6180339887498949


def percentile(sorted_ms: list[float], p: float) -> float | None:
    if not sorted_ms:
        return None
    k = min(len(sorted_ms) - 1, max(0, round(p / 100 * len(sorted_ms) + 0.5) - 1))
    return round(sorted_ms[k], 1)


def parse_mix(entries: list[str]) -> list[tuple[str, float, str]]:
    """`NAME=WEIGHT:PATH` -> (name, weight, path)."""
    out = []
    for e in entries:
        name, _, rest = e.partition("=")
        weight, _, path = rest.partition(":")
        if not (name and path.startswith("/")):
            raise SystemExit(f"--mix {e!r}: expected NAME=WEIGHT:/path")
        out.append((name, float(weight), path))
    return out


def schedule(mix: list[tuple[str, float, str]], i: int) -> tuple[str, str]:
    """Request i's (class, path): a low-discrepancy walk over the weights, so
    every stretch of the run carries each entry in proportion."""
    total = sum(w for _, w, _ in mix)
    x = (i * GOLDEN) % 1 * total
    for name, weight, path in mix:
        if x < weight:
            return name, path
        x -= weight
    return mix[-1][0], mix[-1][2]


def client_address(k: int) -> str:
    return f"198.18.{k // 256 % 256}.{k % 256}"


def summary(latencies: list[float], statuses: Counter[str], errors: Counter[str]) -> dict:
    latencies.sort()
    return {"sent": len(latencies), "statuses": dict(sorted(statuses.items())), "errors": dict(errors),
            "p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99), "max_ms": round(latencies[-1], 1) if latencies else None}


async def run(base: str, mix: list[tuple[str, float, str]], rps: float, duration: float, token: str | None,
              timeout: float, concurrency: int, clients: int, verify: bool | ssl.SSLContext = True) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    slots = asyncio.Semaphore(concurrency)
    statuses: dict[str, Counter[str]] = defaultdict(Counter)
    errors: dict[str, Counter[str]] = defaultdict(Counter)
    latencies: dict[str, list[float]] = defaultdict(list)
    late = 0

    async with httpx.AsyncClient(base_url=base, headers=headers, limits=limits, verify=verify,
                                 timeout=httpx.Timeout(timeout, pool=None)) as client:

        async def one(i: int, due: float) -> None:
            nonlocal late
            name, path = schedule(mix, i)
            extra = {"X-Forwarded-For": client_address(i % clients)} if clients > 1 else None
            async with slots:
                if time.perf_counter() - due > 0.1:
                    late += 1
                try:
                    response = await client.get(path, headers=extra)
                    await response.aread()
                    statuses[name][str(response.status_code)] += 1
                except httpx.HTTPError as exc:
                    errors[name][type(exc).__name__] += 1
            latencies[name].append((time.perf_counter() - due) * 1000)

        start = time.perf_counter()
        total = int(rps * duration)
        tasks = []
        for i in range(total):
            due = start + i / rps
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(i, due)))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    everything = summary([x for v in latencies.values() for x in v], sum(statuses.values(), Counter()),
                         sum(errors.values(), Counter()))
    out = {"rps": rps, "duration_s": duration, "elapsed_s": round(elapsed, 1), **everything,
           "late_starts": late, "concurrency": concurrency, "clients": clients}
    if len(mix) > 1:
        out["classes"] = {name: summary(latencies[name], statuses[name], errors[name])
                          for name in dict.fromkeys(n for n, _, _ in mix)}
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="load.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default=os.environ.get("RW_BASE", "http://127.0.0.1:8000"),
                   help="default $RW_BASE, else http://127.0.0.1:8000")
    p.add_argument("--path", default="/livez", help="every request's path, when there is no --mix")
    p.add_argument("--mix", action="append", default=[], metavar="NAME=WEIGHT:PATH",
                   help="weighted paths, repeatable (instead of --path)")
    p.add_argument("--rps", type=float, default=10)
    p.add_argument("--duration", type=float, default=10, help="seconds")
    p.add_argument("--token", default=os.environ.get("RW_TOKEN"), help="bearer token (default $RW_TOKEN)")
    p.add_argument("--timeout", type=float, default=30, help="per request, seconds")
    p.add_argument("--concurrency", type=int, default=64, help="requests in flight at most (default 64)")
    p.add_argument("--clients", type=int, default=1,
                   help="distinct X-Forwarded-For client addresses, round robin (default 1: none sent)")
    p.add_argument("--cacert", help="trust this CA bundle instead of the default store (a test edge's own CA)")
    args = p.parse_args(argv)
    mix = parse_mix(args.mix) if args.mix else [("all", 1.0, args.path)]
    result = asyncio.run(run(args.base, mix, args.rps, args.duration, args.token, args.timeout,
                             args.concurrency, max(1, args.clients),
                             ssl.create_default_context(cafile=args.cacert) if args.cacert else True))
    print(json.dumps(result))
    failed = sum(result["errors"].values()) + sum(n for s, n in result["statuses"].items() if s.startswith("5"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
