"""Open-loop load at a fixed rate (plan.md T9.4.a, P10.6). Stdlib and httpx.

  python scripts/verify/load.py --rps 50 --duration 120 \\
      --path '/v1/registers/gb_ukgc/tables/licences/rows?limit=1000'

Requests fall due on a fixed schedule whatever the server does: a slow answer
never delays the next one, and each latency counts from when its request was
due, not from when a connection was free to send it (no coordinated omission).
At most `--concurrency` are in flight, on as many keep-alive connections; one
due beyond that waits for a free one, and its wait is in its latency. The cap
keeps an overloaded server from being buried under ever more connections (and
the client under ever more tasks). The result is one JSON line:

  {"rps": 50, "duration_s": 120, "elapsed_s": 120.1, "sent": 6000, "statuses": {"200": 6000},
   "errors": {}, "p50_ms": .., "p95_ms": .., "p99_ms": .., "max_ms": .., "late_starts": 0,
   "concurrency": 64}

`elapsed_s` runs to the last answer: well past `duration_s`, the server fell
behind. `errors` counts requests with no HTTP answer, by exception. `late_starts` counts
requests sent more than 100 ms after they were due: every connection was busy,
or the client itself fell behind. Exits 1 when a request got no answer or any
answer was a 5xx.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

import httpx


def percentile(sorted_ms: list[float], p: float) -> float | None:
    if not sorted_ms:
        return None
    k = min(len(sorted_ms) - 1, max(0, round(p / 100 * len(sorted_ms) + 0.5) - 1))
    return round(sorted_ms[k], 1)


async def run(base: str, path: str, rps: float, duration: float, token: str | None, timeout: float,
              concurrency: int) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    slots = asyncio.Semaphore(concurrency)
    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    latencies: list[float] = []
    late = 0

    async with httpx.AsyncClient(base_url=base, headers=headers, limits=limits,
                                 timeout=httpx.Timeout(timeout, pool=None)) as client:

        async def one(due: float) -> None:
            nonlocal late
            async with slots:
                if time.perf_counter() - due > 0.1:
                    late += 1
                try:
                    response = await client.get(path)
                    await response.aread()
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError as exc:
                    errors[type(exc).__name__] += 1
            latencies.append((time.perf_counter() - due) * 1000)

        start = time.perf_counter()
        total = int(rps * duration)
        tasks = []
        for i in range(total):
            due = start + i / rps
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(due)))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    latencies.sort()
    return {"rps": rps, "duration_s": duration, "elapsed_s": round(elapsed, 1), "sent": total, "statuses": dict(sorted(statuses.items())),
            "errors": dict(errors), "p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99), "max_ms": round(latencies[-1], 1) if latencies else None,
            "late_starts": late, "concurrency": concurrency}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="load.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default=os.environ.get("RW_BASE", "http://127.0.0.1:8000"),
                   help="default $RW_BASE, else http://127.0.0.1:8000")
    p.add_argument("--path", default="/livez")
    p.add_argument("--rps", type=float, default=10)
    p.add_argument("--duration", type=float, default=10, help="seconds")
    p.add_argument("--token", default=os.environ.get("RW_TOKEN"), help="bearer token (default $RW_TOKEN)")
    p.add_argument("--timeout", type=float, default=30, help="per request, seconds")
    p.add_argument("--concurrency", type=int, default=64, help="requests in flight at most (default 64)")
    args = p.parse_args(argv)
    result = asyncio.run(run(args.base, args.path, args.rps, args.duration, args.token, args.timeout,
                             args.concurrency))
    print(json.dumps(result))
    failed = sum(result["errors"].values()) + sum(n for s, n in result["statuses"].items() if s.startswith("5"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
