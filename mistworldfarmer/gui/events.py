"""Tools, Events: select and configure seasonal and special events and gift exchanges."""
from __future__ import annotations

import os
import re
from typing import Any

import wx

from .. import paths, settings
from ..engine import dungeon, events
from ..i18n import _
from .common import Dialog, Form, group, page, tabs, with_code
from .crafting import CHECK, START
from .dungeon import SOURCE_LABELS, WHEN_SHORT


def _multi_events_path() -> str:
    return os.path.join(paths.APP, "scripts", "multi_events.mwf")


def _load_multi_events() -> tuple[str, str, str, list[str]]:
    email, server, relay = "", "S2", "direct"
    chars = [""] * 8
    path = _multi_events_path()
    if not os.path.exists(path):
        return email, server, relay, chars
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'^set\s+account_email\s*=\s*"([^"]*)"', text, re.M)
        if m:
            email = m.group(1)
        m = re.search(r'^set\s+server\s*=\s*"([^"]*)"', text, re.M)
        if m and m.group(1) in ("S2", "S1"):
            server = m.group(1)
        m = re.search(r'^set\s+relay_type\s*=\s*"([^"]*)"', text, re.M)
        if m and m.group(1) in ("direct", "relay"):
            relay = m.group(1)
        for i in range(8):
            m = re.search(r'^set\s+char_%d\s*=\s*"([^"]*)"' % (i + 1), text, re.M)
            if m:
                chars[i] = m.group(1)
    except OSError:
        pass
    return email, server, relay, chars


