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
