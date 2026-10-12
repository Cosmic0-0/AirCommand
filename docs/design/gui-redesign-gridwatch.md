# GUI redesign handoff: Grid Watch × Wireframe A

Written for a fresh session with zero visibility into the chat that produced this
design. Read this whole document before writing any code. It is the final,
approved spec — no rejected alternatives, no exploration history.

**A naming collision to get out of the way first:** `docs/roadmap.md` uses
"Phase 1 / Phase 2 / Phase 3" for the *original* build (core engine, real
hardware, the existing GUI) — all three are already complete, see §1. This
handoff uses "Phase 1 / Phase 2" for a completely separate, two-phase framing
of the *redesign project*: **Phase 1** (this task) is reimplementing the design
below in CustomTkinter. **Phase 2** (not this task, not scoped here) is later
recommending/porting the GUI to a different, better-suited framework. Don't
conflate the two numbering schemes — they're unrelated.

## 0. Before you start — two things need a new ADR, not just code

Cross-checking this design against the already-settled core architecture
surfaced two real conflicts. Both were reviewed and the decision in both cases
was **keep the new design, write a new ADR to supersede the old one** — not
silently reinterpret the mockup to fit existing behavior. Do this core-side
work (and get the ADRs written) before or alongside building the affected
pages, not after.

1. **The Management page's adapter select + Check/Kill/Start/Stop controls
   have no backing core API, and directly contradict
   `docs/adr/0005-networkmanager-check-kill.md`**, which decided
   `airmon-ng check kill` and the NetworkManager restart happen automatically
   inside `RadioController`, "instead of leaving both to the operator."
   `RadioController._start_monitor_mode`/`_stop_monitor_mode` exist today only
   as private methods, invoked automatically whenever Discovery/Capture/
   Enumerate reserve the radio. `Engine.__init__` takes `adapter: str` as a
   fixed constructor argument — there is no public method to list available
   adapters, check for a conflicting process, kill one, or explicitly start/
   stop monitor mode. Building this page means designing and landing that
   public API first (most likely new methods on `RadioController` or a new
   thin facade off `Engine`), and writing a new ADR that supersedes ADR-0005's
   "automatic, not operator-driven" decision for this one flow.
2. **The Discovery page's band-gating (review-feedback item 4, §6) contradicts
   `docs/adr/0013-discovery-band-selection.md`**, which is already implemented
   on `feat/dual-band-discovery` and verified against real hardware. ADR-0013
   decided the Band dropdown **defaults to the most inclusive supported
   choice** (pre-selected, not empty) and that Start Discovery is simply
   enabled once Idle — not additionally gated on an explicit band pick. This
   design instead wants the dropdown to open unchosen and Start to stay
   disabled until a band is picked. Keep the new behavior, but write a new ADR
   superseding ADR-0013's default/gating decision, and re-run real-hardware
   verification under the new behavior — ADR-0013's existing hardware
   verification was done against the old default/gating, so it doesn't carry
   over automatically.

Everything else in this design is compatible with the existing, shipped
architecture — see §5 for the full page-by-page mapping.

## 1. What already exists — this is a redesign, not a green-field build

Per `docs/roadmap.md`'s "Current state": **Phases 1–3 of the original build
are complete.** The core engine (`aircommand/core/`) is fully implemented and
tested against real hardware and real `sudo`. The GUI (`aircommand/gui/`) is
also fully implemented — a real, working `ctk.CTkTabview` with four tabs
(Discovery & Targets, Target Actions, Crack, Audit Log) plus a docked
`StatusBar`, wired to the real `Engine`, specified in full in
`docs/design/gui-structure.md` and `docs/design/core-gui-boundary.md`. 194
tests pass, including real `ctk`/`tkinter` widget interaction. The only thing
nobody has done yet is drive it end-to-end against real hardware with real
`sudo` in an interactive terminal — that's the user's own hands-on task, not
part of this redesign.

**Read `docs/design/gui-structure.md` and `docs/design/core-gui-boundary.md`
in full before writing code.** They specify the exact event subscriptions,
`Engine` method calls, state machines, and gotchas (e.g. "whatever `on_change`
touches must already exist by construction time") already correct in the
shipped code. This redesign changes the **visual style and page layout only**
— the sidebar-nav shell, the Grid Watch look, the five-page structure, and the
layout changes in §6. It does not change what `Engine` methods each view
calls, what events each view subscribes to, or any gating/state-machine logic
that already exists and isn't explicitly called out below as changed. Port the
existing wiring into the new layout; don't redesign it.

Existing GUI source (for reference — restructure these, don't rewrite from
scratch): `aircommand/gui/app.py`, `discovery_view.py`, `target_actions_view.py`,
`capture_view.py`, `enumerate_view.py`, `crack_view.py`, `audit_log_view.py`,
`status_bar.py`, `sudo_dialog.py`, `event_pump.py`, `target_selector.py`,
`table.py`.

## 2. Visual reference — the actual approved design, not just this prose

- **Live, rendered mockup:** <https://claude.ai/artifact/3kBFpG1iiFjnwBdbt6u2FQ>
  — a private Claude Artifact (Design canvas), board labelled "Grid Watch ×
  Wireframe A." Open it directly for the real interactive preview (sidebar
  collapse, page switching, button gating all actually work in it).
