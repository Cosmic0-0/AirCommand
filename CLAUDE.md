# CLAUDE.md

AirCommand is a Python wifi-auditing orchestrator: a CustomTkinter GUI wrapper around aircrack-ng, hashcat, and nmap, adding workflow, tracking, and authorization enforcement on top. See `CONTEXT.md` for domain vocabulary and `docs/adr/` for design decisions — read both before making architectural or scope changes.

## Stack

Python, CustomTkinter GUI (not plain Tkinter — CustomTkinter specifically, for a modern widget set on top of the same Tkinter foundation the user already has experience with). SQLite for structured data (seen networks, the Target allowlist, crack results) plus a working directory for binary artifacts (captures, wordlists). Execution target is Linux only (Linux Mint primary, Kali VM fallback). Windows is edit-only — no Windows compatibility work is needed for AirCommand itself.

## Architecture

Core engine (discovery, capture, cracking, allowlist enforcement) is a GUI-agnostic package; the CustomTkinter GUI is a thin layer that calls into it. This separation is settled — exact module boundaries and interfaces are designed separately (see `/architect`), not redecided per task.

## Scope & safety

Discovery (passive beacon listening) is open to any network. Any Action — capture (passive or active) or transmission (deauth) — is gated to Targets: networks on the authorization allowlist. WEP cracking, WPS attacks, and evil-twin creation are deliberately excluded from the roadmap, not just deprioritized — see `docs/adr/0001-scope-boundaries.md` before reintroducing anything like them. Privilege elevation is a launch-time sudo prompt with a session-scoped keepalive, not setcap or a root-run GUI — see `docs/adr/0002-privilege-elevation.md`.

## Dev workflow: model tiering

This repo deliberately splits work across model strength:

- **Planning, architecture, and review** — module boundaries, interface/type design, debugging non-obvious issues, and reviewing/fixing implementation output — stay with the strongest available model (Opus, or Plan mode). Don't delegate these.
- **Routine implementation** — writing a function/class to an already-decided interface, boilerplate, straightforward tests — gets dispatched to a cheaper subagent (Sonnet or Haiku via the Agent tool) once the interface and scope are already pinned down. Give it a self-contained prompt: exact file paths, the signatures/types decided during planning, and what's explicitly out of scope.
- **Every dispatched implementation gets reviewed before being called done.** Read the actual diff, not just the subagent's summary — check it against the interface that was planned, fix or redo anything that drifted.

Rule of thumb: if the task requires deciding *how* something should be structured, do it directly. If the structure is already decided and the task is just *writing it*, dispatch it.

## Dev workflow: commits and pushes

Claude does not run `git add`, `git commit`, `git push`, or anything else that would create a commit or push to a remote in this repo — not even when explicitly asked to commit. Whenever a commit or push would otherwise happen, instead:

1. Give a short summary of the change and a suggested commit message.
2. Give the exact command(s) to run, staging included (e.g. `git add <files>`, `git commit -m "..."`, `git push` as applicable).

The user runs the command(s) themselves and reports back when done — treat the change as uncommitted until they do.

## Dev workflow: updating the README

`README.md` is the GitHub landing page. Keep it accurate, not just well-written.

- Run `/unslop` on any edit to it. Run `/technical-writing` too whenever restructuring
  prose, not just fixing a typo. It sorts each section into a Diátaxis mode and
  holds it there: Features/Requirements/Status describe and don't instruct; Install
  & run/Development are commands with the condition stated before the step;
  Authorized use only/Architecture explain why, linking to the relevant ADR for
  depth instead of restating it.
- Verify every factual claim against the repo itself, not memory or the previous
  README: run `--help` for the real flags, read `pyproject.toml` for deps and the
  Python floor, read the actual `argv` construction in `crack.py`/`enumerate.py`/etc.
  for which tool does what, and check `docs/final-touches.md` for what's verified on
  real hardware versus still open.
- Verify every link and anchor resolves to a real path or heading in this repo.
  Don't assume a doc still has the section a prior README version pointed at.
- Grep for em dashes and en dashes (`—`/`–`) before calling it done — `/unslop` bans
  both.
- Keep the README short and link out rather than duplicate: the full tab-by-tab
  walkthrough lives in `docs/usage.md`, domain vocabulary in `CONTEXT.md`, design
  rationale in `docs/adr/`. If an edit would copy more than a sentence from one of
  those, link to it instead.
- This falls under the commit/push rule above same as any other change: don't
  commit or push it yourself.

## Repository hygiene

- Preserve unrelated work in a dirty worktree.
- Do not commit `.env`, database files, trained OCR data, generated build output,
  or secrets.
- Commits and pull requests are attributed to the human pushing them. Do not add
  AI co-author trailers.
