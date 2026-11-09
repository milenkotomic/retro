# retro

Retrospectives for Claude Code. The first skill, `session-retro`, reads a
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
`/retro:session-retro`.

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

It links each skill into `~/.claude/skills/`, so the command is
`/session-retro` and edits take effect in the next session. Use one install
method per machine, not both.

## Layout

```
skills/session-retro/
  SKILL.md             the skill: steps, categories, report shape, rules
  scripts/digest.py    transcript to evidence sheet; Python 3 stdlib only
  evals/               test cases with a synthetic transcript
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