- **Exact source, copied into this repo:**
  [`docs/design/mockups/gridwatch-wireframe-a.dc.html`](mockups/gridwatch-wireframe-a.dc.html)
  — the literal, byte-for-byte approved markup/CSS/state logic. Every hex
  code, spacing value, and gating rule in this document is drawn directly from
  this file; treat it as the authoritative style/structure source. It's
  authored in Claude's internal Design Component mockup format (custom
  `<x-dc>`/`<sc-if>`/`<sc-for>` elements resolved by a runtime this file
  doesn't carry) — opening it in a plain browser will **not** render
  pixel-perfect (template holes like `{{management.statAdapter}}` will show as
  literal text). Use the live artifact link for a visual preview; use this
  file for exact values.

## 3. Visual style — "Grid Watch"

**Palette** (CSS custom properties, use these exact values):

```css
--bg:      #0a0b0d   /* page background */
--rail:    #0d0f12   /* sidebar + statusbar background, slightly lighter than bg */
--panel:   #121418   /* card/cell background */
--border:  #262b30   /* all hairline borders */
--text:    #dde2e6   /* primary text */
--muted:   #7d868f   /* secondary text, labels, inactive icons */
--accent:  #b4d94a   /* lime — active-state indicator, primary buttons, links */
--danger:  #c1544a   /* deauth section, destructive actions */
--success: #49ab7a   /* monitor-mode-on / live / active indicators */
--warn:    #d99a3f   /* flagged rows, e.g. "Interrupted by crash" */
```

**Typography:** two Google Fonts families, no others.
- **Hanken Grotesk** (weights 400/500/600/700) — all UI text: nav labels, page
  titles, body copy, buttons.
- **Fira Code** (weights 400/500/600) — all data/mono contexts: adapter names,
  MAC addresses, table cell data, status pills, the statusbar, stat values.

**Shape:** a single 2px border-radius everywhere — cells, buttons, inputs,
pills, chips. No exceptions (this is a deliberate "shape consistency lock,"
not an oversight to "fix").

