#!/usr/bin/env python3
"""
claude_usage_audit.py - reconstruct Claude Code usage per 5-hour limit window
from the local transcripts (~/.claude/projects/**/*.jsonl) and price every
request at public API rates, so windows can be compared on one scale.

Why API-equivalent dollars rather than raw tokens: a cache write of 1 token on
Opus 5.5 costs 40x a cache read of 1 token, so "tokens" alone (as in /stats)
hide exactly the requests that drain a window. Anthropic does not publish how
plan limits weight token types; the dollar scale is the best public proxy, and
the per-type token columns are printed too.

Reads local files only, sends nothing anywhere. Python 3.9+, no dependencies.

  python3 claude_usage_audit.py --tz Europe/Moscow                # windows, last 3 days
  python3 claude_usage_audit.py --tz Europe/Moscow --detail last  # every request of the latest window
  python3 claude_usage_audit.py --tz Europe/Moscow --window-end "2026-09-26 07:50" --detail last
  python3 claude_usage_audit.py --tz Europe/Moscow --csv audit.csv   # per-request CSV for a ticket

Limits of the method: only Claude Code transcripts on this machine are seen.
Usage from claude.ai / desktop chat, other computers, or cloud sessions draws
from the same plan limit but is not in these files.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

WINDOW = dt.timedelta(hours=5)

# $ per million tokens: input, output, cache write 5m, cache write 1h, cache read.
# Source: Anthropic API pricing, September 2026. Longest prefix wins.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-5": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4": (15.00, 75.00, 18.75, 30.00, 1.50),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-mythos-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-mythos-5": (10.00, 50.00, 12.50, 20.00, 1.00),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-sonnet-4": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-3-7-sonnet": (3.00, 15.00, 3.75, 6.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 2.00, 0.10),
    "claude-3-5-haiku": (0.80, 4.00, 1.00, 1.60, 0.08),
}
FAST_MULTIPLIER = 2.0  # fast mode on Opus 5 / 5.5 is 2x standard
_PREFIXES = sorted(PRICES, key=len, reverse=True)
_unpriced = Counter()

LIMIT_RE = re.compile(r"((hit|reached) your .{0,40}limit|limit reached|usage limit)", re.I)


def price_for(model):
    m = (model or "").lower()
    i = m.find("claude-")
    m = m[i:] if i >= 0 else m
    for p in _PREFIXES:
        if m.startswith(p):
            return PRICES[p]
    _unpriced[model] += 1
    return None


def cost_of(model, inp, out, cw5, cw1h, cr, fast=False):
    p = price_for(model)
    if p is None:
        return 0.0
    c = (inp * p[0] + out * p[1] + cw5 * p[2] + cw1h * p[3] + cr * p[4]) / 1e6
    return c * (FAST_MULTIPLIER if fast else 1.0)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def default_roots():
    roots = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots += [Path(p.strip()).expanduser() / "projects" for p in env.split(",") if p.strip()]
    roots += [Path("~/.config/claude/projects").expanduser(), Path("~/.claude/projects").expanduser()]
    seen, out = set(), []
    for r in roots:
        if r.is_dir() and r.resolve() not in seen:
            seen.add(r.resolve())
            out.append(r)
    return out


def text_of(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def usage_parts(u):
    """Split one usage object into (input, output, cw5m, cw1h, cache_read)."""
    cc = u.get("cache_creation") or {}
    cw_total = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
    cw5 = cc.get("ephemeral_5m_input_tokens")
    if cw5 is None:
        cw5 = max(cw_total - cw1h, 0)
    return (u.get("input_tokens") or 0, u.get("output_tokens") or 0, cw5, cw1h,
            u.get("cache_read_input_tokens") or 0)


def load(roots, since):
    """Return (requests, limit_events). Requests are deduplicated across files
    (Claude Code writes one line per content block, and forks/resumes copy history)."""
    reqs = {}
    limits = {}
    since_epoch = since.timestamp() - 86400
    for root in roots:
        for f in root.rglob("*.jsonl"):
            try:
                if f.stat().st_mtime < since_epoch:
                    continue
                fh = f.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"usage"' not in line and "imit" not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ts = e.get("timestamp")
                    if not ts or e.get("type") not in ("assistant", "system"):
                        continue
                    try:
                        t = parse_ts(ts)
                    except ValueError:
                        continue
                    if t < since:
                        continue
                    msg = e.get("message") or {}
                    model = msg.get("model") if isinstance(msg, dict) else None
                    synthetic = e.get("isApiErrorMessage") or model == "<synthetic>" or e.get("type") == "system"
                    if synthetic:
                        txt = text_of(msg) or str(e.get("content") or "")
                        if LIMIT_RE.search(txt):
                            limits.setdefault(t.replace(second=0, microsecond=0), (t, txt.strip()[:120]))
                        continue
                    u = msg.get("usage") if isinstance(msg, dict) else None
                    if not u:
                        continue
                    key = (msg.get("id"), e.get("requestId")) if (msg.get("id") or e.get("requestId")) else e.get("uuid")
                    iters = u.get("iterations") or []
                    attempts = []
                    if len(iters) > 1:
                        for it in iters:
                            attempts.append((it.get("model") or model, usage_parts(it)))
                    else:
                        attempts.append((model, usage_parts(u)))
                    sd = msg.get("stop_details") or {}
                    rec = {
                        "t": t,
                        "session": e.get("sessionId") or f.stem,
                        "sidechain": bool(e.get("isSidechain")),
                        "model": model or "?",
                        "fast": u.get("speed") == "fast",
                        "attempts": attempts,
                        "thinking": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                        "stop": msg.get("stop_reason"),
                        "refusal_cat": sd.get("category") if isinstance(sd, dict) else None,
                        "project": e.get("cwd") or f.parent.name,
                    }
                    old = reqs.get(key)
                    if old is None:
                        reqs[key] = rec
                    else:  # streaming duplicates: keep the fullest usage, earliest time
                        if sum(map(sum, (a[1] for a in rec["attempts"]))) > sum(map(sum, (a[1] for a in old["attempts"]))):
                            old["attempts"] = rec["attempts"]
                        old["thinking"] = max(old["thinking"], rec["thinking"])
                        old["stop"] = rec["stop"] or old["stop"]
                        old["refusal_cat"] = rec["refusal_cat"] or old["refusal_cat"]
                        old["t"] = min(old["t"], rec["t"])
    out = []
    for r in reqs.values():
        inp = out_t = cw5 = cw1h = cr = 0
        cost = 0.0
        for m, (a, b, c, d, e_) in r["attempts"]:
            inp += a; out_t += b; cw5 += c; cw1h += d; cr += e_
            cost += cost_of(m, a, b, c, d, e_, r["fast"])
        r.update(inp=inp, out=out_t, cw5=cw5, cw1h=cw1h, cr=cr, cost=cost,
                 ctx=max(a + c + d + e_ for _, (a, b, c, d, e_) in r["attempts"]),
                 multi=len(r["attempts"]) > 1)
        out.append(r)
    out.sort(key=lambda r: r["t"])
    return out, sorted(limits.values())


def floor_to(t, minutes):
    if minutes <= 0:
        return t
    t = t.replace(second=0, microsecond=0)
    return t - dt.timedelta(minutes=t.minute % minutes)


def build_windows(reqs, floor_minutes, forced_end=None):
    """Greedy reconstruction of 5-hour windows: a window opens at the first request
    after the previous one closed (floored to 10 min, which matches the reset times
    the server reports) and lasts 5 hours. --window-end pins one window exactly."""
    wins = []
    forced = (forced_end - WINDOW, forced_end) if forced_end else None
    for r in reqs:
        if forced and forced[0] <= r["t"] < forced[1]:
            if not wins or wins[-1]["start"] != forced[0]:
                wins.append({"start": forced[0], "end": forced[1], "reqs": []})
            wins[-1]["reqs"].append(r)
            continue
        if not wins or r["t"] >= wins[-1]["end"]:
            start = floor_to(r["t"], floor_minutes)
            if forced and start < forced[1] and start + WINDOW > forced[0] and r["t"] < forced[0]:
                end = forced[0]  # window truncated by the pinned one
            else:
                end = start + WINDOW
            wins.append({"start": start, "end": end, "reqs": []})
        wins[-1]["reqs"].append(r)
    return wins


def summarize(w, limits, big_miss):
    rs = w["reqs"]
    s = {k: sum(r[k] for r in rs) for k in ("inp", "out", "cw5", "cw1h", "cr", "cost", "thinking")}
    s["n"] = len(rs)
    s["sessions"] = len({r["session"] for r in rs})
    s["sidechain"] = sum(r["sidechain"] for r in rs)
    s["models"] = Counter(r["model"] for r in rs)
    s["max_ctx"] = max((r["ctx"] for r in rs), default=0)
    misses = [r for r in rs if r["cw5"] + r["cw1h"] >= big_miss]
    s["misses"] = len(misses)
    s["miss_cost"] = sum(r["cost"] for r in misses)
    s["refusals"] = Counter(r["refusal_cat"] or "?" for r in rs if r["stop"] == "refusal")
    s["multi"] = sum(r["multi"] for r in rs)
    s["fast"] = sum(r["fast"] for r in rs)
    hit = next((t for t, _ in limits if w["start"] <= t < w["end"]), None)
    s["hit"] = hit
    if hit:
        s["to_hit_min"] = (hit - w["start"]).total_seconds() / 60
        s["cost_to_hit"] = sum(r["cost"] for r in rs if r["t"] <= hit)
    # usage until the limit (or the whole window), on several scales at once,
    # so the comparison does not depend on how the plan weights token types
    upto = [r for r in rs if not hit or r["t"] <= hit]
    s["m"] = {
        "calls": len(upto),
        "output tokens": sum(r["out"] for r in upto),
        "output + cache write": sum(r["out"] + r["cw5"] + r["cw1h"] for r in upto),
        "all tokens incl. cache read": sum(r["inp"] + r["out"] + r["cw5"] + r["cw1h"] + r["cr"] for r in upto),
        "API-equivalent $": sum(r["cost"] for r in upto),
    }
    return s


def fmt_tok(n):
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=3, help="how far back to read (default 3)")
    ap.add_argument("--tz", help="IANA time zone for display, e.g. Europe/Moscow (default: system)")
    ap.add_argument("--path", action="append", help="projects dir to scan (repeatable)")
    ap.add_argument("--window-end", help='pin one window by its reset time from the UI, e.g. "2026-09-26 07:50" (in --tz)')
    ap.add_argument("--floor-minutes", type=int, default=10, help="window start rounding (default 10)")
    ap.add_argument("--big-miss", type=int, default=50_000, help="cache write size counted as a cold-cache request (default 50k)")
    ap.add_argument("--detail", help='"last" or a window number from the table: list every request in it')
    ap.add_argument("--csv", help="write every request to this CSV file")
    args = ap.parse_args()

    tz = None
    if args.tz:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(args.tz)
        except Exception as ex:  # Windows without tzdata, typo, ...
            print(f"! time zone {args.tz!r} unavailable ({ex}); using system time", file=sys.stderr)

    def loc(t):
        return t.astimezone(tz) if tz else t.astimezone()

    def hm(t):
        return loc(t).strftime("%m-%d %H:%M")

    roots = [Path(p).expanduser() for p in args.path] if args.path else default_roots()
    if not roots:
        sys.exit("No Claude Code projects directory found; pass --path.")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    reqs, limits = load(roots, since)
    if not reqs:
        sys.exit(f"No requests with usage found in {', '.join(map(str, roots))} for the last {args.days} days.")

    forced_end = None
    if args.window_end:
        naive = dt.datetime.fromisoformat(args.window_end)
        forced_end = (naive.replace(tzinfo=tz) if tz else naive.astimezone()).astimezone(dt.timezone.utc)

    wins = build_windows(reqs, args.floor_minutes, forced_end)
    sums = [summarize(w, limits, args.big_miss) for w in wins]

    print(f"Scanned: {', '.join(map(str, roots))}")
    print(f"Requests: {len(reqs)} (deduplicated), {hm(reqs[0]['t'])} .. {hm(reqs[-1]['t'])}"
          f"   times in {args.tz or 'system time zone'}")
    print("Cost = API-equivalent $ at list prices (proxy for plan-limit consumption; the plan is not billed this).")
    print("cw = cache write (1h+5m), cr = cache read, miss = requests writing >= "
          f"{fmt_tok(args.big_miss)} tokens to cache (cold cache: fork, resume, model switch, >1h pause).\n")

    hdr = (f"{'#':>2}  {'window':<23} {'req':>4} {'ses':>3} {'in':>6} {'cw':>7} {'cr':>8} {'out':>6} "
           f"{'maxctx':>7} {'miss':>4} {'$miss':>7} {'$total':>7}  limit hit")
    print(hdr)
    print("-" * len(hdr))
    for i, (w, s) in enumerate(zip(wins, sums), 1):
        hit = (f"after {s['to_hit_min']:.0f} min, ${s['cost_to_hit']:.2f}" if s["hit"] else "-")
        flags = []
        if s["refusals"]:
            flags.append("refusals " + ",".join(f"{k}x{v}" for k, v in s["refusals"].items()))
        if s["multi"]:
            flags.append(f"fallback-attempts {s['multi']}")
        if s["fast"]:
            flags.append(f"fast {s['fast']}")
        span = f"{hm(w['start'])}-{loc(w['end']).strftime('%H:%M')}"
        print(f"{i:>2}  {span:<23} {s['n']:>4} {s['sessions']:>3} "
              f"{fmt_tok(s['inp']):>6} {fmt_tok(s['cw5'] + s['cw1h']):>7} {fmt_tok(s['cr']):>8} {fmt_tok(s['out']):>6} "
              f"{fmt_tok(s['max_ctx']):>7} {s['misses']:>4} {s['miss_cost']:>7.2f} {s['cost']:>7.2f}  {hit}"
              + (f"  [{'; '.join(flags)}]" if flags else ""))

    hits = [s for s in sums if s["hit"]]
    if hits:
        print("\nWindows that reached the limit (what 100% cost in API-equivalent $):")
        for i, s in ((sums.index(s) + 1, s) for s in hits):
            print(f"  #{i}: {s['to_hit_min']:.0f} min after window start, ${s['cost_to_hit']:.2f}, "
                  f"{s['misses']} cold-cache requests, max context {fmt_tok(s['max_ctx'])}")
    else:
        print("\nNo 'limit reached' messages found in the transcripts for this period.")

    if len(hits) >= 2:
        last, ref = hits[-1], hits[:-1]
        label = "earlier windows that reached 100%"
    else:
        last, ref = sums[-1], [s for s in sums[:-1] if s["n"] >= 10]
        label = "earlier windows with >= 10 calls (not necessarily at 100%)"
    if ref:
        print(f"\nLatest window #{sums.index(last) + 1} (up to the limit) vs median of {len(ref)} {label}:")
        for k, v in last["m"].items():
            med = statistics.median(s["m"][k] for s in ref)
            ratio = f"x{v / med:.2f}" if med else "n/a"
            show = (lambda x: f"${x:.2f}") if k.endswith("$") else (lambda x: f"{x:,.0f}")
            print(f"  {k:<28} {show(v):>14} vs {show(med):>14}   {ratio}")
        print("  All ratios far below x1 = the limit filled on far less usage of every kind than before.\n"
              "  Low calls/tokens but $ near x1 = the few calls were unusually expensive (cold cache, fallback).")

    days = defaultdict(lambda: Counter())
    for r in reqs:
        d = days[loc(r["t"]).date()]
        d["calls"] += 1
        for k in ("inp", "cw5", "cw1h", "cr", "out", "cost"):
            d[k] += r[k]
    print(f"\nPer day ({args.tz or 'system time zone'}), to match against /stats:")
    print(f"  {'day':<10} {'calls':>6} {'in':>7} {'cache write':>12} {'cache read':>11} {'out':>7} {'out+cw':>8} {'$':>8}")
    for day in sorted(days):
        d = days[day]
        print(f"  {day.isoformat():<10} {d['calls']:>6} {fmt_tok(d['inp']):>7} {fmt_tok(d['cw5'] + d['cw1h']):>12} "
              f"{fmt_tok(d['cr']):>11} {fmt_tok(d['out']):>7} {fmt_tok(d['out'] + d['cw5'] + d['cw1h']):>8} {d['cost']:>8.2f}")

    if _unpriced:
        print(f"\n! Unpriced models counted as $0: {dict(_unpriced)}")

    target = None
    if args.detail:
        target = len(wins) if args.detail == "last" else int(args.detail)
    if target:
        w, s = wins[target - 1], sums[target - 1]
        print(f"\nWindow #{target} {hm(w['start'])}-{loc(w['end']).strftime('%H:%M')}: every request")
        h2 = (f"{'time':<14} {'session':<9} {'model':<18} {'in':>6} {'cw':>7} {'cr':>8} {'out':>6} "
              f"{'think':>6} {'$':>6}  stop")
        print(h2)
        print("-" * len(h2))
        for r in w["reqs"]:
            stop = r["stop"] or ""
            if r["stop"] == "refusal":
                stop += f" ({r['refusal_cat'] or '?'})"
            if r["multi"]:
                stop += " +fallback"
            if r["sidechain"]:
                stop += " [subagent]"
            print(f"{loc(r['t']).strftime('%m-%d %H:%M:%S'):<14} {r['session'][:8]:<9} {r['model'][:18]:<18} "
                  f"{fmt_tok(r['inp']):>6} {fmt_tok(r['cw5'] + r['cw1h']):>7} {fmt_tok(r['cr']):>8} "
                  f"{fmt_tok(r['out']):>6} {fmt_tok(r['thinking']):>6} {r['cost']:>6.2f}  {stop}")
        for t, txt in limits:
            if w["start"] <= t < w["end"]:
                print(f"{loc(t).strftime('%m-%d %H:%M:%S'):<14} LIMIT: {txt}")
        cw_cost = sum(cost_of(r["model"], 0, 0, r["cw5"], r["cw1h"], 0, r["fast"]) for r in w["reqs"])
        cr_cost = sum(cost_of(r["model"], 0, 0, 0, 0, r["cr"], r["fast"]) for r in w["reqs"])
        out_cost = sum(cost_of(r["model"], 0, r["out"], 0, 0, 0, r["fast"]) for r in w["reqs"])
        tot = s["cost"] or 1
        print(f"\nCost split: cache writes {cw_cost / tot:.0%}, cache reads {cr_cost / tot:.0%}, "
              f"output {out_cost / tot:.0%} (thinking {fmt_tok(s['thinking'])} of {fmt_tok(s['out'])} output tokens)")

        print("\nPaste-ready summary:")
        span = (w["reqs"][-1]["t"] - w["reqs"][0]["t"]).total_seconds() / 60
        line = (f"Window {hm(w['start'])}-{loc(w['end']).strftime('%H:%M')} ({args.tz or 'local time'}): "
                f"{s['n']} API requests in {span:.0f} min across {s['sessions']} Claude Code session(s), "
                f"model(s) {', '.join(s['models'])}. Tokens: input {s['inp']:,}, cache write {s['cw5'] + s['cw1h']:,}, "
                f"cache read {s['cr']:,}, output {s['out']:,} (largest context {s['max_ctx']:,}). "
                f"API-list-price equivalent ${s['cost']:.2f}.")
        if s["hit"]:
            line += f" Limit reached {s['to_hit_min']:.0f} min after the window opened."
        if s["refusals"]:
            line += f" Refusals in window: {sum(s['refusals'].values())} ({', '.join(s['refusals'])})."
        if s["misses"]:
            line += f" Requests with a cold cache (>= {fmt_tok(args.big_miss)} tokens re-cached): {s['misses']}, ${s['miss_cost']:.2f}."
        print(line)

    if args.csv:
        idx = {id(r): i for i, w in enumerate(wins, 1) for r in w["reqs"]}
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(["time", "window", "session", "subagent", "model", "fast", "input", "cache_write_5m",
                         "cache_write_1h", "cache_read", "output", "thinking", "context", "usd_api_equiv",
                         "stop_reason", "refusal_category", "attempts"])
            for r in reqs:
                wr.writerow([loc(r["t"]).isoformat(timespec="seconds"), idx.get(id(r)), r["session"], r["sidechain"],
                             r["model"], r["fast"], r["inp"], r["cw5"], r["cw1h"], r["cr"], r["out"], r["thinking"],
                             r["ctx"], f"{r['cost']:.4f}", r["stop"], r["refusal_cat"], len(r["attempts"])])
        print(f"\nCSV written: {args.csv}")


if __name__ == "__main__":
    main()
