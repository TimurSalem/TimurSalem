#!/usr/bin/env python3
"""Print the event sequence (user/assistant/attachment/system) of two Claude Code
sessions in two time windows, without message content: short texts only,
longer ones as [text N chars]. Used to compare the working and the broken
cache window of 2026-09-26 (times printed in Moscow time)."""
import json, glob, os, datetime as dt
SESSIONS = ("4584a1d4", "644e581d")
WINDOWS = (("2026-09-25T21:55", "2026-09-25T22:15"), ("2026-09-25T23:45", "2026-09-26T00:02"))
ROOT = os.path.expanduser("~/.claude/projects")
def short(s):
    s = " ".join(str(s).split())
    return repr(s) if len(s) <= 40 else f"[text {len(s)} chars]"
rows = []
for f in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
    if not os.path.basename(f).startswith(SESSIONS):
        continue
    for line in open(f, errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("timestamp", "")
        if not any(a <= t < b for a, b in WINDOWS):
            continue
        typ, m = e.get("type"), e.get("message") or {}
        info = ""
        if typ == "user":
            c = m.get("content")
            if isinstance(c, str):
                info = short(c)
            elif isinstance(c, list):
                info = " ".join(b.get("type", "?") if b.get("type") != "text" else short(b.get("text", "")) for b in c if isinstance(b, dict))
            if e.get("isMeta"): info += " [meta]"
            if e.get("isCompactSummary"): info += " [compact-summary]"
        elif typ == "assistant":
            blocks = []
            for b in m.get("content") or []:
                if not isinstance(b, dict): continue
                k = b.get("type", "?")
                if k in ("thinking", "redacted_thinking"):
                    k += "(signed)" if b.get("signature") else "(UNSIGNED)"
                elif k == "text":
                    k = "text:" + short(b.get("text", ""))
                blocks.append(k)
            u = m.get("usage") or {}
            info = (f"{m.get('model')} stop={m.get('stop_reason')} cw={u.get('cache_creation_input_tokens', 0)} "
                    f"cr={u.get('cache_read_input_tokens', 0)} req={e.get('requestId')} {' '.join(blocks)}"
                    + f" v{e.get('version')}" + (" [API-ERROR]" if e.get("isApiErrorMessage") else ""))
        elif typ == "system":
            info = f"{e.get('subtype', '')} {short(e.get('content', ''))}"
        elif typ == "attachment":
            a = e.get("attachment")
            info = a.get("type", "?") if isinstance(a, dict) else "?"
        else:
            info = e.get("subtype", "") or ""
        msk = (dt.datetime.fromisoformat(t.replace("Z", "+00:00")) + dt.timedelta(hours=3)).strftime("%H:%M:%S")
        rows.append((t, msk, os.path.basename(f)[:8], typ, info))
for t, msk, sid, typ, info in sorted(set(rows)):
    print(msk, sid, f"{typ:<10}", info)
