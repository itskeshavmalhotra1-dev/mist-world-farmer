"""GUI settings dialog for Arnebia Transfer script."""

from __future__ import annotations

import os
import re
from typing import Any
import wx

from .. import paths, settings
from ..engine import plan as plans
from ..i18n import _
from .common import Dialog, Form, group, info, ok_cancel
from .scripts import ScriptDialog

WHERE_CHOICES = (
    _("My own gang's base (cheaper)"),
    _("Another gang's base (by number/name)"),
    _("The nearest city's"),
)
WHERE_VALUES = ("gang", "other_gang", "city")

CLASS_CHOICES = (
    _("Auto-Detect"),
    _("Hunter"),
    _("Warrior"),
    _("Mage"),
    _("Orc"),
    _("Assassin"),
    _("Elf"),
    _("Priest"),
    _("Witcher"),
)
CLASS_VALUES = (
    "auto",
    "Hunter",
    "Warrior",
    "Mage",
    "Orc",
    "Assassin",
    "Elf",
    "Priest",
    "Witcher",
)

SERVER_CHOICES = (
    _("Server 2 (S2)"),
    _("Server 1 (S1)"),
)
SERVER_VALUES = ("S2", "S1")

RELAY_CHOICES = (
    _("Direct Connection"),
    _("Relay Connection"),
)
RELAY_VALUES = ("direct", "relay")

LEVEL_CHOICES = ("15", "30", "45", "60", "70", "80", "90", "100")
LEVEL_VALUES = (15, 30, 45, 60, 70, 80, 90, 100)

LEVEL_TO_CRYSTAL: dict[int, int] = {
    15: 1,
    30: 2,
    45: 3,
    60: 4,
    70: 5,
    80: 6,
    90: 7,
    100: 8,
}

from ..knowledge import DEFAULT_WEAPONS


def script_file_path() -> str:
    path = plans.script_path("arnebia_transfer")
    if os.path.isfile(path):
        return path
    alt = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "arnebia_transfer.mwf")
    if os.path.isfile(alt):
        return alt
    return path


def _clean_char_name(name: str) -> str:
    s = str(name).strip()
    s = re.sub(r'\s*-\s*S[12](?:_relay)?\b', '', s, flags=re.IGNORECASE)
    return s.strip()


def load_settings(path: str) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "account_email": "",
        "server": "S2",
        "relay_type": "direct",
        "server_name": "S2",
        "enhancer_char": "nnatsu",
        "breaker_char": "asta",
        "gear_level": 30,
        "character_class": "auto",
        "target_weapon": "Bow of chaser",
        "crystal_level": 2,
        "weapon_count": 10,
        "shard_count": 40,
        "enhance_up_to": 5,
        "spend_arnebia": 0,
        "enhance_where": "gang",
        "gang_name": "",
    }
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return cfg

    for str_key in ("account_email", "server", "relay_type", "server_name", "enhancer_char", "breaker_char", "character_class", "target_weapon"):
        m = re.search(r'^\s*set\s+' + str_key + r'\s*=\s*["\']?([^"\'\r\n]*)["\']?', text, re.MULTILINE | re.IGNORECASE)
        if m:
            cfg[str_key] = m.group(1).strip()

    raw_srv = str(cfg.get("server_name", "S2")).strip()
    if "_relay" in raw_srv.lower():
        cfg["relay_type"] = "relay"
        cfg["server"] = "S1" if "s1" in raw_srv.lower() else "S2"
    elif not cfg.get("server"):
        cfg["server"] = "S1" if "s1" in raw_srv.lower() else "S2"

    cfg["enhancer_char"] = _clean_char_name(cfg.get("enhancer_char", "nnatsu"))
    cfg["breaker_char"] = _clean_char_name(cfg.get("breaker_char", "asta"))

    for int_key in ("gear_level", "crystal_level", "weapon_count", "shard_count", "enhance_up_to", "spend_arnebia"):
        m = re.search(r'^\s*set\s+' + int_key + r'\s*=\s*(\d+)', text, re.MULTILINE | re.IGNORECASE)
        if m:
            cfg[int_key] = int(m.group(1))

    # Calculate fallback crystal level if gear_level was present
    if "gear_level" in cfg and "crystal_level" not in cfg:
        cfg["crystal_level"] = LEVEL_TO_CRYSTAL.get(cfg["gear_level"], 2)

    m_where = re.search(r'^\s*set\s+enhance_where\s*=\s*["\']?([^"\'\r\n]+)["\']?', text, re.MULTILINE | re.IGNORECASE)
    if m_where:
        val = m_where.group(1).strip().lower()
        if "other" in val:
            cfg["enhance_where"] = "other_gang"
        elif "city" in val:
            cfg["enhance_where"] = "city"
        elif val.startswith("gang:"):
            cfg["enhance_where"] = "other_gang"
            cfg["gang_name"] = val[5:].strip()
        else:
            cfg["enhance_where"] = "gang"

    m_gang = re.search(r'^\s*set\s+gang_name\s*=\s*["\']?([^"\'\r\n]*)["\']?', text, re.MULTILINE | re.IGNORECASE)
    if m_gang:
        cfg["gang_name"] = m_gang.group(1).strip()

    return cfg


