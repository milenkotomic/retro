#!/usr/bin/env python3
"""Collect and render a personal week review (Monday to Sunday).

Usage:
  week.py range    [--week W]   print the week, next week and the data folder
  week.py sessions [--week W]   Claude Code sessions -> sessions.json
  week.py github   [--week W]   commits, PRs, issues in your own repos -> github.json
  week.py ynab     [--week W]   YNAB spending, budget pace, age of money -> ynab.json
  week.py summary  [--week W]   compact text of everything collected, for writing notes
  week.py render   [--week W] [--no-open]   all JSON files -> week-review.html

W is an ISO week (2026-W45) or any date inside the week (2026-11-04). Default:
the last completed week (the Monday to Sunday before today's week).

Data lives in ~/.claude/retro/weekly/<YYYY-Www>/ (RETRO_HOME overrides
~/.claude/retro). Calendar data (calendar.json) and the written notes
(notes.json) come from the agent; their shapes are in the SKILL.md. Config is
~/.claude/retro/week-review.json, created with defaults on first run.

YNAB is read straight from the API so the numbers never pass through the
model: the token comes from YNAB_TOKEN, else from the `ynab` MCP server entry
in ~/.claude.json. Read only. Python 3 standard library only.
"""
import argparse
import collections
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("RETRO_HOME", Path.home() / ".claude" / "retro"))
WEEKS = HOME / "weekly"
CONFIG = HOME / "week-review.json"
PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
LOCAL_TZ = dt.datetime.now().astimezone().tzinfo

DEFAULT_CONFIG = {
    "github_owner": "",
    "exclude_patterns": [],
    "savings_categories": [],
    "savings_confirmed": False,
    "ignore_category_groups": ["Internal Master Category", "Credit Card Payments"],
    "idle_gap_minutes": 5,
}


# ---------------------------------------------------------------- basics

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    else:
        HOME.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def week_of(arg):
    today = dt.date.today()
    if not arg:
        start = today - dt.timedelta(days=today.weekday() + 7)
    elif re.fullmatch(r"\d{4}-W\d{2}", arg):
        y, w = arg.split("-W")
        start = dt.date.fromisocalendar(int(y), int(w), 1)
    else:
        d = dt.date.fromisoformat(arg)
        start = d - dt.timedelta(days=d.weekday())
    end = start + dt.timedelta(days=6)
    y, w, _ = start.isocalendar()
    return {
        "id": f"{y}-W{w:02d}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "next_start": (start + dt.timedelta(days=7)).isoformat(),
        "next_end": (end + dt.timedelta(days=7)).isoformat(),
        "prev_id": "{}-W{:02d}".format(*(start - dt.timedelta(days=7)).isocalendar()[:2]),
        "dir": str(WEEKS / f"{y}-W{w:02d}"),
    }


def bounds(week):
    lo = dt.datetime.fromisoformat(week["start"]).replace(tzinfo=LOCAL_TZ)
    return lo, lo + dt.timedelta(days=7)


def save(week, name, data):
    d = Path(week["dir"])
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {d / name}")


def read(week_dir, name):
    p = Path(week_dir) / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError as e:
        print(f"warning: {p} is not valid JSON ({e}); skipped", file=sys.stderr)
        return None


def excluded(text, cfg):
    t = (text or "").lower()
    return any(p.lower() in t for p in cfg["exclude_patterns"])


