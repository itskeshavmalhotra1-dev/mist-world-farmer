"""Tools, Multi Gathering: run Fishing, Mining or Collect Herbs across up to 8 accounts."""

from __future__ import annotations

import os
import re
from typing import Any

import wx

from .. import jsonio, paths, settings
from ..engine import gathering
from ..i18n import _
from .common import error, group


MAX_ACCOUNTS = 8
SKILL_KEYS = ("auto", "Fishing", "Mining", "Collect Herbs")


def _config_path() -> str:
    return os.path.join(paths.APP, "multi_gathering.json")


def _script_path() -> str:
    return os.path.join(paths.APP, "scripts", "multi_gathering.mwf")


def _load_zones_data() -> dict[str, Any]:
    try:
        with open(os.path.join(paths.DATA, "gathering_zones.json"), "rb") as f:
            data = jsonio.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_config() -> dict[str, Any]:
    try:
        with open(_config_path(), "rb") as f:
            raw = jsonio.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    accounts = raw.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    norm_accounts: list[dict[str, Any]] = []
    for i in range(MAX_ACCOUNTS):
        row = accounts[i] if i < len(accounts) and isinstance(accounts[i], dict) else {}
        norm_accounts.append({
            "name": str(row.get("name") or ""),
            "skill": str(row.get("skill") or "auto"),
            "own_gang": bool(row.get("own_gang", raw.get("own_gang", True))),
            "allied_gang": bool(row.get("allied_gang", False)),
            "allied_name": str(row.get("allied_name") or ""),
            "other_gang": bool(row.get("other_gang", False)),
            "other_name": str(row.get("other_name") or ""),
            "place": str(row.get("place") or ""),
            "zone": str(row.get("zone") or ""),
            "x": int(row.get("x") or 0),
            "y": int(row.get("y") or 0),
        })
    return {
        "email": str(raw.get("email") or ""),
        "server": str(raw.get("server") or "S2"),
        "relay": str(raw.get("relay") or "direct"),
        "accounts": norm_accounts,
        "outside": bool(raw.get("outside", True)),
        "stop_vitality": int(raw.get("stop_vitality", 0) or 0),
        "sell": bool(raw.get("sell", True)),
        "minutes": int(raw.get("minutes", 0) or 0),
    }


def save_config(cfg: dict[str, Any]) -> None:
    os.makedirs(paths.APP, exist_ok=True)
    jsonio.save(_config_path(), cfg)


