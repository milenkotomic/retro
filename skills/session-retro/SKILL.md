---
name: session-retro
description: Run a retrospective on a Claude Code session to improve the agent's environment for future sessions, not to grade the work. Reads the session transcript, finds where the agent lost time, tokens or the user's trust (hard navigation, missing automated checks, steering-file bloat or gaps, noisy or expensive tools, permission friction, missing information, user corrections) and proposes at most five concrete changes, each tied to evidence. Suggests only; changes nothing until the user picks. Use when the user says "retro", "session retro", "retrospective on this session", "what slowed you down", "what should we improve in the setup", "how can the next session go better", or in Spanish "retro de la sesión", "retrospectiva", "qué te costó", "qué mejoramos del setup". Not for a weekly or personal review of the user's own work.
argument-hint: "[session id or prefix | list] [focus]"
---

# Session retro

A retro looks at one session and asks one question: what in the agent's
environment would have made this session cheaper, faster or more correct? The
environment is everything outside the model: steering files (CLAUDE.md, memory,
skills), automated checks, tools and their output, permissions, and what
information the agent could reach. The work itself is not on trial; if the
agent made a mistake, the finding is the change that would have prevented or
caught it.

The digest script turns a transcript into a compact evidence sheet so the
retro does not spend its own context on raw JSON:
`python3 ${CLAUDE_SKILL_DIR}/scripts/digest.py`.

## Defaults

| Setting | Default | How to change |
|---|---|---|
| Session | the current one (newest transcript for this directory) | an id or prefix as the argument; `list` to choose |
| Findings | at most 5, ranked | the user asks for more |
| Output | findings in chat, nothing edited | the user picks findings to apply |

## Steps

1. **Pick the session.** With `list`, run `digest.py --list` and let the
   user choose. With an id or prefix, use it. Otherwise use the current
   session. If the session ran in another directory, pass `--project <dir>`.

2. **Digest it**: `python3 ${CLAUDE_SKILL_DIR}/scripts/digest.py [session]`.
   It prints session facts, the steering files in play with their sizes, every
   user message, flags (errors, permission denials, timeouts, repeated calls,
   re-read files, largest outputs, output lines that recur across many calls)
   and a numbered tool timeline. Cite evidence by timeline number (`#42`).
   Read the full transcript only for a specific call the digest cannot
   explain, and only that part.

3. **Read the steering files** the digest lists, plus any skill or memory
   file the session leaned on. A finding about a file you have not read is a
   guess.

4. **Look for candidates** in these categories. Each needs evidence from this
   session, not general advice.

   | Category | Signal in the digest | Typical change |
   |---|---|---|
   | Navigation | many searches or reads before the right file; a file read several times | a pointer in CLAUDE.md or a memory to where things live; a code index |
   | Automated checks | an error the agent made that a linter, type check, test or hook would have caught; a repo with no check wired | wire the existing check into a hook or CI; add a rule to the repo's own linter |
   | Steering files | instructions ignored, contradicted or unused in practice; long files mostly irrelevant to the work; a rule the user had to restate | delete or shorten; move a rule into a check; add the missing rule |
   | Tool economy | large outputs that were mostly skipped; noise lines repeated across calls; hangs and timeouts; slow round trips that one call could have done | filter or silence the output; a script or flag; a different tool |
   | Permissions | denials and prompts that interrupted the flow | an allow rule for safe read-only commands, or confirm the block was right and say so |
   | Information access | the agent guessed, asked, or worked around something it could have read | an MCP, a CLI, a doc pointer, read access to logs |
   | User corrections | a user message that redirects, repeats an instruction, or points out a mistake | a memory (feedback type) or a steering rule, so the next session does not need the correction |

   Classify before proposing. A mechanical rule (a banned pattern, a file
   location, an import shape) gets a check, not prose. A judgment call gets a
   short steering line. A one-off that will not recur gets nothing.

5. **Rank and cut to five.** Order by cost to future sessions: how often it
   will recur times how much it costs each time (tokens, minutes, a wrong
   result, a user interruption). Drop anything you cannot tie to evidence.
   Fewer, sharper findings beat a full list.

6. **Report** in chat, in this shape:

   ```
   Session <id prefix>, <date>, <n> tool calls, <n> user messages.

   1. <title> (<category>)
      Evidence: <#refs and what happened, one or two lines>
      Cost: <what it cost this session, and how often it will recur>
      Change: <the exact edit: file and text, setting, command, or check>
      Effort: <minutes>

   2. ...

   Also noticed, not worth a change: <one line, optional>
   ```

   End by asking which findings to apply. Then stop.

7. **Apply only what the user picks.** Edit steering files and settings only
   after the user picks a finding, and show each change. Memories go through
   the normal memory rules: check for an existing memory before writing a new
   one.

## Rules

- **Evidence or nothing.** Every finding cites timeline numbers or quotes a
  user message. "Consider adding more tests" with no session evidence is not
  a finding.
- **Environment, not blame.** "The agent should have been more careful" is
  not a change. Name the file, setting, check or tool that makes the right
  behaviour the default.
- **Smaller steering files are a win.** Prefer deleting or moving a rule into
  a check over adding prose. Every line in an always-loaded file costs every
  session.
- **Honest about denials.** A permission block that stopped something the
  user then approved is friction worth a rule only if it will recur. A block
  on a genuinely risky action is the system working; say so.
- **The user's own setup choices stand.** If the user decided something in
  this or an earlier session (a tool, a hook, a convention), do not propose
  undoing it unless the evidence shows it costing them; then present the cost
  and let them decide.
- **No secrets in the report.** Transcripts can hold tokens and personal
  data; quote only what the finding needs.
