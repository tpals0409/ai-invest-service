# AI Investment Copilot — AI Part

Korean stock market only. Virtual portfolio, no real brokerage.
FastAPI + Postgres/pgvector. Engines compute; the LLM only explains.

## Shared agent contract

Read and follow `AGENTS.md` first. It is the model-independent operating
contract shared by Claude and Codex. Orca, Git, and repository files are the
durable sources of truth; never rely on this Claude session's memory for a
handoff. Project workers must be visible in Orca worktrees and terminals, not
hidden subagents.

## How to work here

**Commit each logical unit.** Never hold more than one unfinished unit
uncommitted. Sessions run out of context and die — committed work survives,
working-tree work does not. This has already cost us two workers.

**Probe before you build.** Before writing a module against an external API or
data source, confirm it actually returns what you expect with a throwaway
script. A worker once built a full ingester on an endpoint that silently
returned empty results.

**Find the existing source of truth first.** Before inventing a vocabulary,
constant, or schema, grep the repo for one. Sector names live in
`ingest/ksic_sectors.json`. Enums live in `app/core/enums.py`. Response types
live in `app/core/schemas.py`.

**Read narrowly.** Design docs are in `docs/*.md` — never open the `.html`
twins, they are half CSS. Read the section you were pointed at, once. For
anything broader than a single file, use a separately scoped Orca-visible
worker when parallel exploration is actually useful.

**Keep a progress note.** Append one line to `.worker/progress.md` after every
commit and every decision you would not want to make twice — what is done,
what is next, which paths matter. Two sentences, not a report. `.worker/` is
ignored scratch, so also put durable handoff facts in the Orca worktree comment,
worker prompt, or a commit.

**After a compaction, read that note first.** Then `git log` and `git status`.
Re-read a source file only if the note and the diff cannot answer your
question. Workers have burned entire sessions re-reading the same four files
after every compaction; the note exists to make that unnecessary.

## Definition of done

```
pytest -q          # all green, including tests you did not write
ruff check .       # clean
alembic check      # no model/migration drift, if you touched models
git commit         # conventional commits, see CONTRIBUTING.md
gh pr create       # base main
```

## Boundaries

- `app/core/models.py` and `alembic/` are shared. Do not edit unless the task
  says so — a schema change collides with every other track.
- Stay inside the directories your task names. Other workers run in parallel.
- Never commit `.env`, credentials, or bulk ingested data.

## Output language

Write **commit messages, PR bodies, code comments, and docstrings in Korean** —
the team reads them. Explain *why*, not *what*; the diff already says what.

Your reports to the coordinator are English.
