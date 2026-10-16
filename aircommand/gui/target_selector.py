"""TargetSelector: a reusable Target-picking dropdown for the PySide6 port. See
docs/design/gui-structure.md "Target Actions tab" ("TargetSelector (reusable,
also used by Audit Log's filter, below)") and
docs/design/gui-port/05-boundary-design.md "View mapping" (the target_selector
row: CTkOptionMenu becomes a QComboBox plus a label).

Method names (upsert_row / remove_row) match TargetPicker's (discovery_view.py)
for naming consistency across this codebase's two live-list-of-Targets views,
even though this one renders as a dropdown rather than a table. The bssid-keyed
label bookkeeping, the seed from engine.targets.list(), and the selection
fallback logic port as is from the CustomTkinter original; only the widget layer
is new.

Both call sites now keep this live: TargetActionsView wires TargetAdded /
TargetRemoved to upsert_row / remove_row itself (target_actions_view.py), and the
Logs page's reuse with include_all_option=True gets the same wiring in
App._build_logs_page. The seed from engine.targets.list() still stands on its own,
so the selector also works when a parent never registers those events.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from aircommand.core import BSSID, Target


class TargetSelector(QWidget):
    _NO_TARGETS_LABEL = "No targets"
    _ALL_TARGETS_LABEL = "All Targets"

    def __init__(
        self,
        app,
        on_change: Callable[[Optional[Target]], None],
        include_all_option: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._on_change = on_change
        self._include_all_option = include_all_option
        self._targets_by_label: dict[str, Optional[Target]] = {}  # label -> Target, or None for "All Targets"
        self._selected_label: Optional[str] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel("Target:")
        label.setProperty("class", "fieldLabel")
        layout.addWidget(label)
        self._menu = QComboBox()
        # Resize to the current items, not just the width of the first value
        # shown. Without this the combo keeps the narrow width of the initial
        # "No targets" placeholder and elides a real Target label (the BSSID is
        # the identifier the operator checks before firing deauth, so it must be
        # readable in the closed state).
        self._menu.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        # textActivated fires only on a user pick from the dropdown, not a
        # programmatic setCurrentText(). That reproduces CTkOptionMenu's command=
        # (which .set() never triggered), so _select() can set the displayed value
        # without calling on_change, exactly as the Tk version relied on.
        self._menu.textActivated.connect(self._on_menu_selected)
        layout.addWidget(self._menu)
        layout.addStretch(1)

        self._rebuild(app.engine.targets.list())

    def upsert_row(self, event) -> None:
        self._add_or_update(event.target)

    def remove_row(self, event) -> None:
        self._remove(event.bssid)

    def _label_for(self, target: Target) -> str:
        return f"{target.ssid} ({target.bssid})"

    def _set_menu_values(self, labels: list[str]) -> None:
        # clear() and addItems() change currentIndexChanged / currentTextChanged,
        # which this widget never connects, so neither fires on_change.
        self._menu.clear()
        self._menu.addItems(labels)

    def _select(self, label: str) -> None:
        """Update the displayed value and our own bookkeeping, without calling
        self._on_change: callers decide separately whether a given selection
        change should notify. setCurrentText is a no-op for a non-editable combo
        when the label is not an item, so callers set the items first."""
        self._selected_label = label
        self._menu.setCurrentText(label)

    def _rebuild(self, targets: list[Target]) -> None:
        self._targets_by_label = {}
        if self._include_all_option:
            self._targets_by_label[self._ALL_TARGETS_LABEL] = None
        for target in targets:
            self._targets_by_label[self._label_for(target)] = target

        self._set_menu_values(list(self._targets_by_label.keys()) or [self._NO_TARGETS_LABEL])

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
                # Same Target, just relabelled (e.g. ssid changed): keep the
                # selection pointed at it, but this isn't a selection change, so
                # no on_change call.
                self._selected_label = new_label

        self._targets_by_label[new_label] = target
        self._refresh_menu_items()

    def _refresh_menu_items(self) -> None:
        """Rebuild the combo items from the current Targets and re-assert the
        displayed selection without firing on_change. If the selection is a
        display-only placeholder ("No targets" or "All Targets", which are not
        real Target labels in _targets_by_label), keep it as the first item so
        the combo keeps showing it. A pump event never changes what is displayed
        or bound: before this, adding the first Target dropped the placeholder
        from the items, so setCurrentText("No targets") became a no-op and the
        combo showed a Target that was never selected (on_change never fired)."""
        labels = list(self._targets_by_label.keys())
        if self._selected_label is not None and self._selected_label not in labels:
            labels = [self._selected_label, *labels]
        self._set_menu_values(labels or [self._NO_TARGETS_LABEL])
        if self._selected_label is not None:
            self._menu.setCurrentText(self._selected_label)

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

        if was_selected:
            remaining_targets = [t for t in self._targets_by_label.values() if t is not None]
            if self._include_all_option:
                new_label = self._ALL_TARGETS_LABEL
            elif remaining_targets:
                new_label = self._label_for(remaining_targets[0])
            else:
                new_label = self._NO_TARGETS_LABEL
            self._set_menu_values(list(self._targets_by_label.keys()) or [self._NO_TARGETS_LABEL])
            self._select(new_label)
            self._on_change(self._targets_by_label.get(new_label))
        else:
            self._refresh_menu_items()

    def _on_menu_selected(self, label: str) -> None:
        self._selected_label = label
        self._on_change(self._targets_by_label.get(label))