def one_line(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    except (AttributeError, ValueError):
        return None


# ---------------------------------------------------------------- sessions

WRAPPERS = ("<local-command", "<command-", "<system-reminder>", "<task-notification>")
CORRECTION = re.compile(
    r"^\s*(no[,.! ]|nope|wrong|that's not|that is not|not what i|i said|i asked|stop\b|don't|do not|"
    r"why did you|undo|revert|te dije|eso no|está mal|esta mal|no era|deja de|para\b)",
    re.I,
)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def session_stats(path, lo, hi, gap):
    entrypoint = cwd = title = None
    first_prompt = None
    stamps, prompts, corrections = [], 0, 0
    tools = errors = denials = 0
    cost = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("type")
            entrypoint = entrypoint or e.get("entrypoint")
            cwd = cwd or e.get("cwd")
            if kind == "custom-title" and e.get("customTitle"):
                title = e["customTitle"]
            if kind == "cost-state":
                cost = e.get("totalCostUSD")
            if kind not in ("user", "assistant") or e.get("isSidechain"):
                continue
            ts = parse_ts(e.get("timestamp"))
            if not ts or not (lo <= ts < hi):
                continue
            stamps.append(ts)
            content = (e.get("message") or {}).get("content")
            if kind == "assistant" and isinstance(content, list):
                tools += sum(1 for x in content if isinstance(x, dict) and x.get("type") == "tool_use")
            if kind == "user":
                if isinstance(content, list):
                    for x in content:
                        if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("is_error"):
                            errors += 1
                            if e.get("toolDenialKind") or "doesn't want to proceed" in json.dumps(x.get("content"))[:400]:
                                denials += 1
                if e.get("promptSource") == "typed" or (e.get("isMeta") is not True and isinstance(content, str)):
                    text = text_of(content)
                    if text and not text.lstrip().startswith(WRAPPERS):
                        prompts += 1
                        first_prompt = first_prompt or one_line(text, 90)
                        if CORRECTION.search(text):
                            corrections += 1
    if not stamps:
        return None
    stamps.sort()
    active = sum(min((b - a).total_seconds(), gap * 60) for a, b in zip(stamps, stamps[1:]))
    return {
        "id": path.stem[:8],
        "project": (cwd or path.parent.name).replace(str(Path.home()), "~"),
        "automated": entrypoint == "sdk-cli",
        "title": one_line(title, 90) if title else first_prompt or "(no prompt)",
        "start": stamps[0].isoformat(timespec="minutes"),
        "days": sorted({s.date().isoformat() for s in stamps}),
        "active_min": round(active / 60),
        "prompts": prompts,
        "tool_calls": tools,
        "errors": errors,
        "denials": denials,
        "corrections": corrections,
        "cost_usd": round(cost, 2) if isinstance(cost, (int, float)) else None,
    }


def cmd_sessions(week, cfg):
    lo, hi = bounds(week)
    out, skipped = [], collections.Counter()
    for path in PROJECTS.glob("*/*.jsonl"):
        if dt.datetime.fromtimestamp(path.stat().st_mtime, LOCAL_TZ) < lo:
            continue
        s = session_stats(path, lo, hi, cfg["idle_gap_minutes"])
        if not s:
            continue
        if excluded(s["project"], cfg):
            skipped["excluded"] += 1
            continue
        out.append(s)
    out.sort(key=lambda s: s["start"])
    print(f"{len(out)} sessions ({sum(s['automated'] for s in out)} automated), {skipped['excluded']} excluded by pattern")
    save(week, "sessions.json", {"week": week["id"], "sessions": out, "excluded": skipped["excluded"]})


# ---------------------------------------------------------------- github

def gh(args):
    r = subprocess.run(["gh", "api", "-X", "GET", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"gh failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def gh_search(kind, q):
    items, page = [], 1
    while True:
        data = gh([f"search/{kind}", "-f", f"q={q}", "-f", "per_page=100", "-f", f"page={page}"])
        items += data["items"]
        if len(items) >= data["total_count"] or not data["items"] or page >= 10:
            return items
        page += 1


def cmd_github(week, cfg):
    owner = cfg["github_owner"] or gh(["user"])["login"]
    rng = f"{week['start']}..{week['end']}"
    pad = lambda d, n: (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()
    wide = f"{pad(week['start'], -1)}..{pad(week['end'], 1)}"  # search dates are UTC; filter locally below
    lo, hi = bounds(week)
    commits = collections.defaultdict(list)
    for c in gh_search("commits", f"author:{owner} committer-date:{wide}"):
        repo = c["repository"]["full_name"]
        when = parse_ts(c["commit"]["committer"]["date"])
        if c["repository"]["owner"]["login"] != owner or excluded(repo, cfg) or not when or not (lo <= when < hi):
            continue
        commits[repo].append({
            "sha": c["sha"][:7],
            "date": when.date().isoformat(),
            "message": one_line(c["commit"]["message"].splitlines()[0], 100),
            "url": c["html_url"],
        })
    items = []
    for i in gh_search("issues", f"user:{owner} updated:{rng}"):
        repo = "/".join(i["repository_url"].split("/")[-2:])
        if excluded(repo, cfg):
            continue
        in_week = lambda k: bool(i.get(k)) and lo <= parse_ts(i[k]) < hi
        items.append({
            "repo": repo,
            "number": i["number"],
            "pr": "pull_request" in i,
            "title": one_line(i["title"], 100),
            "state": i["state"],
            "merged": bool((i.get("pull_request") or {}).get("merged_at")),
            "opened_in_week": in_week("created_at"),
            "closed_in_week": in_week("closed_at"),
            "url": i["html_url"],
        })
    repos = sorted(commits, key=lambda r: -len(commits[r]))
    print(f"{sum(map(len, commits.values()))} commits in {len(repos)} repos, {len(items)} PRs/issues touched")
    save(week, "github.json", {
        "week": week["id"], "owner": owner,
        "commits": {r: sorted(commits[r], key=lambda c: c["date"]) for r in repos},
        "items": items,
        "note": "commits come from GitHub search, which covers default branches only",
    })


# ---------------------------------------------------------------- ynab

def ynab_token():
    if os.environ.get("YNAB_TOKEN"):
        return os.environ["YNAB_TOKEN"]
    try:
        conf = json.loads((Path.home() / ".claude.json").read_text())
        return conf["mcpServers"]["ynab"]["env"]["YNAB_TOKEN"]
    except (OSError, ValueError, KeyError):
        sys.exit("no YNAB token: set YNAB_TOKEN or register the ynab MCP server with it (claude mcp add ynab -e YNAB_TOKEN=...)")


def ynab_get(path, token):
    req = urllib.request.Request("https://api.ynab.com/v1" + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        sys.exit(f"YNAB {path}: HTTP {e.code} {e.read()[:200]!r}")


def cmd_ynab(week, cfg):
    token = ynab_token()
    b = "/budgets/last-used"
    start, end = dt.date.fromisoformat(week["start"]), dt.date.fromisoformat(week["end"])
    month_start = end.replace(day=1)
    since = min(start - dt.timedelta(days=28), month_start)
    settings = ynab_get(b + "/settings", token)["settings"]["currency_format"]
    cats = ynab_get(b + "/categories", token)["category_groups"]
    months = ynab_get(b + "/months", token)["months"]
    month = ynab_get(f"{b}/months/{month_start.isoformat()}", token)["month"]
    txns = ynab_get(f"{b}/transactions?since_date={since.isoformat()}", token)["transactions"]

    group_of, name_of = {}, {}
    for g in cats:
        for c in g["categories"]:
            group_of[c["id"]], name_of[c["id"]] = g["name"], c["name"]
    ignore = set(cfg["ignore_category_groups"])
    savings = set(cfg["savings_categories"])

    week_spend, prior_spend, month_spend = collections.Counter(), collections.Counter(), collections.Counter()
    saved = collections.Counter()
    prior_lo = start - dt.timedelta(days=28)
    for t in txns:
        if t.get("deleted"):
            continue
        day = dt.date.fromisoformat(t["date"])
        parts = [s for s in t.get("subtransactions") or [] if not s.get("deleted")] or [t]
        for p in parts:
            cid = p.get("category_id")
            if not cid or group_of.get(cid) in ignore:
                continue
            amount = -p["amount"]  # outflow positive, refunds negative
            if start <= day <= end:
                (saved if name_of.get(cid) in savings else week_spend)[cid] += amount
            elif prior_lo <= day < start and name_of.get(cid) not in savings:
                prior_spend[cid] += amount
            if month_start <= day <= end and name_of.get(cid) not in savings:
                month_spend[cid] += amount

    def cat_rows(counter, extra=None):
        rows = []
        for cid, amt in counter.items():
            if amt == 0:
                continue
            row = {"category": name_of.get(cid, "?"), "group": group_of.get(cid), "amount": amt}
            if extra:
                row.update(extra(cid))
            rows.append(row)
        return sorted(rows, key=lambda r: -r["amount"])

    weekly = cat_rows(week_spend, lambda cid: {"avg_prior_4w": round(prior_spend.get(cid, 0) / 4)})
    for cid, amt in prior_spend.items():  # categories that went quiet this week
        if cid not in week_spend and amt > 0:
            weekly.append({"category": name_of.get(cid, "?"), "group": group_of.get(cid),
                           "amount": 0, "avg_prior_4w": round(amt / 4)})

    days_in_month = ((month_start + dt.timedelta(days=32)).replace(day=1) - month_start).days
    pace = end.day / days_in_month
    budget = []
    for c in month["categories"]:
        if c.get("hidden") or c.get("deleted") or c.get("category_group_name") in ignore or c["name"] in savings:
            continue
        spent = month_spend.get(c["id"], 0)
        if spent <= 0 and c["balance"] >= 0:
            continue
        available = spent + c["balance"]  # carried over + assigned this month; balance is as of today
        used = spent / available if available > 0 else None
        status = ("overspent" if c["balance"] < 0 else "done" if c["balance"] == 0
                  else "ahead" if used is not None and used > pace * 1.15 + 0.05 else "ok")
        budget.append({"status": status,
            "category": c["name"], "group": c.get("category_group_name"),
            "budgeted": c["budgeted"], "spent": spent, "balance": c["balance"], "available": available,
            "used": round(used, 3) if used is not None else None,
        })
    order = {"overspent": 0, "ahead": 1, "ok": 2, "done": 3}
    budget.sort(key=lambda r: (order[r["status"]], -(r["used"] or 0), -r["spent"]))

    history = [{"month": m["month"], "age_of_money": m["age_of_money"]}
               for m in sorted(months, key=lambda m: m["month"]) if not m.get("deleted")]
    history = [h for h in history if h["month"] <= month_start.isoformat()][-12:]
    known = {c for g in cats for c in (x["name"] for x in g["categories"] if not x["hidden"] and not x["deleted"])}
    data = {
        "week": week["id"],
        "currency": settings,
        "age_of_money": month.get("age_of_money"),
        "age_of_money_note": "YNAB only exposes today's value; earlier weeks come from saved snapshots",
        "age_of_money_months": history,
        "week_total": sum(r["amount"] for r in weekly),
        "prior_4w_avg_total": round(sum(prior_spend.values()) / 4),
        "saved": cat_rows(saved),
        "weekly": weekly,
        "month": month_start.isoformat(),
        "month_pace": round(pace, 3),
        "month_budgeted": sum(r["budgeted"] for r in budget),
        "month_spent": sum(r["spent"] for r in budget),
        "budget": budget,
        "savings_categories": sorted(savings),
        "savings_confirmed": cfg["savings_confirmed"],
        "unknown_savings_categories": sorted(savings - known),
        "fetched_at": dt.datetime.now(LOCAL_TZ).isoformat(timespec="minutes"),
    }
    print(f"week spent {money(data['week_total'], settings)}, 4-week avg {money(data['prior_4w_avg_total'], settings)}, "
          f"age of money {data['age_of_money']} days")
    if not cfg["savings_confirmed"]:
        print("savings categories NOT confirmed yet: ask the user, then edit", CONFIG)
    save(week, "ynab.json", data)


def money(milli, cf):
    if milli is None:
        return "?"
    digits = cf.get("decimal_digits", 2)
    val = abs(milli) / 1000
    whole, frac = f"{val:,.{digits}f}".split(".") if digits else (f"{val:,.0f}", "")
    whole = whole.replace(",", cf.get("group_separator", ","))
    num = whole + (cf.get("decimal_separator", ".") + frac if frac else "")
    sym = cf.get("currency_symbol", "")
    s = sym + num if cf.get("symbol_first", True) else num + sym
    return ("-" if milli < 0 else "") + s


# ---------------------------------------------------------------- calendar

def cal_events(cal, key):
    out = []
    for e in (cal or {}).get(key, []) or []:
        s, t = e.get("start"), e.get("end")
        if not s:
            continue
        all_day = e.get("all_day") or len(s) == 10
        hours = 0.0
        if not all_day and t:
            a, b = dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(t)
            hours = max((b - a).total_seconds() / 3600, 0)
        out.append({**e, "all_day": all_day, "hours": hours, "day": s[:10]})
    return sorted(out, key=lambda e: e["start"])


# ---------------------------------------------------------------- summary

def gather(week):
    d = week["dir"]
    return {n: read(d, f"{n}.json") for n in ("sessions", "github", "ynab", "calendar", "notes")}, \
        read(Path(WEEKS / week["prev_id"]), "ynab.json")


def cmd_summary(week, _cfg):
    data, prev_ynab = gather(week)
    print(f"Week {week['id']}: {week['start']} to {week['end']}; next {week['next_start']} to {week['next_end']}")
    cal = data["calendar"]
    if cal:
        ev = cal_events(cal, "events")
        by_day = collections.Counter()
        for e in ev:
            by_day[e["day"]] += e["hours"]
        print(f"\nCalendar: {len(ev)} events, {sum(by_day.values()):.1f} h; per day " +
              ", ".join(f"{d[5:]} {h:.1f}h" for d, h in sorted(by_day.items())))
        for e in ev:
            print(f"  {e['start'][:16]} {e.get('summary', '')}")
        for h in cal.get("holidays", []):
            print(f"  holiday {h.get('date')} {h.get('summary')}")
        nx = cal_events(cal, "next")
        print(f"Next week: {len(nx)} events")
        for e in nx:
            print(f"  {e['start'][:16]} {e.get('summary', '')}")
    else:
        print("\nCalendar: not collected")
    gh_ = data["github"]
    if gh_:
        print(f"\nGitHub ({gh_['owner']}): " + ", ".join(f"{r} {len(c)}" for r, c in gh_["commits"].items()) or "no commits")
        for r, cs in gh_["commits"].items():
            for c in cs[:6]:
                print(f"  {r.split('/')[-1]} {c['date']} {c['message']}")
            if len(cs) > 6:
                print(f"  … {len(cs) - 6} more")
        merged = [i for i in gh_["items"] if i["merged"] and i["closed_in_week"]]
        opened = [i for i in gh_["items"] if i["opened_in_week"]]
        print(f"  PRs merged {len(merged)}, issues/PRs opened {len(opened)}")
    se = data["sessions"]
    if se:
        live = [s for s in se["sessions"] if not s["automated"]]
        auto = [s for s in se["sessions"] if s["automated"]]
        per = collections.defaultdict(lambda: [0, 0])
        for s in live:
            per[s["project"]][0] += 1
            per[s["project"]][1] += s["active_min"]
        print(f"\nClaude: {len(live)} interactive sessions, {sum(s['active_min'] for s in live) / 60:.1f} h active; "
              f"{len(auto)} automated runs; cost ${sum(s['cost_usd'] or 0 for s in live):.0f} interactive, "
              f"${sum(s['cost_usd'] or 0 for s in auto):.0f} automated")
        for p, (n, m) in sorted(per.items(), key=lambda x: -x[1][1]):
            print(f"  {p}: {n} sessions, {m / 60:.1f} h")
        rough = sorted(live, key=lambda s: -(s["corrections"] * 3 + s["denials"] * 2 + s["errors"]))[:5]
        for s in rough:
            if s["corrections"] or s["errors"] > 3 or s["denials"]:
                print(f"  friction {s['id']} {s['title']}: {s['corrections']} corrections, {s['errors']} errors, {s['denials']} denials")
    y = data["ynab"]
    if y:
        cf = y["currency"]
        print(f"\nYNAB: week spent {money(y['week_total'], cf)} vs 4-week avg {money(y['prior_4w_avg_total'], cf)}; "
              f"saved {money(sum(r['amount'] for r in y['saved']), cf)}; age of money {y['age_of_money']} d"
              + (f" (last week {prev_ynab['age_of_money']} d)" if prev_ynab else " (no snapshot from last week)"))
        for r in y["weekly"][:8]:
            print(f"  {r['category']}: {money(r['amount'], cf)} (avg {money(r['avg_prior_4w'], cf)})")
        print(f"  Month {y['month'][:7]} at {y['month_pace'] * 100:.0f}% of the month: spent {money(y['month_spent'], cf)} "
              f"of {money(y['month_budgeted'], cf)} budgeted")
        print(f"  {sum(r['status'] == 'done' for r in y['budget'])} categories fully spent as planned")
        for r in [r for r in y["budget"] if r["status"] != "done"][:6]:
            used = f"{r['used'] * 100:.0f}% used" if r["used"] is not None else "nothing available"
            print(f"  {r['status']}: {r['category']}: spent {money(r['spent'], cf)}, {used}, left {money(r['balance'], cf)}")
        if not y["savings_confirmed"]:
            print("  savings categories not confirmed:", y["savings_categories"] or "none set")


# ---------------------------------------------------------------- render

CSS = """
:root { --cobalt:#1B4DCC; --azure:#5B8DEF; --ink:#0A1530; --ink-2:#2A3454; --ink-3:#58607A;
  --paper:#F4F5F8; --paper-2:#ECEEF3; --wire:#D4D9E4; --card:#FFFFFF;
  --ok:#10A37F; --warn:#E0A106; --bad:#D14836; --bar:var(--cobalt); --bar-2:var(--wire); }
@media (prefers-color-scheme: dark) { :root { --ink:#F4F5F8; --ink-2:#C9CEDB; --ink-3:#8B93AB;
  --paper:#0A1530; --paper-2:#121F42; --wire:#26335A; --card:#0F1B3A; --bar:var(--azure); --bar-2:#26335A; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 Geist, -apple-system, system-ui, sans-serif; }
main { max-width:1040px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:28px; margin:0 0 4px; letter-spacing:-0.01em; }
h2 { font-size:18px; margin:0 0 12px; }
h3 { font:600 12px/1.4 "JetBrains Mono", ui-monospace, monospace; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); margin:16px 0 8px; }
.sub { color:var(--ink-3); margin:0 0 24px; }
.mono { font-family:"JetBrains Mono", ui-monospace, monospace; font-size:13px; }
.card { background:var(--card); border:1px solid var(--wire); border-radius:8px; padding:20px; margin:0 0 16px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; margin:0 0 16px; }
.tile { background:var(--card); border:1px solid var(--wire); border-radius:8px; padding:14px 16px; }
.tile .k { font:600 11px "JetBrains Mono", monospace; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); }
.tile .v { font-size:24px; font-weight:600; margin-top:4px; }
.tile .d { font-size:13px; color:var(--ink-3); }
ul.notes { margin:0; padding-left:20px; } ul.notes li { margin:4px 0; }
table { width:100%; border-collapse:collapse; font-size:14px; }
td, th { padding:6px 8px; border-bottom:1px solid var(--wire); text-align:left; vertical-align:top; }
th { font:600 11px "JetBrains Mono", monospace; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.bar { position:relative; height:8px; background:var(--bar-2); border-radius:4px; min-width:80px; }
.bar i { position:absolute; left:0; top:0; bottom:0; background:var(--bar); border-radius:4px; }
.bar b { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--ink); }
.bar.over i { background:var(--bad); } .bar.near i { background:var(--warn); }
.badge { display:inline-block; font:600 11px "JetBrains Mono", monospace; padding:1px 6px; border-radius:4px; border:1px solid var(--wire); color:var(--ink-2); }
.badge.bad { color:var(--bad); border-color:var(--bad); } .badge.ok { color:var(--ok); border-color:var(--ok); }
.days { display:grid; grid-template-columns:repeat(7, 1fr); gap:6px; }
.day { border:1px solid var(--wire); border-radius:8px; padding:8px; min-height:90px; font-size:13px; }
.day .h { font:600 11px "JetBrains Mono", monospace; color:var(--ink-3); }
.day.hol { background:var(--paper-2); }
.day div.e { margin-top:4px; overflow-wrap:anywhere; }
a { color:var(--cobalt); text-decoration:none; } a:hover { text-decoration:underline; }
@media (prefers-color-scheme: dark) { a { color:var(--azure); } }
details summary { cursor:pointer; color:var(--ink-3); font-size:14px; margin-top:8px; }
.muted { color:var(--ink-3); }
@media (max-width:700px) { .days { grid-template-columns:1fr; } .day { min-height:0; } }
"""

E = html.escape
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def bar(frac, marker=None, cls=""):
    """A bar filled to frac (0..1) with an optional marker line at marker (0..1)."""
    clamp = lambda v: max(0.0, min(v or 0, 1.0)) * 100
    m = f'<b style="left:{clamp(marker):.1f}%"></b>' if marker is not None else ""
    return f'<div class="bar {cls}"><i style="width:{clamp(frac):.1f}%"></i>{m}</div>'


def tile(k, v, d=""):
    return f'<div class="tile"><div class="k">{E(k)}</div><div class="v">{v}</div><div class="d">{d}</div></div>'


def calendar_days(events, holidays, start):
    days = [(dt.date.fromisoformat(start) + dt.timedelta(days=i)).isoformat() for i in range(7)]
    hol = {h.get("date"): h.get("summary", "") for h in holidays or []}
    cells = []
    for i, d in enumerate(days):
        evs = [e for e in events if e["day"] == d]
        hrs = sum(e["hours"] for e in evs)
        body = "".join(
            f'<div class="e"><span class="mono muted">{"all day" if e["all_day"] else E(e["start"][11:16])}</span> {E(e.get("summary", "(busy)"))}</div>'
            for e in evs)
        if d in hol:
            body = f'<div class="e"><span class="badge">holiday</span> {E(hol[d])}</div>' + body
        cells.append(f'<div class="day{" hol" if d in hol else ""}"><div class="h">{DOW[i]} {d[8:]}'
                     f'{f" · {hrs:.1f}h" if hrs else ""}</div>{body or "<div class=e muted>free</div>"}</div>')
    return '<div class="days">' + "".join(cells) + "</div>"


def aom_svg(points):
    pts = [(p["label"], p["value"]) for p in points if isinstance(p["value"], (int, float))]
    if len(pts) < 2:
        return ""
    w, h, pad = 600, 120, 24
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    xy = [(pad + i * (w - 2 * pad) / (len(pts) - 1), h - pad - (v - lo) / span * (h - 2 * pad)) for i, (_, v) in enumerate(pts)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(xy))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="var(--bar)"><title>{E(lbl)}: {v} days</title></circle>'
                   for (x, y), (lbl, v) in zip(xy, pts))
    labels = "".join(f'<text x="{x:.1f}" y="{h - 4}" font-size="10" text-anchor="middle" fill="var(--ink-3)">{E(lbl)}</text>'
                     for (x, _), (lbl, _) in zip(xy, pts))
    vals = "".join(f'<text x="{x:.1f}" y="{y - 8:.1f}" font-size="10" text-anchor="middle" fill="var(--ink-2)">{v}</text>'
                   for (x, y), (_, v) in zip(xy, pts))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Age of money trend">'
            f'<path d="{path}" fill="none" stroke="var(--bar)" stroke-width="2"/>{dots}{vals}{labels}</svg>')


def aom_points(y, week):
    pts = [{"label": m["month"][:7], "value": m["age_of_money"]} for m in y["age_of_money_months"][:-1]]
    snaps = []
    for p in sorted(WEEKS.glob("*-W*/ynab.json")):
        if p.parent.name <= week["id"]:
            try:
                snaps.append({"label": p.parent.name[5:], "value": json.loads(p.read_text())["age_of_money"]})
            except (ValueError, KeyError):
                pass
    return pts[-6:] + snaps[-6:]


def section_ynab(y, prev):
    cf = y["currency"]
    m = lambda v: E(money(v, cf))
    delta = y["week_total"] - y["prior_4w_avg_total"]
    aom_prev = prev["age_of_money"] if prev else None
    tiles = "".join([
        tile("Spent this week", m(y["week_total"]),
             f'{"+" if delta >= 0 else ""}{m(delta)} vs 4-week avg {m(y["prior_4w_avg_total"])}'),
        tile("Saved", m(sum(r["amount"] for r in y["saved"])), ", ".join(E(r["category"]) for r in y["saved"]) or "nothing moved"),
        tile("Age of money", f'{E(str(y["age_of_money"]))} d',
             f'{"+" if (y["age_of_money"] or 0) >= aom_prev else ""}{(y["age_of_money"] or 0) - aom_prev} d vs last week'
             if aom_prev is not None else "first snapshot, trend starts next week"),
        tile(f'{y["month"][:7]} so far', m(y["month_spent"]),
             f'of {m(y["month_budgeted"])} budgeted, {y["month_pace"] * 100:.0f}% of the month gone'),
    ])
    top = max([r["amount"] for r in y["weekly"]] + [r["avg_prior_4w"] for r in y["weekly"]] + [1])
    rows = "".join(
        f'<tr><td>{E(r["category"])}</td><td class="n">{m(r["amount"])}</td><td class="n muted">{m(r["avg_prior_4w"])}</td>'
        f'<td style="width:35%">{bar(r["amount"] / top, r["avg_prior_4w"] / top)}</td></tr>'
        for r in sorted(y["weekly"], key=lambda r: -max(r["amount"], r["avg_prior_4w"]))[:15])
    weekly = (f'<h3>Spending by category vs 4-week average</h3><table><tr><th>Category</th><th class="n">This week</th>'
              f'<th class="n">Avg</th><th>Bar: week, line: avg</th></tr>{rows}</table>')
    brows = []
    done = [r for r in y["budget"] if r["status"] == "done"]
    active = [r for r in y["budget"] if r["status"] != "done"]
    for r in active[:12]:
        b = r["budgeted"]
        ratio = r["used"] if r["used"] is not None else 1.5
        cls = "over" if r["balance"] < 0 or ratio > 1 else "near" if ratio > y["month_pace"] * 1.15 + 0.05 else ""
        flag = '<span class="badge bad">overspent</span>' if r["balance"] < 0 else \
            '<span class="badge">ahead of pace</span>' if cls == "near" else ""
        brows.append(f'<tr><td>{E(r["category"])} {flag}</td><td class="n">{m(r["spent"])}</td><td class="n muted">{m(b)}</td>'
                     f'<td class="n">{m(r["balance"])}</td><td style="width:30%">{bar(ratio / 1.5, y["month_pace"] / 1.5, cls)}</td></tr>')
    budget = (f'<h3>Budget pace, {y["month"][:7]}: spent vs available (carry-over plus assigned); the line is how much of the month is gone</h3><table><tr><th>Category</th>'
              f'<th class="n">Spent</th><th class="n">Assigned</th><th class="n">Left</th><th>Used of available</th></tr>{"".join(brows)}</table>')
    if active[12:]:
        budget += (f'<details><summary>{len(active) - 12} more categories on or under pace</summary><p>'
                   + ", ".join(f'{E(r["category"])} {m(r["spent"])}' for r in active[12:]) + "</p></details>")
    if done:
        budget += (f'<details><summary>{len(done)} categories fully spent as planned ({m(sum(r["spent"] for r in done))})</summary>'
                   f'<p>{", ".join(E(r["category"]) + " " + m(r["spent"]) for r in done)}</p></details>')
    chart = aom_svg(aom_points(y, {"id": y["week"]}))
    aom = f'<h3>Age of money (month ends, then weekly snapshots)</h3>{chart}' if chart else ""
    note = "" if y["savings_confirmed"] else \
        '<p class="muted">Savings categories not confirmed yet; spending may include money that was only moved to savings.</p>'
    return f'<section class="card"><h2>Money</h2><div class="tiles">{tiles}</div>{weekly}{budget}{aom}{note}</section>'


def section_github(g):
    total = sum(map(len, g["commits"].values()))
    merged = [i for i in g["items"] if i["merged"] and i["closed_in_week"]]
    opened_issues = [i for i in g["items"] if i["opened_in_week"] and not i["pr"]]
    closed_issues = [i for i in g["items"] if i["closed_in_week"] and not i["pr"]]
    tiles = "".join([tile("Commits", total, f'{len(g["commits"])} repos'), tile("PRs merged", len(merged)),
                     tile("Issues", f'+{len(opened_issues)} / -{len(closed_issues)}', "opened / closed")])
    top = max([len(c) for c in g["commits"].values()] + [1])
    repos = "".join(
        f'<tr><td><a href="https://github.com/{E(r)}">{E(r.split("/")[-1])}</a></td><td class="n">{len(cs)}</td>'
        f'<td style="width:40%">{bar(len(cs) / top)}</td></tr>' for r, cs in g["commits"].items())
    det = "".join(
        f'<details><summary>{E(r.split("/")[-1])}: {len(cs)} commits</summary><table>' +
        "".join(f'<tr><td class="mono muted">{E(c["date"][5:])}</td><td><a href="{E(c["url"])}">{E(c["message"])}</a></td></tr>' for c in cs) +
        "</table></details>" for r, cs in g["commits"].items())
    prs = "".join(f'<li><a href="{E(i["url"])}">{E(i["repo"].split("/")[-1])}#{E(str(i["number"]))}</a> {E(i["title"])}</li>' for i in merged[:15])
    return (f'<section class="card"><h2>GitHub</h2><div class="tiles">{tiles}</div>'
            f'<table><tr><th>Repo</th><th class="n">Commits</th><th></th></tr>{repos}</table>{det}'
            + (f'<h3>Merged</h3><ul class="notes">{prs}</ul>' if prs else "") +
            f'<p class="muted">{E(g["note"])}</p></section>')


def section_sessions(s):
    live = [x for x in s["sessions"] if not x["automated"]]
    auto = [x for x in s["sessions"] if x["automated"]]
    cost = sum(x["cost_usd"] or 0 for x in s["sessions"])
    per = collections.defaultdict(list)
    for x in live:
        per[x["project"]].append(x)
    days = collections.Counter()
    for x in live:
        for d in x["days"]:
            days[d] += 1
    tiles = "".join([
        tile("Active time", f'{sum(x["active_min"] for x in live) / 60:.1f} h', f'{len(live)} sessions, {len(per)} projects'),
        tile("Automated runs", len(auto), f'{len({x["project"] for x in auto})} projects'),
        tile("Cost", f"${cost:,.0f}", f'${sum(x["cost_usd"] or 0 for x in auto):,.0f} of it automated; whole-session cost'),
        tile("Days with sessions", len(days), ", ".join(DOW[dt.date.fromisoformat(d).weekday()] for d in sorted(days))),
    ])
    top = max([sum(x["active_min"] for x in v) for v in per.values()] + [1])
    rows = []
    for p, xs in sorted(per.items(), key=lambda kv: -sum(x["active_min"] for x in kv[1])):
        mins = sum(x["active_min"] for x in xs)
        titles = "".join(f'<li><span class="mono muted">{E(x["start"][5:10])} {E(x["id"])}</span> {E(x["title"])}</li>' for x in xs)
        rows.append(f'<tr><td><details><summary class="mono">{E(p)}</summary><ul class="notes">{titles}</ul></details></td>'
                    f'<td class="n">{len(xs)}</td><td class="n">{mins / 60:.1f} h</td><td style="width:30%">{bar(mins / top)}</td></tr>')
    rough = [x for x in sorted(live, key=lambda x: -(x["corrections"] * 3 + x["denials"] * 2 + x["errors"]))
             if x["corrections"] or x["denials"] or x["errors"] > 3][:6]
    fr = "".join(
        f'<tr><td><span class="mono muted">{E(x["id"])}</span> {E(x["title"])}<div class="mono muted">{E(x["project"])}</div></td>'
        f'<td class="n">{x["corrections"]}</td><td class="n">{x["errors"]}</td><td class="n">{x["denials"]}</td></tr>' for x in rough)
    friction = (f'<h3>Friction (worth a /retro:session-retro &lt;id&gt;)</h3><table><tr><th>Session</th><th class="n">Corrections</th>'
                f'<th class="n">Errors</th><th class="n">Denials</th></tr>{fr}</table>'
                '<p class="muted">Corrections are a keyword heuristic on your messages; treat them as a hint.</p>') if fr else ""
    autos = collections.Counter(x["project"] for x in auto)
    au = ("<h3>Automated runs</h3><p>" + ", ".join(f'<span class="mono">{E(p)}</span> {n}' for p, n in autos.most_common()) + "</p>") if autos else ""
    exc = f'<p class="muted">{E(str(s["excluded"]))} sessions left out by exclude patterns.</p>' if s.get("excluded") else ""
    return (f'<section class="card"><h2>Claude Code</h2><div class="tiles">{tiles}</div>'
            f'<table><tr><th>Project</th><th class="n">Sessions</th><th class="n">Active</th><th></th></tr>{"".join(rows)}</table>'
            f'{friction}{au}{exc}</section>')


def section_calendar(cal, week):
    ev, nx = cal_events(cal, "events"), cal_events(cal, "next")
    hrs = sum(e["hours"] for e in ev)
    busiest = collections.Counter()
    for e in ev:
        busiest[e["day"]] += e["hours"]
    b = max(busiest.items(), key=lambda kv: kv[1]) if busiest else None
    tiles = "".join([tile("Events", len(ev), f"{hrs:.1f} h scheduled"),
                     tile("Busiest day", DOW[dt.date.fromisoformat(b[0]).weekday()] if b else "none", f"{b[1]:.1f} h" if b else ""),
                     tile("Next week", len(nx), f'{sum(e["hours"] for e in nx):.1f} h scheduled')])
    return (f'<section class="card"><h2>Calendar</h2><div class="tiles">{tiles}</div>'
            f'<h3>Last week</h3>{calendar_days(ev, cal.get("holidays"), week["start"])}</section>')


def section_next(cal, notes, week):
    nx = cal_events(cal or {}, "next")
    items = "".join(f"<li>{E(n)}</li>" for n in (notes or {}).get("next_week", []))
    days = calendar_days(nx, (cal or {}).get("next_holidays"), week["next_start"]) if cal else '<p class="muted">Calendar not collected.</p>'
    return (f'<section class="card"><h2>Next week, {week["next_start"][5:]} to {week["next_end"][5:]}</h2>'
            + (f'<ul class="notes">{items}</ul>' if items else "") + f'<h3>On the calendar</h3>{days}</section>')


def cmd_render(week, _cfg, open_page=True):
    data, prev_ynab = gather(week)
    notes = data["notes"] or {}
    obs = "".join(f"<li>{E(o)}</li>" for o in notes.get("observations", []))
    parts = [f'<h1>Week {week["id"][6:]}: {dt.date.fromisoformat(week["start"]).strftime("%b %-d")} to '
             f'{dt.date.fromisoformat(week["end"]).strftime("%b %-d, %Y")}</h1>'
             f'<p class="sub">Personal week review, generated {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}</p>']
    if obs:
        parts.append(f'<section class="card"><h2>What stood out</h2><ul class="notes">{obs}</ul></section>')
    parts.append(section_next(data["calendar"], notes, week))
    missing = []
    if data["calendar"]:
        parts.append(section_calendar(data["calendar"], week))
    else:
        missing.append("calendar")
    for key, fn in (("github", section_github), ("sessions", section_sessions)):
        if data[key]:
            parts.append(fn(data[key]))
        else:
            missing.append(key)
    if data["ynab"]:
        parts.append(section_ynab(data["ynab"], prev_ynab))
    else:
        missing.append("ynab")
    if missing:
        parts.append(f'<p class="muted">Not collected: {", ".join(missing)}.</p>')
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Week review {week["id"]}</title><style>{CSS}</style></head><body><main>{"".join(parts)}</main></body></html>')
    out = Path(week["dir"]) / "week-review.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out}")
    if open_page and sys.platform == "darwin":
        subprocess.run(["open", str(out)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["range", "sessions", "github", "ynab", "summary", "render"])
    ap.add_argument("--week")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    os.umask(0o077)  # the data folder holds finances and session titles: owner only
    HOME.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)
    week = week_of(a.week)
    cfg = load_config()
    if a.command == "range":
        print(json.dumps({**week, "config": str(CONFIG)}, indent=1))
    elif a.command == "render":
        cmd_render(week, cfg, not a.no_open)
    else:
        globals()[f"cmd_{a.command}"](week, cfg)


if __name__ == "__main__":
    main()
