# AI Investment Copilot — AI Part

Korean stock market only. Virtual portfolio, no real brokerage.
FastAPI + Postgres/pgvector. Engines compute; the LLM only explains.

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
anything broader than a single file, delegate to an Explore subagent so the
file dumps land in its context, not yours.

**Do not re-derive what you already committed.** After a compaction, check
`git log` and `git status` before re-reading files.

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
