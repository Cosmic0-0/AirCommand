"""TargetSelector — reusable Target-picking dropdown. See
docs/design/gui-structure.md 'Target Actions tab' ("TargetSelector (reusable —
also used by Audit Log's filter, below)"). Method names (upsert_row/remove_row)
match TargetPicker's (discovery_view.py) for naming consistency across this
codebase's two "live list of Targets" views, even though this one renders as a
dropdown, not a table.
"""

from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from aircommand.core import BSSID, Target


class TargetSelector(ctk.CTkFrame):
    """A dropdown seeded from engine.targets.list(), kept live via
    upsert_row(TargetAdded)/remove_row(TargetRemoved). `include_all_option`,
    when True, adds an extra "All Targets" choice mapping to `None` in the
    callback -- not used by this slice (Target Actions tab doesn't need it),
    kept here since the design doc calls this class out as reusable by a later
    slice (Audit Log's filter).
    """

    _NO_TARGETS_LABEL = "No targets"
    _ALL_TARGETS_LABEL = "All Targets"

    def __init__(
        self,
        master,
        app,
        on_change: Callable[[Optional[Target]], None],
        include_all_option: bool = False,
    ) -> None:
        super().__init__(master)
        self._app = app
        self._on_change = on_change
        self._include_all_option = include_all_option
        self._targets_by_label: dict[str, Optional[Target]] = {}  # label -> Target, or None for "All Targets"
        self._selected_label: Optional[str] = None

        ctk.CTkLabel(self, text="Target:").pack(side="left", padx=(0, 5))
        self._menu = ctk.CTkOptionMenu(self, values=[self._NO_TARGETS_LABEL], command=self._on_menu_selected)
        self._menu.pack(side="left")

        self._rebuild(app.engine.targets.list())

    def upsert_row(self, event) -> None:
        self._add_or_update(event.target)

    def remove_row(self, event) -> None:
        self._remove(event.bssid)

    def _label_for(self, target: Target) -> str:
        return f"{target.ssid} ({target.bssid})"

    def _select(self, label: str) -> None:
        """Updates the widget's displayed value and our own bookkeeping,
        without calling self._on_change -- callers decide separately whether
        this particular selection change is one that should notify."""
        self._selected_label = label
        self._menu.set(label)

    def _rebuild(self, targets: list[Target]) -> None:
        self._targets_by_label = {}
        if self._include_all_option:
            self._targets_by_label[self._ALL_TARGETS_LABEL] = None
        for target in targets:
            self._targets_by_label[self._label_for(target)] = target

        self._menu.configure(values=list(self._targets_by_label.keys()) or [self._NO_TARGETS_LABEL])

        if self._include_all_option:
            initial_label = self._ALL_TARGETS_LABEL
        elif targets:
            initial_label = self._label_for(targets[0])
        else:
            initial_label = self._NO_TARGETS_LABEL

        self._select(initial_label)
        self._on_change(self._targets_by_label.get(initial_label))

    def _add_or_update(self, target: Target) -> None:
        new_label = self._label_for(target)

        stale_label = next(
            (label for label, existing in self._targets_by_label.items()
             if existing is not None and existing.bssid == target.bssid and label != new_label),
            None,
        )
        if stale_label is not None:
            del self._targets_by_label[stale_label]
            if self._selected_label == stale_label:
                # Same Target, just relabelled (e.g. ssid changed) -- keep the
                # selection pointed at it and keep the widget's displayed text
                # in sync, but this isn't a selection change: no on_change call.
                self._selected_label = new_label
                self._menu.set(new_label)

        self._targets_by_label[new_label] = target
        self._menu.configure(values=list(self._targets_by_label.keys()))

    def _remove(self, bssid: BSSID) -> None:
        stale_label = next(
            (label for label, existing in self._targets_by_label.items()
             if existing is not None and existing.bssid == bssid),
            None,
        )
        if stale_label is None:
            return

        was_selected = stale_label == self._selected_label
        del self._targets_by_label[stale_label]

        self._menu.configure(values=list(self._targets_by_label.keys()) or [self._NO_TARGETS_LABEL])

        if was_selected:
            remaining_targets = [t for t in self._targets_by_label.values() if t is not None]
            if self._include_all_option:
                new_label = self._ALL_TARGETS_LABEL
            elif remaining_targets:
                new_label = self._label_for(remaining_targets[0])
            else:
                new_label = self._NO_TARGETS_LABEL
            self._select(new_label)
            self._on_change(self._targets_by_label.get(new_label))

    def _on_menu_selected(self, label: str) -> None:
        self._selected_label = label
        self._on_change(self._targets_by_label.get(label))
