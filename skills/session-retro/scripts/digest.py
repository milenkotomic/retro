#!/usr/bin/env python3
"""Digest a Claude Code session transcript into a compact evidence sheet.

Usage:
  digest.py                      newest session for the current directory
  digest.py SESSION              a session id (or unique prefix) or a .jsonl path
  digest.py --list [N]           list recent sessions for the current directory
  digest.py --project DIR ...    look in DIR's project folder instead of cwd's

The sheet has: session facts, the user's messages, a numbered tool timeline,
and flags (errors, timeouts, denials, repeated calls, re-read files, largest
outputs, output noise that recurs across calls), plus the steering files that
were in play. Python 3 standard library only; reads, never writes.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
WRAPPERS = ("<local-command-caveat>", "<command-name>", "<local-command-stdout>", "<system-reminder>", "<task-notification>")


def project_folder(directory):
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(Path(directory).resolve()))


def sessions(folder):
    return sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def resolve(arg, folder):
    if arg and arg.endswith(".jsonl"):
        return Path(arg).expanduser()
    found = sessions(folder)
    if not found:
        sys.exit(f"no sessions in {folder}")
    if not arg:
        return found[0]
    hits = [p for p in found if p.stem.startswith(arg)]
    if len(hits) != 1:
        sys.exit(f"{len(hits)} sessions match {arg!r} in {folder}")
    return hits[0]


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for x in content:
            if x.get("type") == "text":
                out.append(x.get("text", ""))
            elif x.get("type") == "tool_result":
                out.append(text_of(x.get("content")))
        return "\n".join(out)
    return ""


def one_line(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_input(name, inp):
    for key in ("command", "file_path", "pattern", "query", "skill", "url", "prompt", "description"):
        if key in inp:
            val = inp[key]
            if key == "command" and inp.get("description"):
                return f"{one_line(inp['description'], 60)} :: {one_line(val, 110)}"
            return one_line(str(val), 150)
    return one_line(json.dumps(inp, ensure_ascii=False), 150)


def first_prompt(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                e = json.loads(line)
                if e.get("type") == "user" and e.get("promptSource") == "typed":
                    return one_line(text_of(e["message"].get("content")), 80)
    except (OSError, ValueError):
        pass
    return ""


def load(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def steering(cwd):
    home = Path.home()
    files = [home / ".claude" / "CLAUDE.md"]
    d = Path(cwd).resolve()
    chain = []
    while True:
        chain += [d / "CLAUDE.md", d / ".claude" / "CLAUDE.md", d / "CLAUDE.local.md", d / "AGENTS.md"]
        if d == d.parent or d == home.parent:
            break
        d = d.parent
    files += chain
    imported = []
    for f in files:
        if f.is_file():
            for m in re.finditer(r"^@(\S+)", f.read_text(errors="ignore"), re.M):
                imported.append((f.parent / m.group(1)).expanduser())
    mem = project_folder(cwd) / "memory" / "MEMORY.md"
    out, seen = [], set()
    for f in files + imported + [mem]:
        f = f.resolve()
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        lines = f.read_text(errors="ignore").count("\n")
        out.append(f"- `{f}`: {lines} lines")
    return out


def digest(path, timeline_cap):
    entries = load(path)
    main = [e for e in entries if not e.get("isSidechain")]
    side = len(entries) - len(main)
    stamps = [e["timestamp"] for e in main if e.get("timestamp")]
    cwd = next((e["cwd"] for e in main if e.get("cwd")), os.getcwd())
    branch = next((e["gitBranch"] for e in main if e.get("gitBranch")), "")

    calls, results = {}, {}
    order = []
    user_msgs = []
    for e in main:
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for x in content:
                if x.get("type") == "tool_use":
                    calls[x["id"]] = (x["name"], x.get("input") or {}, e.get("timestamp", ""))
                    order.append(x["id"])
        elif e.get("type") == "user":
            if isinstance(content, list) and any(x.get("type") == "tool_result" for x in content):
                for x in content:
                    if x.get("type") == "tool_result":
                        results[x["tool_use_id"]] = (x, e)
            elif not e.get("isMeta"):
                t = text_of(content)
                if t.strip() and not t.lstrip().startswith(WRAPPERS):
                    user_msgs.append((e.get("timestamp", ""), e.get("promptSource") or "", t))
                elif t.lstrip().startswith("<command-name>"):
                    m = re.search(r"<command-name>(.*?)</command-name>", t)
                    user_msgs.append((e.get("timestamp", ""), "slash", m.group(1) if m else t))

    by_tool = collections.Counter(calls[i][0] for i in order)
    idx = {cid: n for n, cid in enumerate(order, 1)}
    rows, errors, timeouts, denials, sizes = [], [], [], [], []
    repeat = collections.Counter()
    reads = collections.Counter()
    noise = collections.Counter()
    for cid in order:
        name, inp, ts = calls[cid]
        res, rentry = results.get(cid, (None, {}))
        body = text_of(res.get("content")) if res else ""
        size = len(body)
        flag = ""
        if res and res.get("is_error"):
            flag = "ERR"
            if rentry.get("toolDenialKind") or "denied" in body[:200].lower():
                denials.append((idx[cid], name, rentry.get("toolDenialKind", ""), one_line(body, 160)))
                flag = "DENIED"
            else:
                errors.append((idx[cid], name, summarize_input(name, inp), one_line(body, 200)))
        if "did not complete within" in body or "timed out" in body[:300].lower():
            timeouts.append((idx[cid], name, summarize_input(name, inp)))
            flag = flag or "TIMEOUT"
        repeat[(name, json.dumps(inp, sort_keys=True))] += 1
        if name == "Read":
            reads[inp.get("file_path", "")] += 1
        for line in set(body.splitlines()):
            line = line.strip()
            if 20 <= len(line) <= 200:
                noise[line] += 1
        sizes.append((size, idx[cid], name, summarize_input(name, inp)))
        rows.append(f"{idx[cid]:>3}. {ts[11:16]} {name}{' [' + flag + ']' if flag else ''} {size}c :: {summarize_input(name, inp)}")

    out = []
    start = stamps[0] if stamps else ""
    end = stamps[-1] if stamps else ""
    try:
        dur = dt.datetime.fromisoformat(end.replace("Z", "+00:00")) - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        dur_s = f"{dur.total_seconds() / 60:.0f} min wall clock (includes idle gaps)"
    except ValueError:
        dur_s = "unknown"
    out += [f"# Session digest: {path.stem}", "",
            f"- File: `{path}` ({path.stat().st_size // 1024} KB, {len(entries)} entries, {side} subagent entries)",
            f"- Directory: `{cwd}`" + (f" | branch {branch}" if branch else ""),
            f"- Span: {start[:16]} to {end[:16]} UTC, {dur_s}",
            f"- User messages: {len(user_msgs)} | tool calls: {len(order)} | " + ", ".join(f"{k} {v}" for k, v in by_tool.most_common()),
            ""]
    out += ["## Steering files in play"] + (steering(cwd) or ["- none found"]) + [""]
    out += ["## User messages", "Corrections, repeats and frustration show up here."]
    for ts, src, t in user_msgs:
        out.append(f"- {ts[5:16]} ({src or 'other'}): {one_line(t, 400)}")
    out.append("")

    out += ["## Flags"]
    if errors:
        out.append(f"### Errors ({len(errors)})")
        out += [f"- #{n} {name}: {inp} -> {msg}" for n, name, inp, msg in errors]
    if denials:
        out.append(f"### Permission denials ({len(denials)})")
        out += [f"- #{n} {name} {kind}: {msg}" for n, name, kind, msg in denials]
    if timeouts:
        out.append(f"### Timeouts or backgrounded ({len(timeouts)})")
        out += [f"- #{n} {name}: {inp}" for n, name, inp in timeouts]
    reps = [(c, k) for k, c in repeat.items() if c > 1]
    if reps:
        out.append("### Identical calls repeated")
        for c, (name, inp) in sorted(reps, reverse=True)[:10]:
            out.append(f"- {c}x {name}: {one_line(inp, 150)}")
    rr = [(c, f) for f, c in reads.items() if c > 1]
    if rr:
        out.append("### Files read more than once")
        out += [f"- {c}x `{f}`" for c, f in sorted(rr, reverse=True)[:10]]
    out.append("### Largest tool outputs (characters)")
    out += [f"- #{n} {name} {size}c: {inp}" for size, n, name, inp in sorted(sizes, reverse=True)[:8]]
    common = [(c, l) for l, c in noise.items() if c >= 5 and c >= len(order) * 0.1]
    if common:
        out.append("### Output lines recurring across many calls (likely noise)")
        out += [f"- {c} calls: {one_line(l, 150)}" for c, l in sorted(common, reverse=True)[:8]]
    out.append("")

    out += ["## Tool timeline", "Format: number, time UTC, tool, [flag], output size, input summary."]
    if len(rows) > timeline_cap:
        half = timeline_cap // 2
        out += rows[:half] + [f"... {len(rows) - timeline_cap} calls omitted, rerun with --timeline {len(rows)} ..."] + rows[-half:]
    else:
        out += rows
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("session", nargs="?")
    p.add_argument("--project", default=os.getcwd())
    p.add_argument("--list", nargs="?", const=10, type=int)
    p.add_argument("--timeline", type=int, default=120, help="max timeline rows (default 120)")
    a = p.parse_args()
    folder = project_folder(a.project)
    if a.list:
        for s in sessions(folder)[: a.list]:
            when = dt.datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{s.stem}  {when}  {s.stat().st_size // 1024:>6} KB  {first_prompt(s)}")
        return
    print(digest(resolve(a.session, folder), a.timeline))


if __name__ == "__main__":
    main()
