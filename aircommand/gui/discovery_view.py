"""NetworksView, TargetPicker — the Discovery & Targets tab's two tables. See
docs/design/gui-structure.md 'Discovery & Targets tab'.

No table widget exists in CustomTkinter, so each row is built by hand inside a
ctk.CTkScrollableFrame -- one CTkLabel per column plus a trailing action
button, laid out with .grid(row=..., column=...), tracked in a
dict[BSSID, dict] keyed by bssid so _upsert/_remove can update or destroy a
specific row's widgets in place.
"""

from __future__ import annotations

import customtkinter as ctk

from aircommand.core import BSSID, MacAddress, Network, Target
from aircommand.core.events import TargetAdded, TargetRemoved


class NetworksView(ctk.CTkFrame):
    _COLUMNS = ("SSID", "BSSID", "Channel", "Encryption", "Signal (dBm)", "Last Seen")

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self._app = app
        self._rows: dict[BSSID, dict] = {}   # bssid -> {"labels": [CTkLabel,...], "button": CTkButton}
        self._dialog: "_AddAsTargetDialog | None" = None

        header = ctk.CTkFrame(self)
        header.pack(fill="x")
        for col, text in enumerate(self._COLUMNS):
            ctk.CTkLabel(header, text=text, font=ctk.CTkFont(weight="bold")).grid(row=0, column=col, padx=5, sticky="w")

        self._body = ctk.CTkScrollableFrame(self)
        self._body.pack(fill="both", expand=True)

        for network in app.engine.discovery.list_networks():
            self._upsert(network)

    def upsert_row(self, event) -> None:
        self._upsert(event.network)

    def _upsert(self, network: Network) -> None:
        values = (
            network.ssid or "(hidden)",
            str(network.bssid),
            str(network.channel),
            network.encryption.value.upper(),
            f"{network.last_signal_dbm} dBm",
            network.last_seen.strftime("%H:%M:%S"),
        )
        existing = self._rows.get(network.bssid)
        if existing is None:
            row_index = len(self._rows)
            labels = []
            for col, value in enumerate(values):
                label = ctk.CTkLabel(self._body, text=value)
                label.grid(row=row_index, column=col, padx=5, pady=2, sticky="w")
                labels.append(label)
            button = ctk.CTkButton(self._body, text="Add as Target",
                                    command=lambda n=network: self._open_add_target_dialog(n))
            button.grid(row=row_index, column=len(values), padx=5, pady=2)
            self._rows[network.bssid] = {"labels": labels, "button": button}
        else:
            for label, value in zip(existing["labels"], values):
                label.configure(text=value)

    def _open_add_target_dialog(self, network: Network) -> None:
        self._dialog = _AddAsTargetDialog(self, self._app, network)


class _AddAsTargetDialog(ctk.CTkToplevel):
    """Modal, pre-filled bssid/ssid/channel (read-only), asks only for a label.
    Same modal convention as SudoPasswordDialog (grab_set(), no wait_window() --
    nothing blocks on the result, it calls engine.targets.add(...) itself and
    closes).
    """

    def __init__(self, master, app, network: Network) -> None:
        super().__init__(master)
        self.title("Add as Target")
        self._app = app
        self._network = network

        ctk.CTkLabel(self, text=f"SSID: {network.ssid or '(hidden)'}").pack(padx=20, pady=(20, 0), anchor="w")
        ctk.CTkLabel(self, text=f"BSSID: {network.bssid}").pack(padx=20, pady=(0, 0), anchor="w")
        ctk.CTkLabel(self, text=f"Channel: {network.channel}").pack(padx=20, pady=(0, 10), anchor="w")

        ctk.CTkLabel(self, text="Label:").pack(padx=20, pady=(0, 0), anchor="w")
        self._label_entry = ctk.CTkEntry(self)
        self._label_entry.pack(padx=20, pady=(0, 10), fill="x")

        self._error_label = ctk.CTkLabel(self, text="", text_color="red")
        self._error_label.pack(padx=20, pady=(0, 10))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")

        self._add_button = ctk.CTkButton(button_row, text="Add", command=self._on_add)
        self._add_button.pack(side="right", padx=(10, 0))

        self._cancel_button = ctk.CTkButton(button_row, text="Cancel", command=self._on_cancel)
        self._cancel_button.pack(side="right")

        self._label_entry.focus_set()
        self.grab_set()

    def _on_add(self) -> None:
        label = self._label_entry.get().strip()
        if not label:
            self._error_label.configure(text="Label can't be empty")
            return
        self._app.engine.targets.add(self._network.bssid, self._network.ssid, self._network.channel, label)
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()


