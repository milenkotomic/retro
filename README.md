# retro

Retrospectives for Claude Code. Two skills:

- `session-retro`: what in the agent's environment slowed one session down.
- `week-review`: a personal Monday to Sunday review as an HTML page, from
  Google Calendar, your own GitHub repos, Claude Code sessions and YNAB, with
  a look at next week.

## session-retro

`session-retro` reads a
session transcript and asks one question: what in the agent's environment
would have made this session cheaper, faster or more correct? It answers with
at most five findings, each tied to evidence from the session, and changes
nothing until you pick which ones to apply.

```
/retro:session-retro                 # the current session
/retro:session-retro list            # choose from recent sessions in this directory
/retro:session-retro 8373ff95        # a session by id prefix
```

Plain language works too: "retro on this session", "what slowed you down?",
and in Spanish "retro de la sesión", "qué mejoramos del setup".

## What it looks at

The environment, not the work: steering files (CLAUDE.md, memory, skills),
automated checks, tool output, permissions and the information the agent
could reach. Findings fall into navigation, automated checks, steering files,
tool economy, permissions, information access and user corrections. A
mechanical rule becomes a check, a judgment call becomes a short steering
line, and a one-off becomes nothing.

Each finding comes with the evidence (timeline numbers or a quoted message),
what it cost, the exact change, and the effort.

## The digest

`skills/session-retro/scripts/digest.py` turns a transcript (often several
MB of JSON) into a short evidence sheet, so the retro spends its context on
judgment instead of parsing:

- session facts and tool counts
- the steering files in play, with their sizes
- every user message
- flags: errors, permission denials, timeouts, repeated identical calls,
  files read more than once, the largest outputs, and output lines that recur
  across many calls (banners and warnings that waste tokens on every call)
- a numbered tool timeline

Run it by hand: `python3 skills/session-retro/scripts/digest.py --list`, then
`python3 skills/session-retro/scripts/digest.py <id>`. Python 3 standard
library only; it reads transcripts under `~/.claude/projects/` and never
writes.

## week-review

```
/retro:week-review                   # last completed week
/retro:week-review 2026-W45          # a given ISO week (or any date inside it)
```

Or "review my week", "how did my week go", "repaso de la semana".

The page has: observations that connect the sources, next week's calendar and
heads-ups, last week's calendar, commits and merged PRs per repo, Claude Code
active time per project with a friction table (sessions worth a
`session-retro`), and money: spending per category against the 4-week
average, budget pace for the month (spent against what was available, with a
marker for how much of the month is gone), and age of money.

`skills/week-review/scripts/week.py` does the counting so numbers never pass
through the model; the agent collects the calendar through the Google
Calendar connector and writes the observations. Data and snapshots live in
`~/.claude/retro/weekly/<week>/`; the config is
`~/.claude/retro/week-review.json`:

| Key | Meaning |
|---|---|
| `github_owner` | account whose repos count; empty means the `gh` user |
| `exclude_patterns` | substrings that drop sessions (by project path) and repos, for work you keep out |
| `savings_categories` | YNAB categories shown as saved, not spent; confirmed on the first run |
| `ignore_category_groups` | YNAB groups that are never spending |
| `idle_gap_minutes` | gaps longer than this do not count as active session time |

Needs: `gh` logged in, the Google Calendar connector, and a YNAB token in
`YNAB_TOKEN` or in the `ynab` MCP server entry of `~/.claude.json`. Any
missing source is skipped and named on the page.

## Install

Requires Claude Code and Python 3.

The plugin is published through my plugin marketplace,
[milenkotomic/claude-plugins](https://github.com/milenkotomic/claude-plugins).
From a shell:

```sh
claude plugin marketplace add milenkotomic/claude-plugins
claude plugin install retro@milenkotomic
```

Or inside Claude Code: `/plugin marketplace add milenkotomic/claude-plugins`,
then `/plugin install retro@milenkotomic`. Start a new session and type
`/retro:session-retro` or `/retro:week-review`.

### Update

```sh
claude plugin marketplace update milenkotomic
claude plugin update retro@milenkotomic
```

Restart Claude Code afterwards. Updates follow the `version` in
`.claude-plugin/plugin.json`, so a change ships only with a version bump.

### Developing the skills

```sh
git clone https://github.com/milenkotomic/retro.git ~/Developer/retro
~/Developer/retro/install.sh
```

It links each skill into `~/.claude/skills/`, so the commands are
`/session-retro` and `/week-review`, and edits take effect in the next session. Use one install
method per machine, not both.

## Layout

```
skills/session-retro/
  SKILL.md             the skill: steps, categories, report shape, rules
  scripts/digest.py    transcript to evidence sheet; Python 3 stdlib only
  evals/               test cases with a synthetic transcript
skills/week-review/
  SKILL.md             the skill: collect, summarize, write notes, render
  scripts/week.py      collectors, summary and HTML renderer; Python 3 stdlib only
install.sh             symlink install for developing the skills
.claude-plugin/        plugin.json
```

## Credits

The idea of a retro aimed at the agent's environment rather than the work,
and of moving mechanical rules into checks, comes from Matt Pocock's
[retro](https://github.com/mattpocock/skills/tree/main/skills/engineering/retro)
skill (MIT). This is an independent implementation; no text was copied.

## License

MIT, see [LICENSE](LICENSE).
