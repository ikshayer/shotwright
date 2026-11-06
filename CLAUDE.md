# CCTV Analog Horror Production Pipeline — Claude

**Read `AGENTS.md` first.** It is the shared contract for every agent in this repository:
the non-negotiable invariants, this machine's measured facts, the working rules and the
layout. It is deliberately the only copy — do not restate its rules here, and if this
file and `AGENTS.md` ever disagree, `AGENTS.md` wins.

What follows is Claude-specific, and additional to it.

## Orchestration

Work is delegated across models. The main session adjudicates; it does not accept a
subagent's report as fact.

- **Opus (main session)** — architectural design, entity invariants, concurrency
  semantics, and all adjudication of review findings.
- **Sonnet** — implementation, tests, moderate refactors.
- **Haiku** — repo facts, mechanical edits, formatting, running tests and reporting.
- **GPT via `codex exec`** — independent review, and implementation where it already
  holds the context. A different model family has different blind spots, which is the
  point; it has repeatedly found defects Claude agents read past.

Rules that have earned themselves:

- **Partition by file.** Never let two agents edit the same module concurrently. Create
  package skeletons before dispatching so agents cannot collide on `__init__.py`.
- **Subagents start cold.** Point them at `AGENTS.md` and the specific PRD sections,
  with exact file paths and an explicit "do not touch" list.
- **Verify, do not trust.** Re-run the tests yourself. Check a claimed enumeration
  against the spec. Reports have been accurate so far; that is not a reason to stop.
- **A reviewer reports, it does not fix.** Tell test and review agents to surface a bug
  rather than correct it, so adjudication stays in one place.
- **Demand the gaps.** Ask every agent what it could not do and what the spec left
  ambiguous. The most valuable lines in their reports are the admissions.

## Invoking Codex

`codex-main` and `codex-alt` are PowerShell functions in the user's profile that only
set `CODEX_HOME`; the PowerShell tool runs `-NoProfile`, so set it directly:

```powershell
$env:CODEX_HOME = "$HOME\.codex-alt"    # or $HOME\.codex
codex exec -m <model> -c model_reasoning_effort="<effort>" `
  --sandbox read-only|workspace-write -C <dir> -o <outfile> "<prompt>"
```

`-o` writes only the final message, so a whole-repo review costs a few lines of context.
`codex-alt` carries the richer tool surface (Playwright, Chrome DevTools, Blender,
context7).

**Models and effort — set both explicitly:**

| Role | Model | Effort |
|---|---|---|
| Implementation, test authoring | `gpt-6-luna` | **`max`** |
| Review | `gpt-6.1-sol` | **`high`** |

**Always pass `-c model_reasoning_effort=` rather than relying on the config.**
`~/.codex-alt/config.toml` sets `model = "gpt-6.1-sol"` and
`model_reasoning_effort = "high"` globally, so a `-m gpt-6-luna` dispatch silently
inherits `high` — which is not what implementation work should get, and which hides the
setting from anyone reading the command. Being explicit also survives a config change.

**Long prompts must be piped in on stdin**, which `codex exec` reads when the prompt
argument is `-`:

```powershell
Get-Content -Raw <promptfile> | codex exec -m <model> -c model_reasoning_effort="<effort>" `
  --sandbox read-only -C <dir> -o <outfile> -
```

Neither a here-string nor `Get-Content -Raw` into a variable passed as an argument
survives: PowerShell 5.1 word-splits a multi-line string containing an apostrophe when
handing it to a native command, and Codex then rejects the fragments — `AGENTS.md`'s own
"Measure, don't assume" was enough to produce `error: unexpected argument 'don't' found`.
Piping avoids argument parsing altogether.

## Tooling note

Bash heredocs in this environment corrupt backslash escapes. Use the `Write`/`Edit`
tools for any content containing `\\`, regex escapes or Windows paths.