def save_settings(path: str, cfg: dict[str, Any]) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return

    for key, val in cfg.items():
        if isinstance(val, str):
            replacement = f'set {key} = "{val}"'
        else:
            replacement = f'set {key} = {val}'
        pattern = r'^\s*set\s+' + key + r'\s*=.*$'
        if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
            text = re.sub(pattern, replacement, text, flags=re.MULTILINE | re.IGNORECASE)
        else:
            text = replacement + "\n" + text

    enhancer = str(cfg.get("enhancer_char", "")).strip()
    if enhancer:
        login_replacement = f'login "{enhancer}"'
        login_pattern = r'^\s*login\s+["\']?([^"\']+)["\']?'
        if re.search(login_pattern, text, re.MULTILINE | re.IGNORECASE):
            text = re.sub(login_pattern, login_replacement, text, count=1, flags=re.MULTILINE | re.IGNORECASE)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


class ArnebiaTransferDialog(Dialog):
    def __init__(self, parent: wx.Window, script_name: str = "arnebia_transfer") -> None:
        super().__init__(parent, title=_("Arnebia Transfer Settings"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.script_name = script_name
        self.path = script_file_path()
        self.cfg = load_settings(self.path)

        auto_profiles = settings.automatic_login_profiles()
        saved_emails = sorted({em for _c, em in auto_profiles if em})

        cfg_email = str(self.cfg.get("account_email", "")).strip()
        cfg_enhancer = _clean_char_name(str(self.cfg.get("enhancer_char", "")).strip())
        cfg_breaker = _clean_char_name(str(self.cfg.get("breaker_char", "")).strip())

        server_val = str(self.cfg.get("server", "")).strip().upper()
        if not server_val:
            srv_raw = str(self.cfg.get("server_name", "S2")).strip().upper()
            server_val = "S1" if "S1" in srv_raw else "S2"
        server_idx = SERVER_VALUES.index(server_val) if server_val in SERVER_VALUES else 0

        relay_val = str(self.cfg.get("relay_type", "direct")).strip().lower()
        if not relay_val:
            srv_raw = str(self.cfg.get("server_name", "")).strip().lower()
            relay_val = "relay" if "relay" in srv_raw else "direct"
        relay_idx = RELAY_VALUES.index(relay_val) if relay_val in RELAY_VALUES else 0

        if not cfg_email and auto_profiles:
            for c, em in auto_profiles:
                if cfg_enhancer and cfg_enhancer.casefold() in (c.casefold(), settings.profile_name(c).casefold()):
                    cfg_email = em
                    break
            if not cfg_email:
                for c, em in auto_profiles:
                    if cfg_breaker and cfg_breaker.casefold() in (c.casefold(), settings.profile_name(c).casefold()):
                        cfg_email = em
                        break
        if not cfg_email and saved_emails:
            cfg_email = saved_emails[0]

        clean_chars = self.get_clean_chars_for(server_val, cfg_email)

        if not cfg_enhancer or cfg_enhancer == "{enhancer_char}":
            cfg_enhancer = clean_chars[0] if clean_chars else "nnatsu"

        if not cfg_breaker or cfg_breaker == "{breaker_char}":
            if len(clean_chars) > 1:
                cfg_breaker = next((c for c in clean_chars if c.casefold() != cfg_enhancer.casefold()), clean_chars[1])
            else:
                cfg_breaker = "asta"

        root = wx.BoxSizer(wx.VERTICAL)

        # Group 1: Account and Characters
        f1 = group(self, root, _("Account and Characters"))
        self.account_email = f1.combo(_("&Account / Email:"), cfg_email, saved_emails)
        self.server_name = f1.choice(_("&Server:"), list(SERVER_CHOICES), server_idx)
        self.relay_type = f1.choice(_("Connection &Line / Relay:"), list(RELAY_CHOICES), relay_idx)
        self.enhancer_char = f1.combo(_("&Enhancer character:"), cfg_enhancer, clean_chars)
        self.breaker_char = f1.combo(_("&Breaker character:"), cfg_breaker, clean_chars)

        # Group 2: Gear, Class and Enhancing
        f2 = group(self, root, _("Gear, Class and Enhancing"))
        gear_lvl = self.cfg.get("gear_level", 30)
        lvl_idx = LEVEL_VALUES.index(gear_lvl) if gear_lvl in LEVEL_VALUES else 1
        self.gear_level = f2.choice(_("&Gear level:"), list(LEVEL_CHOICES), lvl_idx)

        char_cls = self.cfg.get("character_class", "auto")
        cls_idx = CLASS_VALUES.index(char_cls) if char_cls in CLASS_VALUES else 0
        self.char_class = f2.choice(_("Character &class:"), list(CLASS_CHOICES), cls_idx)

        def_weapon = str(self.cfg.get("target_weapon", ""))
        if not def_weapon:
            def_weapon = DEFAULT_WEAPONS.get((gear_lvl, char_cls)) or "Bow of chaser"
        self.target_weapon = f2.text(_("&Weapon name:"), def_weapon)

        self.weapon_count = f2.number(_("&Weapons to buy per round:"), self.cfg.get("weapon_count", 10), 1, 100)
        self.shard_count = f2.number(_("&Weapon Enhance Crystal Shards to buy per round:"), self.cfg.get("shard_count", 40), 1, 10000)
        self.enhance_up_to = f2.number(_("&Enhance equipment up to level:"), self.cfg.get("enhance_up_to", 5), 1, 12)
        self.spend_arnebia = f2.number(_("&Total Arnebia to spend (0 = no limit):"), self.cfg.get("spend_arnebia", 0), 0, 2000000000)

        # Group 3: Location
        f3 = group(self, root, _("Crafting and Enhancing Location"))
        where_val = self.cfg.get("enhance_where", "gang")
        where_idx = WHERE_VALUES.index(where_val) if where_val in WHERE_VALUES else 0
        self.where = f3.choice(_("Cra&ftsman location:"), list(WHERE_CHOICES), where_idx)
        self.gang_name = f3.text(_("&Gang number or name (for another gang):"), str(self.cfg.get("gang_name", "")))
        self.gang_name.Enable(where_idx == 1)

        self.account_email.Bind(wx.EVT_COMBOBOX, self.on_account_email_changed)
        self.account_email.Bind(wx.EVT_TEXT, self.on_account_email_changed)
        self.server_name.Bind(wx.EVT_CHOICE, self.on_server_changed)
        self.enhancer_char.Bind(wx.EVT_COMBOBOX, self.on_enhancer_char_changed)
        self.enhancer_char.Bind(wx.EVT_TEXT, self.on_enhancer_char_changed)

        self.gear_level.Bind(wx.EVT_CHOICE, self.on_gear_or_class_changed)
        self.char_class.Bind(wx.EVT_CHOICE, self.on_gear_or_class_changed)
        self.where.Bind(wx.EVT_CHOICE, self.on_where_changed)

        ok_cancel(self, root, _("&Save"), extra=[(_("&Start"), self.on_start), (_("Edit &raw script"), self.on_raw_script)])
        self.SetSizerAndFit(root)
        self.SetMinSize(wx.Size(560, 560))
        self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.account_email.SetFocus()

    def get_clean_chars_for(self, srv: str, email: str = "") -> list[str]:
        auto_profiles = settings.automatic_login_profiles()
        all_profiles = settings.profiles()
        names: list[str] = []
        if email:
            names = [settings.profile_name(c) for c, em in auto_profiles 
                     if em.casefold() == email.casefold() and (not srv or settings.profile_server(c) == srv)]
        if not names:
            names = [settings.profile_name(c) for c in all_profiles 
                     if settings.is_character_profile(c) and (not srv or settings.profile_server(c) == srv)]
        if not names and auto_profiles:
            names = [settings.profile_name(c) for c, _em in auto_profiles 
                     if (not srv or settings.profile_server(c) == srv)]
        seen = set()
        res = []
        for n in names:
            if n and n.casefold() not in seen:
                seen.add(n.casefold())
                res.append(n)
        return res

    def on_server_changed(self, evt: wx.Event) -> None:
        sel_srv = self.server_name.GetSelection()
        srv_val = SERVER_VALUES[sel_srv] if 0 <= sel_srv < len(SERVER_VALUES) else "S2"
        email = self.account_email.GetValue().strip().casefold()
        chars = self.get_clean_chars_for(srv_val, email)
        if chars:
            current_enhancer = self.enhancer_char.GetValue().strip()
            self.enhancer_char.Clear()
            for ch in chars:
                self.enhancer_char.Append(ch)
            if any(current_enhancer.casefold() == c.casefold() for c in chars):
                self.enhancer_char.SetValue(current_enhancer)
            else:
                self.enhancer_char.SetValue(chars[0])

            current_breaker = self.breaker_char.GetValue().strip()
            self.breaker_char.Clear()
            for ch in chars:
                self.breaker_char.Append(ch)
            if any(current_breaker.casefold() == c.casefold() for c in chars):
                self.breaker_char.SetValue(current_breaker)
            elif len(chars) > 1:
                self.breaker_char.SetValue(chars[1])
            else:
                self.breaker_char.SetValue(chars[0])
        evt.Skip()

    def on_account_email_changed(self, evt: wx.Event) -> None:
        email = self.account_email.GetValue().strip().casefold()
        if email:
            auto_profiles = settings.automatic_login_profiles()
            matched_char = next((c for c, em in auto_profiles if em.casefold() == email), "")
            if matched_char:
                server = settings.profile_server(matched_char)
                if server in SERVER_VALUES:
                    self.server_name.SetSelection(SERVER_VALUES.index(server))
            sel_srv = self.server_name.GetSelection()
            srv_val = SERVER_VALUES[sel_srv] if 0 <= sel_srv < len(SERVER_VALUES) else "S2"
            chars = self.get_clean_chars_for(srv_val, email)
            if chars:
                self.enhancer_char.Clear()
                self.breaker_char.Clear()
                for ch in chars:
                    self.enhancer_char.Append(ch)
                    self.breaker_char.Append(ch)
                current_enhancer = self.enhancer_char.GetValue().strip()
                if not any(current_enhancer.casefold() == c.casefold() for c in chars):
                    self.enhancer_char.SetValue(chars[0])
                if len(chars) > 1:
                    current_breaker = self.breaker_char.GetValue().strip()
                    if not any(current_breaker.casefold() == c.casefold() for c in chars):
                        other = next((c for c in chars if c.casefold() != self.enhancer_char.GetValue().strip().casefold()), chars[1])
                        self.breaker_char.SetValue(other)
        evt.Skip()

    def on_enhancer_char_changed(self, evt: wx.Event) -> None:
        char_val = self.enhancer_char.GetValue().strip().casefold()
        if char_val:
            auto_profiles = settings.automatic_login_profiles()
            match = next(((c, em) for c, em in auto_profiles
                          if settings.profile_name(c).casefold() == char_val
                          or settings.profile_display(c).casefold() == char_val
                          or c.casefold() == char_val), None)
            if match:
                c, match_email = match
                if match_email and match_email.casefold() != self.account_email.GetValue().strip().casefold():
                    self.account_email.SetValue(match_email)
                server = settings.profile_server(c)
                if server in SERVER_VALUES:
                    self.server_name.SetSelection(SERVER_VALUES.index(server))
        evt.Skip()

    def on_gear_or_class_changed(self, evt: wx.Event) -> None:
        lvl_str = self.gear_level.GetStringSelection()
        try:
            lvl = int(lvl_str)
        except (ValueError, TypeError):
            lvl = 30
        cls_idx = self.char_class.GetSelection()
        cls_val = CLASS_VALUES[cls_idx] if 0 <= cls_idx < len(CLASS_VALUES) else "auto"
        weapon = DEFAULT_WEAPONS.get((lvl, cls_val)) or DEFAULT_WEAPONS.get((lvl, "auto")) or "Bow of chaser"
        self.target_weapon.SetValue(weapon)
        evt.Skip()

    def on_where_changed(self, evt: wx.Event) -> None:
        self.gang_name.Enable(self.where.GetSelection() == 1)
        evt.Skip()

    def _validate_and_save(self) -> bool:
        email = self.account_email.GetValue().strip()
        if not email:
            info(self, _("Account email is required. Please enter an email."))
            self.account_email.SetFocus()
            return False

        enhancer = self.enhancer_char.GetValue().strip()
        if not enhancer:
            info(self, _("Enhancer character is required."))
            self.enhancer_char.SetFocus()
            return False

        breaker = self.breaker_char.GetValue().strip()
        if not breaker:
            info(self, _("Breaker character is required."))
            self.breaker_char.SetFocus()
            return False

        self.save()
        return True

    def on_ok(self, evt: wx.Event | None = None) -> None:
        if not self._validate_and_save():
            return
        parent = self.GetParent()
        self.EndModal(wx.ID_OK)
        info(parent, _("Arnebia transfer settings saved successfully."))

    def on_start(self, evt: wx.Event | None = None) -> None:
        if not self._validate_and_save():
            return
        parent = self.GetParent()
        self.EndModal(wx.ID_OK)
        if parent is not None and hasattr(parent, "start"):
            parent.start(item=("script", self.script_name))

    def on_char_hook(self, evt: wx.KeyEvent) -> None:
        if evt.GetKeyCode() == wx.WXK_RETURN:
            focused = self.FindFocus()
            if isinstance(focused, wx.Button) and focused.GetId() == wx.ID_CANCEL:
                self.EndModal(wx.ID_CANCEL)
                return
            if isinstance(focused, wx.Button) and focused.GetLabel() == _("&Start"):
                self.on_start(evt)
                return
            if isinstance(focused, wx.Button) and focused.GetLabel() == _("Edit &raw script"):
                self.on_raw_script(evt)
                return
            self.on_ok()
            return
        evt.Skip()

    def on_raw_script(self, evt: wx.Event) -> None:
        with ScriptDialog(self, self.script_name) as d:
            d.ShowModal()
        self.cfg = load_settings(self.path)
        self.account_email.SetValue(str(self.cfg.get("account_email", "")))

        server_val = str(self.cfg.get("server", "")).strip().upper()
        if not server_val:
            srv_raw = str(self.cfg.get("server_name", "S2")).strip().upper()
            server_val = "S1" if "S1" in srv_raw else "S2"
        server_idx = SERVER_VALUES.index(server_val) if server_val in SERVER_VALUES else 0
        self.server_name.SetSelection(server_idx)

        relay_val = str(self.cfg.get("relay_type", "")).strip().lower()
        if not relay_val:
            srv_raw = str(self.cfg.get("server_name", "")).strip().lower()
            relay_val = "relay" if "relay" in srv_raw else "direct"
        relay_idx = RELAY_VALUES.index(relay_val) if relay_val in RELAY_VALUES else 0
        self.relay_type.SetSelection(relay_idx)

        self.enhancer_char.SetValue(str(self.cfg.get("enhancer_char", "nnatsu")))
        self.breaker_char.SetValue(str(self.cfg.get("breaker_char", "asta")))

        gear_lvl = self.cfg.get("gear_level", 30)
        lvl_idx = LEVEL_VALUES.index(gear_lvl) if gear_lvl in LEVEL_VALUES else 1
        self.gear_level.SetSelection(lvl_idx)

        char_cls = self.cfg.get("character_class", "auto")
        cls_idx = CLASS_VALUES.index(char_cls) if char_cls in CLASS_VALUES else 0
        self.char_class.SetSelection(cls_idx)

        self.target_weapon.SetValue(str(self.cfg.get("target_weapon", "Bow of chaser")))
        self.weapon_count.SetValue(self.cfg.get("weapon_count", 10))
        self.shard_count.SetValue(self.cfg.get("shard_count", 40))
        self.enhance_up_to.SetValue(self.cfg.get("enhance_up_to", 5))
        self.spend_arnebia.SetValue(self.cfg.get("spend_arnebia", 0))

        where_val = self.cfg.get("enhance_where", "gang")
        where_idx = WHERE_VALUES.index(where_val) if where_val in WHERE_VALUES else 0
        self.where.SetSelection(where_idx)
        self.gang_name.SetValue(str(self.cfg.get("gang_name", "")))
        self.gang_name.Enable(where_idx == 1)

    def collect(self) -> dict[str, Any]:
        sel = self.where.GetSelection()
        where_val = WHERE_VALUES[sel] if 0 <= sel < len(WHERE_VALUES) else "gang"
        try:
            gear_lvl = int(self.gear_level.GetStringSelection())
        except (ValueError, TypeError):
            gear_lvl = 30
        cls_idx = self.char_class.GetSelection()
        cls_val = CLASS_VALUES[cls_idx] if 0 <= cls_idx < len(CLASS_VALUES) else "auto"
        crystal_lvl = LEVEL_TO_CRYSTAL.get(gear_lvl, 2)

        sel_srv = self.server_name.GetSelection()
        srv_val = SERVER_VALUES[sel_srv] if 0 <= sel_srv < len(SERVER_VALUES) else "S2"

        sel_relay = self.relay_type.GetSelection()
        relay_val = RELAY_VALUES[sel_relay] if 0 <= sel_relay < len(RELAY_VALUES) else "direct"

        full_server = srv_val if relay_val == "direct" else f"{srv_val}_relay"

        enhancer = _clean_char_name(self.enhancer_char.GetValue())
        breaker = _clean_char_name(self.breaker_char.GetValue())

        return {
            "account_email": self.account_email.GetValue().strip(),
            "server": srv_val,
            "relay_type": relay_val,
            "server_name": full_server,
            "enhancer_char": enhancer or "nnatsu",
            "breaker_char": breaker or "asta",
            "gear_level": gear_lvl,
            "character_class": cls_val,
            "target_weapon": self.target_weapon.GetValue().strip() or "Bow of chaser",
            "crystal_level": crystal_lvl,
            "weapon_count": self.weapon_count.GetValue(),
            "shard_count": self.shard_count.GetValue(),
            "enhance_up_to": self.enhance_up_to.GetValue(),
            "spend_arnebia": self.spend_arnebia.GetValue(),
            "enhance_where": where_val,
            "gang_name": self.gang_name.GetValue().strip(),
        }

    def save(self) -> None:
        new_cfg = self.collect()
        save_settings(self.path, new_cfg)
        for base in ("Uncle Farmer", "uncle-farmer", ".."):
            alt = os.path.join(base, "scripts", "arnebia_transfer.mwf")
            if os.path.isfile(alt) and os.path.abspath(alt) != os.path.abspath(self.path):
                save_settings(alt, new_cfg)