def _save_multi_events(email: str, server: str, relay: str, chars: list[str]) -> str:
    path = _multi_events_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# ============================================================",
        "# Multi-Account Events Script",
        "# ============================================================",
        f'set account_email = "{email.strip()}"',
        f'set server = "{server}"',
        f'set relay_type = "{relay}"',
        f'set server_name = "{server}"',
        "",
    ]
    for i in range(8):
        name = chars[i].strip() if i < len(chars) else ""
        lines.append(f'set char_{i + 1} = "{name}"')
    lines.append("")
    for i in range(8):
        lines.extend([
            f'if char_{i + 1} != ""',
            f'  login "{{char_{i + 1}}}"',
            f'  switch_character "{{char_{i + 1}}}"',
            f'  say Starting Events for {{char_{i + 1}}}...',
            "  events",
            f'  say Finished Events for {{char_{i + 1}}}.',
            "end",
            "",
        ])
    lines.append("say All configured accounts completed Events!")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class EventsDialog(Dialog):
    def __init__(self, parent: wx.Window, character: str) -> None:
        super().__init__(parent, title=_("{tool} - {name}", tool=_("Events"),
                                         name=settings.profile_display(character)),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.character = character
        self.p = settings.load_profile(character)
        self.cfg = events.config(self.p)

        root = wx.BoxSizer(wx.VERTICAL)
        nb = tabs(self)

        # Tab 1: Event
        panel_event, box_event = page(nb, _("Event"))
        f_event = Form(panel_event)

        event_keys = list(events.EVENTS)
        event_labels = [events.label(k) for k in event_keys]
        cur_idx = event_keys.index(self.cfg.get("event", "thanksgiving")) if self.cfg.get("event") in event_keys else 0

        self.event_choice = f_event.choice(_("&Event:"), event_labels, cur_idx)
        self.enabled_check = f_event.check(_("&Enable this event"), self.cfg.get("enabled", True))
        self.entries_spin = f_event.number(_("Event &entries (times to run):"), int(self.cfg.get("entries", 1)), 1, 99)

        mode_keys = list(events.HUNT_MODES)
        cur_mode = self.cfg.get("hunt_mode", "exchange")
        cur_mode_idx = mode_keys.index(cur_mode) if cur_mode in mode_keys else 0
        self.hunt_mode_choice = f_event.choice(_("Hunt &until:"),
                                               [_(events.HUNT_MODE_LABELS[k]) for k in mode_keys],
                                               cur_mode_idx)

        self.auto_exchange_check = f_event.check(_("&Automatically redeem gifts at Activity Envoy"),
                                                 self.cfg.get("auto_exchange", True))
        self.skip_big_mobs_check = f_event.check(_("&Skip monsters with HP over:"),
                                                 self.cfg.get("skip_big_mobs", True))
        self.max_mob_hp_spin = f_event.number(_("Maximum &monster HP:"),
                                              int(self.cfg.get("max_mob_hp", 40000)),
                                              1000, 99999999)
        self.max_mob_hp_spin.Enable(self.skip_big_mobs_check.GetValue())
        self.skip_big_mobs_check.Bind(wx.EVT_CHECKBOX,
                                      lambda e: self.max_mob_hp_spin.Enable(self.skip_big_mobs_check.GetValue()))

        known_pets: list[dict[str, Any]] = self.p.get("pets") or []
        self.pet_names = [str(x["name"]) for x in known_pets if x.get("name")]
        cur_pet = str(self.cfg.get("pet") or "")
        if cur_pet and cur_pet != events.NO_PET and cur_pet not in self.pet_names:
            self.pet_names.append(cur_pet)
        self.pet_choice = f_event.choice(
            _("&Pet:"),
            [_("The pets ticked in Character, Settings (else the one out)"), _("No pet")] + self.pet_names,
            1 if cur_pet == events.NO_PET else self.pet_names.index(cur_pet) + 2 if cur_pet else 0,
        )

        today_status = _("Today: already done (2/2)") if events.done_today(character) else _("Today: available (not 2/2 yet)")
        f_event.readonly(_("Status:"), today_status, lines=1)
        f_event.readonly(_("Description:"), _(
            "Configure seasonal and special game events. Currently active event: Thanksgiving Hunt. "
            "Defeating turkeys in the event hunt map grants turkeys that can be exchanged for valuable gifts."
        ), lines=4)
        box_event.Add(f_event.grid, 1, wx.EXPAND)

        # Tab 2: Exchange
        panel_ex, box_ex = page(nb, _("Exchange"))
        f_ex = group(panel_ex, box_ex, _("Gifts to redeem with turkeys (up to 2 times a day each)"))

        cur_ex: dict[str, dict[str, Any]] = self.cfg.get("exchange", {})
        self.exchange_controls: dict[str, tuple[wx.CheckBox, wx.SpinCtrl]] = {}

        for key, info in events.EXCHANGES.items():
            name = events.item_name(key)
            item_cfg = cur_ex.get(key, {})
            is_enabled = bool(item_cfg.get("enabled", False))
            cost_val = int(item_cfg.get("cost", info["default_cost"]))

            chk = f_ex.check(_("Redeem {item}", item=name), is_enabled)
            spin = f_ex.number(_("Turkeys required for {item}:", item=name), cost_val, 1, 999)
            spin.Enable(is_enabled)
            chk.Bind(wx.EVT_CHECKBOX, lambda e, s=spin, c=chk: (s.Enable(c.GetValue()), self._rebuild_after_tab()))
            self.exchange_controls[key] = (chk, spin)

        # Tab 3: Supplies
        panel_sup, box_sup = page(nb, _("Supplies"))
        f_sup = group(panel_sup, box_sup, _("Before setting out, have at least (0: don't look)"), 1)
        self.supply_rows: dict[str, tuple[wx.Choice, wx.SpinCtrl, list[str]]] = {}
        auto = _("What is on the hotkey (else the strongest in the bag)")
        for kind in dungeon.SUPPLIES:
            row = self.cfg["supplies"][kind]
            names = dungeon.known_items(kind)
            item: str = row.get("item") or ""
            if item and item not in names:
                names.append(item)
            label = _(dungeon.SUPPLY_NAMES[kind])
            first = _("Pet Rice Roll, or the pet food in Character, Settings") if kind == "pet_food" else auto
            choice = f_sup.choice(_("{what}:", what=label), [first] + names, names.index(item) + 1 if item else 0)
            count = f_sup.number(_("{what}, at least:", what=label), int(row.get("min") or 0), 0, 9999)
            self.supply_rows[kind] = (choice, count, names)

        f_short = group(panel_sup, box_sup, _("What is short"))
        sources = [k for k, _t in SOURCE_LABELS]
        self.source_choice = f_short.choice(
            _("&Get it:"), [_(t) for _k, t in SOURCE_LABELS],
            sources.index(self.cfg["source"]) if self.cfg.get("source") in sources else 0,
        )
        self.short_choice = f_short.choice(
            _("Still short &after that:"), [_("Don't go"), _("Go anyway")],
            WHEN_SHORT.index(self.cfg["when_short"]) if self.cfg.get("when_short") in WHEN_SHORT else 0,
        )
        box_sup.Add(wx.StaticText(panel_sup, label=_(
            "The pet's rows count only with a pet active, and the blue potions only for a character that uses MP. "
            "Between two entries the bot tops up again."
        )), 0, wx.TOP, 10)

        # Tab 4: Multi-Account
        panel_multi, box_multi = page(nb, _("Multi-Account"))
        f_multi = Form(panel_multi)
        m_email, m_server, m_relay, m_chars = _load_multi_events()
        self.multi_email = f_multi.text(_("Account &Email:"), m_email)
        self.multi_server = f_multi.choice(_("&Server:"), ["S2", "S1"], 0 if m_server == "S2" else 1)
        self.multi_relay = f_multi.choice(_("&Connection:"), ["direct", "relay"], 0 if m_relay == "direct" else 1)
        self.multi_chars = [f_multi.text(_("Character &{n}:", n=i + 1), m_chars[i]) for i in range(8)]
        box_multi.Add(f_multi.grid, 1, wx.EXPAND)
        test_login_btn = wx.Button(panel_multi, label=_("Test &Login All"))
        test_login_btn.Bind(wx.EVT_BUTTON, self.on_test_login)
        box_multi.Add(test_login_btn, 0, wx.TOP, 8)

        # Tab 5: After Events (Sell & Store)
        self.panel_after, self.box_after = page(nb, _("After Events (Sell & Store)"))
        self.after_choices: dict[str, wx.Choice] = {}
        self.after_shards_check: wx.CheckBox | None = None
        self._after_state: dict[str, str] = dict(self.cfg.get("after_exchange") or {})
        self._rebuild_after_tab()

        root.Add(nb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label_text, code in ((_("Save"), wx.ID_OK), (_("&Check only"), CHECK), (_("Sta&rt"), START)):
            btn = wx.Button(self, label=label_text)
            btn.Bind(wx.EVT_BUTTON, with_code(self.done, code))
            buttons.Add(btn, 0, wx.RIGHT, 8)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, _("Cancel")), 0)
        root.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 12)

        self.SetSizerAndFit(root)
        self.SetMinSize(wx.Size(560, 520))

    def _rebuild_after_tab(self) -> None:
        actions = ("keep", "store")
        action_labels = [_("Keep in bag"), _("Store in storehouse")]
        if self.after_shards_check is not None:
            self._after_state["gem_shards"] = "bag_rules" if self.after_shards_check.GetValue() else "keep"
        for k, ch in self.after_choices.items():
            sel = ch.GetSelection()
            self._after_state[k] = actions[sel] if 0 <= sel < len(actions) else "keep"
        self.after_shards_check = None
        self.after_choices.clear()
        self.box_after.Clear(True)
        f_after = group(self.panel_after, self.box_after, _("After redeeming gifts (only ticked Exchange items)"))
        shown = 0
        for key, (chk, _spin) in self.exchange_controls.items():
            if key == "arnebia" or not chk.GetValue():
                continue
            if key == "gem_shards":
                cur_on = self._after_state.get("gem_shards") in ("bag_rules", "sell", "store")
                self.after_shards_check = f_after.check(
                    _("Sell / Store &Gem Shards (uses Character -> Bag and supplies rules)"), cur_on
                )
                shown += 1
                continue
            cur = self._after_state.get(key, "keep")
            idx = 1 if cur == "store" else 0
            self.after_choices[key] = f_after.choice(_("{item}:", item=events.item_name(key)), action_labels, idx)
            shown += 1
        if not shown:
            f_after.readonly(_("Items:"), _("Tick items in the Exchange tab first to set Sell or Store here."), lines=2)
        self.panel_after.Layout()

    def _save_multi(self) -> str:
        srv = "S2" if self.multi_server.GetSelection() == 0 else "S1"
        rel = "direct" if self.multi_relay.GetSelection() == 0 else "relay"
        return _save_multi_events(
            self.multi_email.GetValue(),
            srv,
            rel,
            [c.GetValue() for c in self.multi_chars],
        )

    def on_test_login(self, _evt: wx.CommandEvent) -> None:
        self._save_multi()
        srv = "S2" if self.multi_server.GetSelection() == 0 else "S1"
        rel = "direct" if self.multi_relay.GetSelection() == 0 else "relay"
        email = self.multi_email.GetValue().strip()
        chars = [c.GetValue().strip() for c in self.multi_chars if c.GetValue().strip()]
        if not chars:
            return
        path = os.path.join(paths.APP, "scripts", "test_multi_login.mwf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = [
            f'set account_email = "{email}"',
            f'set server = "{srv}"',
            f'set relay_type = "{rel}"',
            f'set server_name = "{srv}"',
            "",
        ]
        for ch in chars:
            lines.extend([
                f'login "{ch}"',
                f'switch_character "{ch}"',
                f'say Test Login OK: {ch}',
                "wait 1",
                "",
            ])
        lines.append("say All test logins completed!")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        parent = self.GetParent()
        self.EndModal(wx.ID_OK)
        if parent is not None:
            if hasattr(parent, "fill_runs"):
                parent.fill_runs(("script", "test_multi_login"))
            if hasattr(parent, "start"):
                parent.start()

    def done(self, code: int) -> None:
        idx = self.event_choice.GetSelection()
        event_keys = list(events.EVENTS)
        selected_event = event_keys[idx] if 0 <= idx < len(event_keys) else "thanksgiving"
        self.cfg["event"] = selected_event
        self.cfg["enabled"] = self.enabled_check.GetValue()
        self.cfg["entries"] = self.entries_spin.GetValue()
        m_idx = self.hunt_mode_choice.GetSelection()
        mode_keys = list(events.HUNT_MODES)
        self.cfg["hunt_mode"] = mode_keys[m_idx] if 0 <= m_idx < len(mode_keys) else "exchange"
        self.cfg["auto_exchange"] = self.auto_exchange_check.GetValue()
        self.cfg["skip_big_mobs"] = self.skip_big_mobs_check.GetValue()
        self.cfg["max_mob_hp"] = self.max_mob_hp_spin.GetValue()
        p_idx = self.pet_choice.GetSelection()
        self.cfg["pet"] = events.NO_PET if p_idx == 1 else (self.pet_names[p_idx - 2] if p_idx >= 2 else "")

        ex_dict: dict[str, dict[str, Any]] = {}
        for key, (chk, spin) in self.exchange_controls.items():
            ex_dict[key] = {
                "enabled": chk.GetValue(),
                "cost": spin.GetValue(),
            }
        self.cfg["exchange"] = ex_dict
        actions = ("keep", "store")
        after_dict = dict(self._after_state)
        if self.after_shards_check is not None:
            after_dict["gem_shards"] = "bag_rules" if self.after_shards_check.GetValue() else "keep"
        for key, ch in self.after_choices.items():
            sel = ch.GetSelection()
            after_dict[key] = actions[sel] if 0 <= sel < len(actions) else "keep"
        self.cfg["after_exchange"] = after_dict
        events.save_global_after(after_dict)
        self.cfg["supplies"] = {
            kind: {
                "item": "" if ch.GetSelection() <= 0 else names[ch.GetSelection() - 1],
                "min": cnt.GetValue(),
            }
            for kind, (ch, cnt, names) in self.supply_rows.items()
        }
        self.cfg["source"] = SOURCE_LABELS[max(0, self.source_choice.GetSelection())][0]
        self.cfg["when_short"] = WHEN_SHORT[max(0, self.short_choice.GetSelection())]

        events.save(self.character, self.cfg)
        self._save_multi()
        if code == START and any(c.GetValue().strip() for c in self.multi_chars):
            parent = self.GetParent()
            self.EndModal(wx.ID_OK)
            if parent is not None:
                if hasattr(parent, "fill_runs"):
                    parent.fill_runs(("script", "multi_events"))
                if hasattr(parent, "start"):
                    parent.start()
            return
        self.EndModal(code)