**Core component patterns** (reuse these exactly; don't invent new ones):

| Component | Pattern |
|---|---|
| `.cell` | The atomic content container. Panel background, 1px border, 2px radius, flex column. Every page's content is built from `.cell`s inside a 12-column `.dash-grid` (`gap: 8px`, `span-4`/`span-6`/`span-8`/`span-12` utility widths). |
| `.cell-head` | Cell's title bar: title (11.5px/600) on the left, a literal "⋮" menu glyph on the right, bottom border separating it from `.cell-body`. |
| `.cell-body` | Padded content area (12px/14px), flex-grow. |
| `.btn` / `.btn-outline` / `.btn-danger` | Flat bordered buttons, 2px radius, 12px/600 text. Primary actions (Start Discovery, Start Airmon-ng, Start Crack) use an inline `background:var(--accent);border-color:var(--accent);color:#0a0b0d` style rather than a dedicated class. `:disabled` drops opacity to .35 — this is the **only** visual signal for a gated/disabled control, no separate "locked" iconography. |
| `.status-pill` | Small bordered Fira Code label, muted by default, success-green when `.active` (used both for "Sudo session active" in the statusbar and LIVE/OFF adapter indicators). |
| `.adapter-row` | Clickable row: a 14×14 `.sig-bar` box containing a 6×6 `.dot` as the *selection* indicator (accent-filled when selected, transparent otherwise — not a live signal-strength meter despite the name), adapter name + description, and a status pill. Selecting one sets its border to accent via inline style. |
| `.table` | Plain bordered table — muted 10.5px headers, 11.5px Fira Code data cells, bottom-border row dividers, no zebra striping. |
| `.tag` / `.tag-warn` | Small bordered inline label; `.tag-warn` (amber) flags rows like "Interrupted by crash." |
| `.chip` | Target allowlist entries — bordered Fira Code pill with an inline "×" remove button that turns danger-red on hover. |
| `.field` | Label + input/select wrapper — muted 11.5px label above a dark (`var(--bg)`) bordered input. |

**Sidebar specifics:** 196px wide expanded, 54px collapsed, `.14s ease` width
transition. Header: inline wifi-mark SVG + "AIRCOMMAND" wordmark + a
chevron-shaped collapse toggle (rotates 180° when collapsed). Nav items use a
small 6×6px square "dot" indicator next to each item — muted by default,
accent-filled when that page is active — instead of changing icon color; each
item also carries a 14×14 muted-stroke SVG icon. Collapsed mode hides the dot
and label, centers the icon only. The sidebar holds four main nav items
(Management, Discovery & Targets, Capture & Attack, Cracking) in a `<ul>`, and
a fifth (Logs) pinned in a separately-bordered footer slot below — visually
set apart, but functionally just a fifth nav destination, same click behavior.

## 4. Structural layout — "Wireframe A," as finalized

**Overall shell (every page):** sidebar (above) on the left; a main column on
the right containing the page content area, then a plain statusbar pinned to
the bottom. **There is no tab-strip above the content** and **no dockable
log/output panel below it** — both existed in the original Wireframe A
reference (VS Code's own layout) and were explicitly removed, see §6 items 1
and 2. The statusbar is the only persistent bottom chrome: a "Sudo session
active" status pill plus the active adapter's label, both Fira Code, 11px.

**Content area:** `overflow: auto` at the `.content` level — the whole page
scrolls as one unit. No page puts its own `overflow`/fixed-height scroll box
inside an individual cell (see §6 item 3) — if a table or list grows past the
viewport, the *page* scrolls, not a sub-box.

## 5. Page-by-page spec and core/GUI mapping

| Page | Maps to existing view(s) | Status |
|---|---|---|
| Management | **None — net new**, see §0 item 1 | Needs new core API + new ADR |
| Discovery & Targets | `NetworksView` + `TargetPicker` (`discovery_view.py`) | Existing wiring applies; band-gating needs §0 item 2 |
| Capture & Attack | `TargetSelector` + `CapturePanel` + `EnumeratePanel` (`target_actions_view.py`, `capture_view.py`, `enumerate_view.py`) | Existing wiring applies; layout changed per §6 item 5 |
| Cracking | `HandshakePicker` + `WordlistPicker` + `CrackPanel` (`crack_view.py`) | Existing wiring applies, restyle only |
| Logs | `AuditLogView` (`audit_log_view.py`) | Existing wiring applies, restyle only — see note below |

**Management.** Page copy: "Select the adapter, then step its monitor-mode
cycle by hand." Three stat cells (`span-4` each): Adapter (selected adapter
name, or `--`), Monitor mode (ON/OFF), Session time. Two cells (`span-6`
each): **Adapter select** — a list of `.adapter-row`s (the mockup's two
illustrative rows, `wlan0`/"Realtek RTL8812AU, USB"/live and `wlan1`/"Intel
AX200, internal"/not live, are placeholder content, not a literal product
requirement — the real list comes from whatever the new adapter-listing API
in §0 item 1 returns); and **Monitor-mode controls** — Check / Kill
Conflicting Process / Start Airmon-ng / Stop Airmon-ng buttons plus a
"last action" status line. Gating (reuses the same precondition-disable
pattern as everywhere else in this app): Check, Kill, and Start are disabled
until an adapter is selected; Start additionally disables and relabels to
"Airmon-ng Running" once monitor mode is active; Stop is disabled until
monitor mode is actually running.

**Discovery & Targets.** Page copy: "Passive beacon listening, open to any
network. A network must join the Target allowlist before Capture or Action
can run against it." One `span-12` Session cell: Start Discovery + New
Session buttons, and a Band `<select>`. **Per §6 item 4 / §0 item 2:** the
select opens on a disabled placeholder ("Choose a band…"), nothing
pre-selected; Start Discovery stays disabled until a real band option (2.4 +
5 GHz / 2.4 GHz / 5 GHz) is picked. Below that: a `span-8` Networks seen cell
and a `span-4` Target allowlist cell — **per §6 item 3, neither has an
internal fixed-height scroll any more; both grow with the page.**

**Capture & Attack.** Page copy: "Capture and transmission are gated to
Targets. Every deauth firing is logged." A Target `<select>` at the top, then
**three independent, stacked, full-width (`span-12`) sections, in this order**
(the layout decided for §6 item 5): (1) **Capture** — passive capture
start/cancel; (2) **Deauth** — danger-bordered, red title, its own "Fire
Deauth" button and warning copy; (3) **Enumerate**, visually and structurally
separate from the Capture/Deauth group above it — its own hint copy ("Requires
this adapter already associated via your OS's wifi settings") and its own
"Start Enumerate" button. Capture and Deauth stay grouped near each other
(both route through the same `CapturePanel`/radio-reservation path); Enumerate
no longer shares a row or grid with either.

**Cracking.** A `span-12` Handshake table, then a `span-6` Wordlist picker and
a `span-6` Crack cell (progress + Start, gated on both a handshake and a
wordlist being selected).

**Logs.** A `span-12` Transmission log table, including the `tag-warn`
"Interrupted by crash" row styling. **Note:** the mockup shows a flat content
sample — it does **not** visually include the `TargetSelector`-driven filter
("All Targets" default) or the persistent interrupted-deauth banner that
`docs/design/gui-structure.md`'s `AuditLogView` spec already requires. Both
are pre-existing, already-decided requirements and must be built into this
page even though the mockup doesn't show them; they just weren't part of what
this round of visual/layout feedback touched.

## 6. The five review-feedback changes (final state, for traceability)

1. **Removed the redundant top tab/label.** The old Wireframe A had a
   `.tab-strip` showing the current page as a single active tab, above the
   content area. Removed — the sidebar's active nav item already shows this.
2. **Removed the persistent bottom "Logs (live tail)" panel.** The old
   Wireframe A had a toggleable, tabbed dock ("Logs (live tail)" / "Output")
   between the content area and the statusbar. Removed entirely. The plain
   statusbar (sudo pill + adapter label) is the only thing left at the bottom.
3. **Networks seen / Target allowlist expand with page scroll.** No internal
   fixed-height scroll box on either cell; the whole page scrolls instead.
4. **Discovery start gated on band selection.** Band `<select>` defaults
   unchosen; Start Discovery is disabled until a band is explicitly picked.
   **Conflicts with already-verified ADR-0013 — see §0 item 2.**
5. **Capture/Deauth separated from Enumerate.** Previously paired side-by-side
   in a shared two-column row; now three independent stacked full-width
   sections (Capture, then Deauth, then — separately — Enumerate), per the
   layout decided in §5.

## 7. Phase 1 vs Phase 2 (the redesign project's own framing)

**Phase 1 — this task.** Implement the design in this document in
CustomTkinter, replacing the existing tabview-based shell in `aircommand/gui/`
with the sidebar-nav Grid Watch × Wireframe A design, including the new
Management page and the two ADR-driven core changes in §0. This is a visual
and structural restructuring of an existing, working, tested GUI — not a
rewrite of its Engine-wiring logic (§1).

**Phase 2 — explicitly out of scope here.** Later, recommend and likely port
this GUI to a different, better-suited GUI framework than CustomTkinter. Do
not start this, scope it, or let it influence implementation choices in
Phase 1 — it's a separate future task.

## 8. Suggested branch and implementation checklist

**Branch:** `feat/gridwatch-gui-redesign`

**Checklist:**

- [ ] Write the new ADR for Management's adapter/monitor-mode API (§0 item 1);
      land the public `RadioController`/`Engine` methods it specifies (list
      adapters, check conflicting process, kill it, explicit start/stop
      monitor mode) before wiring the Management page's buttons to them.
- [ ] Write the new ADR superseding ADR-0013's band default/gating (§0 item 2);
      re-verify on real hardware once the new default/gating behavior lands.
- [ ] Replace `ctk.CTkTabview`'s 4-tab shell with a collapsible icon+label
      sidebar (Management, Discovery & Targets, Capture & Attack, Cracking,
      Logs). Keep `StatusBar` docked at the bottom; do not reintroduce a
      tab-strip above content or any dockable panel below it (items 1–2).
- [ ] Apply the Grid Watch token set (§3) across every view — colors,
      typography, 2px-radius shape rule, and the shared component patterns
      (`.cell`, `.dash-grid`, buttons, status-pill, table, chip, adapter-row).
- [ ] Build the new Management view (adapter select + Check/Kill/Start/Stop),
      wired to the new core API from the first checklist item.
- [ ] Port `NetworksView`/`TargetPicker` into the new Discovery & Targets page;
      remove any internal fixed-height scroll (item 3); apply the new
      band-default/gating behavior once its ADR lands (item 4).
- [ ] Port `CapturePanel`/`EnumeratePanel` into the new Capture & Attack page
      as three independent stacked sections — Capture, Deauth, Enumerate —
      not a shared two-column row (item 5).
- [ ] Port `HandshakePicker`/`WordlistPicker`/`CrackPanel` into the new
      Cracking page, restyled only — no logic changes.
- [ ] Port `AuditLogView` into the new Logs page, restyled only, **including**
      its `TargetSelector` filter and interrupted-deauth banner (§5 note) even
      though the mockup's flat content sample doesn't show either.
- [ ] Re-run the existing 194-test suite; update any test that asserts on the
      old `CTkTabview`/4-tab structure to match the new sidebar shell.
- [ ] Leave WEP/WPS/evil-twin scope boundaries (ADR-0001) and the sudo/
      privilege flow (ADR-0002) untouched — out of scope for this redesign.