class TargetPicker(ctk.CTkFrame):
    _COLUMNS = ("SSID", "BSSID", "Channel", "Label", "Date Added")

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self._app = app
        self._rows: dict[BSSID, dict] = {}
        self._dialog: "_AddManuallyDialog | None" = None

        header = ctk.CTkFrame(self)
        header.pack(fill="x")
        for col, text in enumerate(self._COLUMNS):
            ctk.CTkLabel(header, text=text, font=ctk.CTkFont(weight="bold")).grid(row=0, column=col, padx=5, sticky="w")

        add_manually_button = ctk.CTkButton(header, text="Add manually", command=self._open_add_manually_dialog)
        add_manually_button.grid(row=0, column=len(self._COLUMNS), padx=5)

        self._body = ctk.CTkScrollableFrame(self)
        self._body.pack(fill="both", expand=True)

        for target in app.engine.targets.list():
            self._upsert(target)

    def upsert_row(self, event: TargetAdded) -> None:
        self._upsert(event.target)

    def remove_row(self, event: TargetRemoved) -> None:
        self._remove(event.bssid)

    def _upsert(self, target: Target) -> None:
        values = (
            target.ssid,
            str(target.bssid),
            str(target.channel),
            target.label,
            target.date_added.strftime("%Y-%m-%d %H:%M"),
        )
        existing = self._rows.get(target.bssid)
        if existing is None:
            row_index = len(self._rows)
            labels = []
            for col, value in enumerate(values):
                label = ctk.CTkLabel(self._body, text=value)
                label.grid(row=row_index, column=col, padx=5, pady=2, sticky="w")
                labels.append(label)
            button = ctk.CTkButton(self._body, text="Remove",
                                    command=lambda b=target.bssid: self._app.engine.targets.remove(b))
            button.grid(row=row_index, column=len(values), padx=5, pady=2)
            self._rows[target.bssid] = {"labels": labels, "button": button}
        else:
            for label, value in zip(existing["labels"], values):
                label.configure(text=value)

    def _remove(self, bssid: BSSID) -> None:
        existing = self._rows.pop(bssid, None)
        if existing is None:
            return
        for label in existing["labels"]:
            label.destroy()
        existing["button"].destroy()

        # Re-grid every remaining row at sequential row indices so no blank gap
        # is left where the removed row was. dict insertion order is preserved.
        for row_index, row in enumerate(self._rows.values()):
            for col, label in enumerate(row["labels"]):
                label.grid(row=row_index, column=col, padx=5, pady=2, sticky="w")
            row["button"].grid(row=row_index, column=len(row["labels"]), padx=5, pady=2)

    def _open_add_manually_dialog(self) -> None:
        self._dialog = _AddManuallyDialog(self, self._app)


class _AddManuallyDialog(ctk.CTkToplevel):
    """Modal, all four fields editable -- for a Target not currently visible in
    NetworksView (e.g. temporarily out of range). Same modal convention as
    SudoPasswordDialog (grab_set(), no wait_window()).
    """

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self.title("Add Target manually")
        self._app = app

        ctk.CTkLabel(self, text="BSSID:").pack(padx=20, pady=(20, 0), anchor="w")
        self._bssid_entry = ctk.CTkEntry(self)
        self._bssid_entry.pack(padx=20, pady=(0, 10), fill="x")

        ctk.CTkLabel(self, text="SSID:").pack(padx=20, pady=(0, 0), anchor="w")
        self._ssid_entry = ctk.CTkEntry(self)
        self._ssid_entry.pack(padx=20, pady=(0, 10), fill="x")

        ctk.CTkLabel(self, text="Channel:").pack(padx=20, pady=(0, 0), anchor="w")
        self._channel_entry = ctk.CTkEntry(self)
        self._channel_entry.pack(padx=20, pady=(0, 10), fill="x")

        ctk.CTkLabel(self, text="Label:").pack(padx=20, pady=(0, 0), anchor="w")
        self._label_entry = ctk.CTkEntry(self)
        self._label_entry.pack(padx=20, pady=(0, 10), fill="x")

        self._error_label = ctk.CTkLabel(self, text="", text_color="red")
        self._error_label.pack(padx=20, pady=(0, 10))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=20, pady=(0, 20), fill="x")

        self._add_button = ctk.CTkButton(button_row, text="Add", command=self._on_add)
        self._add_button.pack(side="right", padx=(10, 0))

        self._cancel_button = ctk.CTkButton(button_row, text="Cancel", command=self._on_cancel)
        self._cancel_button.pack(side="right")

        self._bssid_entry.focus_set()
        self.grab_set()

    def _on_add(self) -> None:
        try:
            bssid = MacAddress.parse(self._bssid_entry.get().strip())
        except ValueError:
            self._error_label.configure(text="Invalid BSSID")
            return

        ssid = self._ssid_entry.get().strip()
        if not ssid:
            self._error_label.configure(text="SSID can't be empty")
            return

        try:
            channel = int(self._channel_entry.get().strip())
        except ValueError:
            self._error_label.configure(text="Invalid channel")
            return

        label = self._label_entry.get().strip()
        if not label:
            self._error_label.configure(text="Label can't be empty")
            return

        self._app.engine.targets.add(bssid, ssid, channel, label)
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()
