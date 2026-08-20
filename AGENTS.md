# AI Investment Copilot — Agent Operating Contract

This repository is developed with both Codex and Claude. Orca, Git, and the
repository are the durable sources of truth; no task may depend on one model's
private memory or a particular chat session.

## Model independence

- Write task briefs so either Codex or Claude can continue them.
- Do not require model-specific commands, tools, prompt syntax, model names, or
  reasoning-effort settings unless the user explicitly asks for them.
- Treat Codex and Claude as interchangeable workers. Record decisions in Orca
  and Git, not in model memory.
- Do not create hidden or in-process workers for project work. Every worker must
  be visible as an Orca worktree and terminal so a later session can inspect,
  resume, or stop it.

## Orca is the control plane

- Create, resume, inspect, and remove workers through Orca.
- Give each independent task its own Orca worktree. Use a new terminal in an
  existing worktree only when continuing that exact task.
- Keep the Orca worktree comment and workspace status current at meaningful
  checkpoints: started, implementation complete, validation, blocked, PR open,
  merged, and completed.
- Before sending input to a worker, read its current terminal. Before declaring
  a worker dead or duplicating it, inspect the terminal output and Git state;
  a quiet worker may still be reading or reasoning.
- Do not remove a worktree until its changes are committed or intentionally
  discarded and any required PR is merged.

## Resume protocol

At the start of every new session or after context compaction, recover state in
this order:

1. Identify the current Orca worktree, status, comment, and live terminals.
2. Read the active worker terminal's latest output.
3. Run `git status`, `git log`, and inspect any current diff.
4. Read the task brief and only the relevant design-document sections.
5. Read `.worker/progress.md` if it exists, then continue from the first
   unfinished acceptance criterion.

Do not restart discovery from scratch when these sources already answer the
question.

## Durable checkpoints and handoff

- Commit each logical unit. Never leave more than one logical unit uncommitted.
- After every commit or non-obvious decision, update the Orca comment and append
  a concise line to `.worker/progress.md` when that file exists.
- `.worker/` is ignored local scratch. It is useful inside the same worktree but
  is not sufficient for cross-worktree recovery. Durable facts must also live
  in commits, an Orca comment, or a self-contained Orca worker prompt.
- Before ending a session with unfinished work, leave all of the following:
  current status, completed work, remaining acceptance criteria, validation
  already run, known blockers, and the exact next action.
- A handoff prompt must be self-contained. Include scope, owned paths,
  acceptance criteria, constraints, current commit or diff state, and required
  verification. Do not merely point at a large reference file.

## Worker boundaries

- Workers are not alone in the repository. Assign explicit file or module
  ownership and do not revert another worker's changes.
- `app/core/models.py` and `alembic/` are shared surfaces. Edit them only when
  the task explicitly assigns schema ownership.
- Preserve user changes and unrelated dirty files.
- Never commit `.env`, credentials, secrets, or bulk ingested data.

## Validation and integration

Unless the task narrows the validation scope, completion requires:

```text
pytest -q
ruff check .
alembic check  # when models or migrations are involved, or before integration
git status     # clean after commits
```

Use Podman for local containers; do not start Docker Desktop. On macOS, start a
Podman machine from a persistent Orca terminal when a short-lived automation
shell would terminate its AppleHV child process. Stop validation containers and
the Podman machine after use unless the next task needs them.

For integration: push the task branch, open a PR against `main`, wait for CI,
merge using the repository's configured strategy, fast-forward local `main`,
rerun proportional verification, mark the Orca worktree completed, and remove
the worker only after the merged result is recoverable from `main`.

## Project conventions

- Korean stock market only; virtual portfolio, no real brokerage.
- FastAPI + PostgreSQL/pgvector. Engines compute; the LLM explains.
- Read `docs/*.md`, not generated `.html` twins, unless visual output is the
  task. Read only the sections needed.
- Find existing vocabularies, constants, schemas, and patterns before adding
  new ones.
- Write commit messages, PR bodies, code comments, and docstrings in Korean.
  Explain why; the diff already shows what changed.
- Follow `CONTRIBUTING.md` for branch, commit, and PR conventions.
