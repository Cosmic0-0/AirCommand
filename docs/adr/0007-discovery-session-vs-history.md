# Discovery's network table: separate the current scan session from all-time history

**Status: partially superseded by ADR-0010.** This ADR decided on a session-scoped view plus an all-time archive view; neither had been built when ADR-0010 revisited it. ADR-0010 keeps the session-scoped view, drops the all-time archive view and its derived Band column (the operator concluded Targets already cover what is worth keeping), and answers the open `job_id`-vs-timestamp question: neither, because a session now spans several Discovery jobs. The reasoning below is left in place as history.

`Discovery.list_networks()` reads every Network the database has ever seen (`NetworkRepository.all()`), not just the current Discovery job's sightings, and the GUI's `NetworksView` shows that unfiltered. There is currently no way to tell "what I'm seeing right now" from "what showed up at some point in the past." We decided to split this into two views: one showing only the current (or most recent) Discovery session's sightings, and a separate view showing the full historical record — including a derived "Band" column (from channel number, no new stored field), since historical rows can span multiple bands and sessions in a way a single live session's rows never do.

## Why

Surfaced while scoping dual-band support (ADR-0006): once band becomes a per-session choice, a table that silently mixes sightings from different bands and different sessions with no indicator stops being a minor gap and becomes actively misleading — a 2.4GHz network from last week and a 5GHz network from today would sit side by side with only a raw channel number to tell them apart. But the underlying problem — no session/history distinction at all — already exists for 2.4GHz-only usage today; dual-band support just made it visible, it didn't create it.

## Considered Options

- **Leave the table unified, just add a Band column.** Rejected — doesn't address the actual confusion (distinguishing live from stale), only adds one more column to an already-conflated view.
- **Clear the table every time Discovery starts, so it only ever shows the current session.** Rejected — throws away the historical record entirely, which has standalone value (e.g. noticing a network that only shows up intermittently), and would mean scoping persistence itself down rather than just adding a filtered view.
- **Add a second, session-filtered view alongside the existing all-time one, leaving the historical query/table unchanged.** Chosen — `JobRegistry` already mints a `job_id` per Discovery job, so there's no new persistence concept to invent, just a second read path.

## Consequences

- The exact filtering mechanism (tagging sightings with the originating `job_id` vs. a session-start-timestamp cutoff) is an implementation-design question for a later pass, not decided here.
- This applies independent of band and doesn't block or get blocked by ADR-0006 — it can ship on its own schedule.
- The derived Band column belongs on the historical/all-time view only. The current-session view doesn't need one: every row there already shares whatever band was selected at that session's start (ADR-0006), so a per-row column would be redundant there.