def write_script(cfg: dict[str, Any]) -> str:
    path = _script_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    email = str(cfg.get("email") or "").strip()
    server = "S1" if str(cfg.get("server") or "").upper() == "S1" else "S2"
    relay = "relay" if str(cfg.get("relay") or "").lower() == "relay" else "direct"
    lines = [
        "# Multi Gathering Script (generated from Tools -> Multi Gathering)",
        f'set account_email = "{email}"',
        f'set server = "{server}"',
        f'set relay_type = "{relay}"',
        f'set server_name = "{server}"',
        "",
    ]
    accounts = cfg.get("accounts") or []
    for idx, acc in enumerate(accounts):
        name = str(acc.get("name") or "").strip()
        if not name:
            continue
        lines.extend([
            f'set multi_gather_slot = "{idx}"',
            f'switch_character "{name}"',
            f'say Starting gathering for {name}',
            "gather",
            f'say Finished gathering for {name}',
            "",
        ])
    lines.append("say All multi gathering accounts finished!")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _maps_for_skill(skill: str, zones_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (map_keys, display_labels) for the selected skill."""
    keys = [""]
    labels = [_("Auto (Best for this character)")]
    skills = gathering.SKILL_NAMES if skill == "auto" else (skill,)
    seen: set[tuple[str, int]] = set()
    for sk in skills:
        prefix = f"[{_(sk)}] " if skill == "auto" else ""
        sk_maps = zones_data.get(sk)
        if not isinstance(sk_maps, dict):
            continue
        for mid_str, info in sk_maps.items():
            try:
                mid = int(mid_str)
            except (TypeError, ValueError):
                continue
            if (sk, mid) in seen:
                continue
            seen.add((sk, mid))
            mname = str(info.get("name") or mid_str) if isinstance(info, dict) else mid_str
            keys.append(f"map:{mid}")
            labels.append(f"{prefix}{mname} ({mid})")
    return keys, labels


def _extract_map_id(place_key: str) -> int | None:
    if not place_key:
        return None
    if place_key.startswith("map:"):
        try:
            return int(place_key.split(":", 1)[1])
        except ValueError:
            return None
    m = re.search(r"\((\d+)\)", place_key)
    return int(m.group(1)) if m else None


def _zones_for_map(skill: str, place_key: str, zones_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (zone_keys, display_labels) for the selected (skill, map)."""
    keys = [""]
    labels = [_("Auto (Any zone)")]
    mid = _extract_map_id(place_key)
    if mid is None:
        return keys, labels
    skills = gathering.SKILL_NAMES if skill == "auto" else (skill,)
    seen: set[str] = set()
    for sk in skills:
        entry = (zones_data.get(sk) or {}).get(str(mid))
        if not isinstance(entry, dict):
            continue
        for z in entry.get("zones") or []:
            zname = str(z.get("name") or "").strip()
            if not zname or zname.casefold() in seen:
                continue
            seen.add(zname.casefold())
            keys.append(zname)
            labels.append(zname)
    return keys, labels


class MultiGatheringDialog(wx.Dialog):
    """Configure and start Multi Gathering across up to 8 accounts (General + 8 account tabs)."""

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, title=_("Multi Gathering"))
        self.cfg = load_config()
        self.zones_data = _load_zones_data()
        root = wx.BoxSizer(wx.VERTICAL)
        nb = wx.Notebook(self)

        # --- Tab 1: General (Email, Server, Line, Global Options) ---
        tab_gen = wx.Panel(nb)
        g_box = wx.BoxSizer(wx.VERTICAL)

        f_acc = group(tab_gen, g_box, _("Account and Connection"))
        saved_emails = sorted({em for _k, em in settings.automatic_login_profiles() if em})
        email_val = self.cfg["email"] or (saved_emails[0] if saved_emails else "")
        self.email = f_acc.combo(_("&Account / Email:"), email_val, saved_emails)
        srv_idx = 1 if self.cfg["server"].upper() == "S1" else 0
        self.server = f_acc.choice(_("&Server:"), [_("S2"), _("S1")], srv_idx)
        rel_idx = 1 if self.cfg["relay"].lower() == "relay" else 0
        self.relay = f_acc.choice(_("Connection &Line:"), [_("Direct"), _("Relay")], rel_idx)

        f_opt = group(tab_gen, g_box, _("Outside / Vitality and General"))
        self.outside = f_opt.check(_("Gather &outside using Vitality after gang finishes"), self.cfg["outside"])
        self.stop_vitality = f_opt.number(_("Stop when &Vitality is down to (0 = until it runs out):"),
                                          self.cfg["stop_vitality"], 0, 100000)
        self.sell = f_opt.check(_("&Sell gathered items to the Buyer"), self.cfg["sell"])
        self.minutes = f_opt.number(_("Max &minutes per character (0 = no limit):"),
                                    self.cfg["minutes"], 0, 1440)

        tab_gen.SetSizer(g_box)
        nb.AddPage(tab_gen, _("General"))

        # --- Tabs 2..9: Account 1 .. Account 8 ---
        skill_labels = [
            _("Auto (Character's own)"),
            _("Fishing"),
            _("Mining"),
            _("Collect Herbs"),
        ]
        self.acc_tabs: list[dict[str, Any]] = []
        for i in range(MAX_ACCOUNTS):
            acc = self.cfg["accounts"][i]
            panel = wx.Panel(nb)
            p_box = wx.BoxSizer(wx.VERTICAL)
            f = group(panel, p_box, _("Account {n} Settings", n=i + 1))

            name_ctrl = f.text(_("&Character name:"), acc["name"])
            sk = acc["skill"] if acc["skill"] in SKILL_KEYS else "auto"
            skill_ctrl = f.choice(_("&Skill:"), skill_labels, SKILL_KEYS.index(sk))

            own_ctrl = f.check(_("&Own gang"), acc["own_gang"])
            allied_ctrl = f.check(_("&Allied gang"), acc["allied_gang"])
            allied_name_ctrl = f.text(_("Allied gang &name:"), acc["allied_name"])
            other_ctrl = f.check(_("O&ther gang"), acc["other_gang"])
            other_name_ctrl = f.text(_("Other &gang name:"), acc["other_name"])

            map_keys, map_labels = _maps_for_skill(sk, self.zones_data)
            map_ctrl = f.choice(_("Outside &map:"), map_labels,
                                map_keys.index(acc["place"]) if acc["place"] in map_keys else 0)
            chosen_map_key = map_keys[map_ctrl.GetSelection()] if map_ctrl.GetSelection() >= 0 else ""

            zone_keys, zone_labels = _zones_for_map(sk, chosen_map_key, self.zones_data)
            zone_ctrl = f.choice(_("&Zone:"), zone_labels,
                                 zone_keys.index(acc["zone"]) if acc["zone"] in zone_keys else 0)

            x_ctrl = f.number(_("&X coordinate (0 = auto):"), int(acc.get("x") or 0), 0, 9999)
            y_ctrl = f.number(_("&Y coordinate (0 = auto):"), int(acc.get("y") or 0), 0, 9999)

            tab_state: dict[str, Any] = {
                "name": name_ctrl,
                "skill": skill_ctrl,
                "own_gang": own_ctrl,
                "allied_gang": allied_ctrl,
                "allied_name": allied_name_ctrl,
                "other_gang": other_ctrl,
                "other_name": other_name_ctrl,
                "map": map_ctrl,
                "map_keys": map_keys,
                "zone": zone_ctrl,
                "zone_keys": zone_keys,
                "x": x_ctrl,
                "y": y_ctrl,
            }
            self.acc_tabs.append(tab_state)

            allied_ctrl.Bind(wx.EVT_CHECKBOX, lambda e, idx=i: self._sync_gang(idx))
            other_ctrl.Bind(wx.EVT_CHECKBOX, lambda e, idx=i: self._sync_gang(idx))
            skill_ctrl.Bind(wx.EVT_CHOICE, lambda e, idx=i: self._on_skill_changed(idx))
            map_ctrl.Bind(wx.EVT_CHOICE, lambda e, idx=i: self._on_map_changed(idx))
            self._sync_gang(i)

            panel.SetSizer(p_box)
            nb.AddPage(panel, _("Account {n}", n=i + 1))

        root.Add(nb, 1, wx.EXPAND | wx.ALL, 8)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        start_btn = wx.Button(self, label=_("&Start"))
        save_btn = wx.Button(self, wx.ID_OK, _("&Save"))
        cancel_btn = wx.Button(self, wx.ID_CANCEL, _("Cancel"))
        start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        save_btn.Bind(wx.EVT_BUTTON, self.on_save)
        btns.Add(start_btn, 0, wx.RIGHT, 8)
        btns.Add(save_btn, 0, wx.RIGHT, 8)
        btns.Add(cancel_btn, 0)
        root.Add(btns, 0, wx.ALL | wx.ALIGN_RIGHT, 10)

        self.SetSizerAndFit(root)
        self.SetMinSize(wx.Size(640, 560))
        self.email.SetFocus()

    def _sync_gang(self, idx: int) -> None:
        st = self.acc_tabs[idx]
        st["allied_name"].Enable(st["allied_gang"].GetValue())
        st["other_name"].Enable(st["other_gang"].GetValue())

    def _on_skill_changed(self, idx: int) -> None:
        st = self.acc_tabs[idx]
        sk_idx = st["skill"].GetSelection()
        sk = SKILL_KEYS[sk_idx] if 0 <= sk_idx < len(SKILL_KEYS) else "auto"
        old_map_idx = st["map"].GetSelection()
        old_map = st["map_keys"][old_map_idx] if 0 <= old_map_idx < len(st["map_keys"]) else ""
        new_keys, new_labels = _maps_for_skill(sk, self.zones_data)
        st["map_keys"] = new_keys
        st["map"].Set(new_labels)
        st["map"].SetSelection(new_keys.index(old_map) if old_map in new_keys else 0)
        self._on_map_changed(idx)

    def _on_map_changed(self, idx: int) -> None:
        st = self.acc_tabs[idx]
        sk_idx = st["skill"].GetSelection()
        sk = SKILL_KEYS[sk_idx] if 0 <= sk_idx < len(SKILL_KEYS) else "auto"
        map_idx = st["map"].GetSelection()
        map_key = st["map_keys"][map_idx] if 0 <= map_idx < len(st["map_keys"]) else ""
        old_z_idx = st["zone"].GetSelection()
        old_z = st["zone_keys"][old_z_idx] if 0 <= old_z_idx < len(st["zone_keys"]) else ""
        z_keys, z_labels = _zones_for_map(sk, map_key, self.zones_data)
        st["zone_keys"] = z_keys
        st["zone"].Set(z_labels)
        st["zone"].SetSelection(z_keys.index(old_z) if old_z in z_keys else 0)

    def _collect_and_save(self, require_accounts: bool) -> dict[str, Any] | None:
        email = self.email.GetValue().strip()
        if require_accounts and not email:
            error(self, _("Please enter the Account / Email."))
            self.email.SetFocus()
            return None
        accounts: list[dict[str, Any]] = []
        has_any = False
        for st in self.acc_tabs:
            name = st["name"].GetValue().strip()
            if name:
                has_any = True
            sk_idx = st["skill"].GetSelection()
            sk = SKILL_KEYS[sk_idx] if 0 <= sk_idx < len(SKILL_KEYS) else "auto"
            m_idx = st["map"].GetSelection()
            place = st["map_keys"][m_idx] if 0 <= m_idx < len(st["map_keys"]) else ""
            z_idx = st["zone"].GetSelection()
            zone = st["zone_keys"][z_idx] if 0 <= z_idx < len(st["zone_keys"]) else ""
            accounts.append({
                "name": name,
                "skill": sk,
                "own_gang": st["own_gang"].GetValue(),
                "allied_gang": st["allied_gang"].GetValue(),
                "allied_name": st["allied_name"].GetValue().strip(),
                "other_gang": st["other_gang"].GetValue(),
                "other_name": st["other_name"].GetValue().strip(),
                "place": place,
                "zone": zone,
                "x": int(st["x"].GetValue()),
                "y": int(st["y"].GetValue()),
            })
        if require_accounts and not has_any:
            error(self, _("Please enter at least one character name in Account 1."))
            self.acc_tabs[0]["name"].SetFocus()
            return None
        cfg: dict[str, Any] = {
            "email": email,
            "server": "S1" if self.server.GetSelection() == 1 else "S2",
            "relay": "relay" if self.relay.GetSelection() == 1 else "direct",
            "accounts": accounts,
            "outside": self.outside.GetValue(),
            "stop_vitality": self.stop_vitality.GetValue(),
            "sell": self.sell.GetValue(),
            "minutes": self.minutes.GetValue(),
        }
        save_config(cfg)
        write_script(cfg)
        return cfg

    def on_save(self, _evt: wx.CommandEvent) -> None:
        if self._collect_and_save(require_accounts=False) is not None:
            self.EndModal(wx.ID_OK)

    def on_start(self, _evt: wx.CommandEvent) -> None:
        if self._collect_and_save(require_accounts=True) is None:
            return
        parent = self.GetParent()
        self.EndModal(wx.ID_OK)
        if parent is not None and hasattr(parent, "start"):
            parent.start(item=("script", "multi_gathering"))
