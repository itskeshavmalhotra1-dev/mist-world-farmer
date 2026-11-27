"""Tools, Events: automated seasonal and special events (Thanksgiving, etc.)."""
from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any, cast

from .. import atlas, jsonio, knowledge, settings
from ..clock import clock_for
from ..i18n import N_, _
from . import chores, dungeon
from .botparts.base import CONFIRM

if TYPE_CHECKING:
    from .bot import Bot

EVENTS: tuple[str, ...] = ("thanksgiving",)

EVENT_NAMES: dict[str, str] = {
    "thanksgiving": N_("Thanksgiving"),
}

HUNT_MODES: tuple[str, ...] = ("exchange", "full")

HUNT_MODE_LABELS: dict[str, str] = {
    "exchange": N_("Until required exchange turkeys are collected (then quit via Action menu)"),
    "full": N_("Full event duration (until game auto-exits)"),
}

EXCHANGES: dict[str, dict[str, Any]] = {
    "arnebia": {"name": N_("Arnebia"), "pattern": r"Arnebia", "default_cost": 4},
    "gem_shards": {"name": N_("Gem Shards"), "pattern": r"Gem Shards", "default_cost": 4},
    "res_crystals": {"name": N_("Resurrection Crystals"), "pattern": r"Resurrection Crystals", "default_cost": 4},
    "butterflies": {"name": N_("Memory Butterflies"), "pattern": r"Memory Butterflies", "default_cost": 4},
    "plat_stones": {"name": N_("Platinum Teleportation Stones"), "pattern": r"Platinum Teleportation", "default_cost": 4},
    "black_gold_stones": {"name": N_("Black Gold Teleportation Stones"), "pattern": r"Black Gold Teleportation", "default_cost": 4},
    "skill_pills": {"name": N_("Skill Proficiency Pills (large)"), "pattern": r"Skill Proficiency", "default_cost": 2},
    "large_red": {"name": N_("Large Red Potions"), "pattern": r"Large Red Potions", "default_cost": 2},
    "large_blue": {"name": N_("Large Blue Potions"), "pattern": r"Large Blue Potions", "default_cost": 2},
    "hearthstones": {"name": N_("Hearthstones"), "pattern": r"Hearthstones", "default_cost": 2},
    "toolboxes": {"name": N_("Toolboxes (Small/Large/Alloy)"), "pattern": r"Toolbox", "default_cost": 2},
    "pliers": {"name": N_("Pliers (Small/Large/Alloy)"), "pattern": r"Pliers", "default_cost": 1},
    "pet_rice": {"name": N_("Pet Rice Balls"), "pattern": r"Pet Rice", "default_cost": 1},
}

DEFAULT_EXCHANGE: dict[str, dict[str, Any]] = {
    "arnebia": {"enabled": True, "cost": 4},
    "gem_shards": {"enabled": True, "cost": 4},
    "res_crystals": {"enabled": True, "cost": 4},
    "butterflies": {"enabled": True, "cost": 4},
    "plat_stones": {"enabled": False, "cost": 4},
    "black_gold_stones": {"enabled": False, "cost": 4},
    "skill_pills": {"enabled": False, "cost": 2},
    "large_red": {"enabled": False, "cost": 2},
    "large_blue": {"enabled": False, "cost": 2},
    "hearthstones": {"enabled": False, "cost": 2},
    "toolboxes": {"enabled": False, "cost": 2},
    "pliers": {"enabled": False, "cost": 1},
    "pet_rice": {"enabled": False, "cost": 1},
}

NO_PET = dungeon.NO_PET
DONE_KEY = "_event thanksgiving"

DEFAULTS: dict[str, Any] = {
    "event": "thanksgiving",
    "enabled": True,
    "entries": 1,
    "hunt_mode": "exchange",
    "skip_big_mobs": True,
    "max_mob_hp": 40000,
    "pet": "",
    "supplies": copy.deepcopy(dungeon.DEFAULTS["supplies"]),
    "source": "both",
    "when_short": "stop",
    "auto_exchange": True,
    "exchange": DEFAULT_EXCHANGE,
}


def label(event_key: str) -> str:
    return _(EVENT_NAMES.get(event_key, event_key))


def item_name(ex_key: str) -> str:
    item = EXCHANGES.get(ex_key)
    return _(item["name"]) if item else ex_key


def required_turkeys(cfg: dict[str, Any]) -> int:
    """Calculate total turkeys needed to redeem all enabled exchange items (2 rounds each)."""
    ex_cfg: dict[str, dict[str, Any]] = cfg.get("exchange", {})
    total = 0
    for key, info in EXCHANGES.items():
        item_cfg = ex_cfg.get(key, {})
        if item_cfg.get("enabled", False):
            cost = int(item_cfg.get("cost", info["default_cost"]))
            total += cost * 2
    return total


def turkey_count(b: Bot) -> int:
    """Total turkey items in bag."""
    from . import town

    return sum(count for name, count in town.counts(b).items() if "turkey" in name)


def config(p: dict[str, Any]) -> dict[str, Any]:
    mine: dict[str, Any] = p.get("events") or {}
    c = {**copy.deepcopy(DEFAULTS), **mine}
    if c.get("event") not in EVENTS:
        c["event"] = "thanksgiving"
    c["entries"] = max(1, int(mine.get("entries", 1)))
    c["hunt_mode"] = str(mine.get("hunt_mode", "exchange"))
    if c["hunt_mode"] not in HUNT_MODES:
        c["hunt_mode"] = "exchange"
    c["skip_big_mobs"] = bool(mine.get("skip_big_mobs", True))
    c["max_mob_hp"] = max(1000, int(mine.get("max_mob_hp", 40000)))
    c["pet"] = str(mine.get("pet") or "")
    supplies: dict[str, dict[str, Any]] = {k: dict(v) for k, v in DEFAULTS["supplies"].items()}
    chosen_sup: dict[str, Any] = c.get("supplies") or {}
    for k, v in chosen_sup.items():
        if k in supplies and jsonio.is_object(v):
            supplies[k].update(v)
    c["supplies"] = supplies
    if c.get("source") not in dungeon.SOURCES:
        c["source"] = "both"
    if c.get("when_short") not in ("stop", "go"):
        c["when_short"] = "stop"

    # Merge exchange config safely supporting both old boolean and new {enabled, cost} dict formats
    ex: dict[str, dict[str, Any]] = copy.deepcopy(DEFAULT_EXCHANGE)
    own_ex: dict[str, Any] = c.get("exchange") or {}
    for k in EXCHANGES:
        if k in own_ex:
            v = own_ex[k]
            default_cost = int(EXCHANGES[k]["default_cost"])
            if isinstance(v, bool):
                ex[k] = {"enabled": v, "cost": default_cost}
            elif isinstance(v, dict):
                v_dict = cast(dict[str, Any], v)
                en = bool(v_dict.get("enabled", False))
                cost_raw = v_dict.get("cost", default_cost)
                co = int(cost_raw) if isinstance(cost_raw, (int, float)) else default_cost
                ex[k] = {"enabled": en, "cost": max(1, co)}
    c["exchange"] = ex
    return c


def save(character: str, cfg: dict[str, Any]) -> None:
    p = settings.load_profile(character)
    p["events"] = dict(cfg)
    settings.save_profile(character, p)


KNOWN_ENVOYS: dict[int, tuple[str, tuple[float, float]]] = {
    1021: ("activity envoy", (75.0, 281.0)),     # Mario Basin
    1041: ("activity envoys", (201.0, 200.0)),   # Central Plains
    1061: ("activity envoys", (67.0, 131.0)),    # Fertile Wilds
    1081: ("activity envoys", (338.0, 160.0)),   # Tuneful Wetland
    1141: ("activity envoys", (84.0, 83.0)),     # Hilly Terrain
    1181: ("activity envoy", (225.0, 225.0)),    # Black Wasteland
}


def find_envoy_in_sight(b: Bot) -> dict[str, Any] | None:
    """Return the Activity Envoy NPC currently in sight, matching singular or plural names."""
    snap = b.snap()
    for e in snap.get("entities", []):
        name = (e.get("name") or "").strip().lower()
        if name in ("activity envoy", "activity envoys"):
            return e
    return None


def _envoy_on_map(mid: int) -> tuple[str, tuple[float, float]] | None:
    if mid in KNOWN_ENVOYS:
        return KNOWN_ENVOYS[mid]
    for n in knowledge.data().get("npcs", []):
        name = (n.get("name") or "").strip()
        if n.get("map") == mid and name.lower() in ("activity envoy", "activity envoys"):
            return name, (float(n["x"]), float(n["y"]))
    return None


def talk_to_envoy(b: Bot) -> bool:
    """Talk to Activity Envoy on the current map if present, else travel to Mario Basin."""
    b.close_dialogs()
    seen = find_envoy_in_sight(b)
    if seen is not None:
        return b.talk(str(seen["name"]), at=(float(seen["x"]), float(seen["y"])))

    mid = b.map_id()
    spot = _envoy_on_map(mid)
    if spot is not None:
        return b.talk(spot[0], at=spot[1])

    # If currently in a city (e.g. Giant Rock), check if an envoy is in town before flying away
    if any(c.get("map") == mid for c in knowledge.cities()):
        for candidate in ("activity envoy", "activity envoys"):
            if b.talk(candidate):
                return True

    b.log(_("traveling to Activity Envoy in Mario Basin..."), speak=True)
    b.travel(1021, (75, 283))
    b.settled_map()
    if b.map_id() != 1021:
        b.log(_("couldn't travel to Activity Envoy in Mario Basin"))
        return False
    return b.talk("activity envoy", at=(75.0, 281.0))


def _can_redeem(d: dict[str, Any], pat: str) -> bool:
    """True if the dialog menu has an available (not sold out / 2/2 times) exchange option."""
    if not d.get("open"):
        return False
    rows: list[list[dict[str, Any]]] = d.get("top", {}).get("rows", [])
    for row in rows:
        for col in row:
            for opt in col.get("options", []):
                if re.search(pat, opt, re.I):
                    low = str(opt).lower()
                    return "sold out" not in low and "2/2 times" not in low
    return False


def _dialog_options(d: dict[str, Any]) -> list[str]:
    if not d.get("open"):
        return []
    rows: list[list[dict[str, Any]]] = d.get("top", {}).get("rows", [])
    return [str(opt) for row in rows for col in row for opt in col.get("options", [])]


def _is_redeem_submenu(d: dict[str, Any]) -> bool:
    opts = _dialog_options(d)
    return any(
        "today's exchanges" in opt.lower()
        or "please select the gift" in opt.lower()
        or "sold out" in opt.lower()
        for opt in opts
    )


def _wait_redeem_submenu(b: Bot, timeout: float = 4.0) -> bool:
    end = clock_for(b).time() + timeout
    while clock_for(b).time() < end:
        if _is_redeem_submenu(b.dialog()):
            return True
        b.sleep(0.15)
    return _is_redeem_submenu(b.dialog())


def _open_redeem_menu(b: Bot) -> bool:
    """Ensure the Redeem Thanksgiving Gifts submenu is open and loaded."""
    d = b.dialog()
    if _is_redeem_submenu(d):
        return True
    if any("redeem thanksgiving gifts" in opt.lower() for opt in _dialog_options(d)):
        if b.choose(r"Redeem Thanksgiving Gifts") is not None and _wait_redeem_submenu(b):
            return True

    if not talk_to_envoy(b):
        b.log(_("couldn't talk to Activity Envoy"))
        return False
    b.wait_dialog(3.0)
    if b.choose(r"Redeem Thanksgiving Gifts") is None:
        b.close_dialogs()
        b.log(_("couldn't open Redeem Thanksgiving Gifts menu"))
        return False
    if not _wait_redeem_submenu(b):
        b.close_dialogs()
        b.log(_("couldn't open Redeem Thanksgiving Gifts menu"))
        return False
    return True


def _wait_turkey_deducted(b: Bot, before: int, timeout: float = 2.5) -> bool:
    """Wait until the server deducts turkeys from the bag after an exchange click."""
    end = clock_for(b).time() + timeout
    while clock_for(b).time() < end:
        if turkey_count(b) < before:
            return True
        b.sleep(0.2)
    return turkey_count(b) < before


def redeem_thanksgiving(b: Bot, cfg: dict[str, Any], check_only: bool = False) -> tuple[int, list[str]]:
    """Talk to Activity Envoy and redeem selected gifts using turkeys in bag."""
    turkeys = turkey_count(b)
    if turkeys <= 0:
        b.log(_("no turkey in bag to redeem"))
        return 0, []

    ex_cfg: dict[str, dict[str, Any]] = cfg.get("exchange", {})
    wanted_keys = [k for k in EXCHANGES if ex_cfg.get(k, {}).get("enabled", False)]
    if not wanted_keys:
        b.log(_("no items selected for Thanksgiving exchange"))
        return 0, []

    if check_only:
        b.log(_("have {n} turkeys; would exchange selected gifts with Activity Envoy", n=turkeys))
        return 0, []

    redeemed: list[str] = []
    total_spent = 0

    for key in wanted_keys:
        info = EXCHANGES[key]
        item_cfg = ex_cfg.get(key, {})
        cost = int(item_cfg.get("cost", info["default_cost"]))
        pat = str(info["pattern"])

        for _round in range(2):
            cur_turkeys = turkey_count(b)
            if cur_turkeys < cost:
                break
            if not _open_redeem_menu(b):
                b.close_dialogs()
                return total_spent, redeemed
            if not _can_redeem(b.dialog(), pat):
                break
            if b.choose(pat) is not None:
                _wait_turkey_deducted(b, cur_turkeys)
                b.close_dialogs()
                b.sleep(0.3)
                redeemed.append(key)
                total_spent += cost
                b.log(_("exchanged {name} for {cost} turkeys", name=item_name(key), cost=cost))
            else:
                break

    b.close_dialogs()
    return total_spent, redeemed


HUNTING_MAPS: tuple[int, ...] = (3401, 3402, 3404)


def done_today(name: str | None) -> bool:
    """True if Thanksgiving Hunt (2/2) is already marked done for this game day."""
    return DONE_KEY in chores.done_today(name)


def mark_done_today(b: Bot, reason: str = "2/2 completed") -> None:
    """Record that Thanksgiving Hunt's 2/2 daily entries are finished for today."""
    chores.mark(chores.who(b), DONE_KEY, reason)


def _hunt_option_text(d: dict[str, Any]) -> str | None:
    if not d.get("open"):
        return None
    rows: list[list[dict[str, Any]]] = d.get("top", {}).get("rows", [])
    for row in rows:
        for col in row:
            for opt in col.get("options", []):
                if re.search(r"Thanksgiving Hunt|click to join", opt, re.I):
                    return str(opt)
    return None


def can_join_hunt(d: dict[str, Any]) -> bool:
    """True if Activity Envoy has an available Thanksgiving Hunt entry (not 2/2 times)."""
    opt = _hunt_option_text(d)
    return opt is not None and "2/2" not in opt


def is_in_hunting_map(b: Bot) -> bool:
    """True if character is currently on any Thanksgiving hunting map."""
    mid = b.map_id()
    if mid in HUNTING_MAPS:
        return True
    try:
        name = atlas.name(mid)
        return "hunting" in name.lower()
    except Exception:
        return False


def enter_thanksgiving_hunt(b: Bot) -> bool:
    """Talk to Activity Envoy (on current map or in Mario Basin) and enter Thanksgiving Hunt."""
    if not talk_to_envoy(b):
        b.log(_("couldn't talk to Activity Envoy"))
        return False

    b.wait_dialog(3.0)
    d = b.dialog()
    opt = _hunt_option_text(d)
    if opt is None:
        b.close_dialogs()
        b.log(_("Thanksgiving Hunt is not available in Activity Envoy's menu"))
        return False
    if "2/2" in opt:
        b.close_dialogs()
        mark_done_today(b, "2/2 completed")
        b.log(_("Thanksgiving Hunt daily entries already used up (2/2)"), speak=True)
        return False
    is_second_entry = "1/2" in opt

    if b.choose(r"Thanksgiving Hunt|click to join") is None:
        b.close_dialogs()
        b.log(_("couldn't choose Thanksgiving Hunt from dialog"))
        return False

    end_time = b.clock.time() + 10.0
    tried_enter = False
    while b.clock.time() < end_time:
        if is_in_hunting_map(b):
            b.settled_map()
            if is_second_entry:
                mark_done_today(b, "2/2 completed")
            return True
        d2 = b.dialog()
        if d2.get("open"):
            top = d2.get("top")
            if can_join_hunt(d2) and not tried_enter:
                tried_enter = True
                b.key(b.p.get("enter_key", "enter"))
            elif top and b.find_entry(top, CONFIRM) is not None:
                b.choose(CONFIRM)
            elif top and b.find_entry(top, r"(?i)^\s*(enter|join|start)\b") is not None:
                b.choose(r"(?i)^\s*(enter|join|start)\b")
        b.sleep(0.25)

    b.close_dialogs()
    return False


def _apply_pet_choice(b: Bot, cfg: dict[str, Any]) -> list[str] | None:
    was = getattr(b, "pets_for_run", None)
    pet_cfg = str(cfg.get("pet") or "")
    b.pets_for_run = [] if pet_cfg == NO_PET else ([pet_cfg] if pet_cfg else None)
    return was


def hunt_one_entry(b: Bot, cfg: dict[str, Any], cur: int, total: int) -> bool:
    """Execute one Thanksgiving Hunt run until required turkeys collected or auto-exit."""
    was_pets = _apply_pet_choice(b, cfg)
    was_max_hp = getattr(b, "max_mob_hp", None)
    was_no_xp_card = getattr(b, "no_xp_card", False)
    b.no_xp_card = True
    try:
        if not is_in_hunting_map(b):
            dungeon.pet_ready(b)
            if not enter_thanksgiving_hunt(b):
                return False

        mid = b.map_id()
        hunt_mode = cfg.get("hunt_mode", "exchange")
        target_turkeys = required_turkeys(cfg) if hunt_mode == "exchange" else 0

        if hunt_mode == "exchange" and target_turkeys > 0:
            b.log(_("in Thanksgiving Hunt map {mid} ({cur} of {total}): hunting until {target} turkeys collected...",
                    mid=mid, cur=cur, total=total, target=target_turkeys), speak=True)
        else:
            b.log(_("in Thanksgiving Hunt map {mid} ({cur} of {total}): hunting until auto-exit...",
                    mid=mid, cur=cur, total=total), speak=True)

        def out() -> bool:
            if not is_in_hunting_map(b):
                return True
            if hunt_mode == "exchange" and target_turkeys > 0:
                if turkey_count(b) >= target_turkeys:
                    return True
            return False

        if cfg.get("skip_big_mobs", True):
            b.max_mob_hp = int(cfg.get("max_mob_hp", 40000))
        b.farm(mid, patrol="random", until=out, instance=True)

        # If still inside the hunting map because turkey target was reached, quit via Action menu
        if is_in_hunting_map(b):
            if hunt_mode == "exchange" and target_turkeys > 0 and turkey_count(b) >= target_turkeys:
                b.log(_("collected {n} turkeys (reached target {target}); quitting event via Action menu...",
                        n=turkey_count(b), target=target_turkeys), speak=True)
                b.leave_instance()

        b.settled_map()
        b.log(_("completed Thanksgiving Hunt entry {cur} of {total}",
                cur=cur, total=total), speak=True)
        b.sleep(2.0)
        return True
    finally:
        b.no_xp_card = was_no_xp_card
        b.max_mob_hp = was_max_hp
        b.pets_for_run = was_pets


def _check_supplies(b: Bot, cfg: dict[str, Any], check_only: bool) -> None:
    was_pets = _apply_pet_choice(b, cfg)
    try:
        dungeon.get_supplies(b, cfg, check_only)
    finally:
        b.pets_for_run = was_pets


def run_thanksgiving(b: Bot, cfg: dict[str, Any], check_only: bool = False) -> tuple[str, bool]:
    """Run Thanksgiving event for configured number of entries, then auto-redeem gifts."""
    if not is_in_hunting_map(b) and done_today(chores.who(b)):
        b.log(_("Thanksgiving Hunt is already done (2/2) for today - proceeding to exchange"), speak=True)
        if cfg.get("auto_exchange", True):
            redeem_thanksgiving(b, cfg, check_only=check_only)
        return _("Thanksgiving Hunt already done today (2/2)"), True

    total_entries = max(1, int(cfg.get("entries", 1)))
    hunt_mode = cfg.get("hunt_mode", "exchange")
    target_turkeys = required_turkeys(cfg) if hunt_mode == "exchange" else 0
    cur_turkeys = turkey_count(b)

    if check_only:
        _check_supplies(b, cfg, check_only=True)
        if hunt_mode == "exchange" and target_turkeys > 0:
            b.log(_("have {n} turkeys (target {target} for exchange); would run Thanksgiving Hunt up to {entries} time(s)",
                    n=cur_turkeys, target=target_turkeys, entries=total_entries))
        else:
            b.log(_("have {n} turkeys; would run Thanksgiving Hunt {entries} time(s) (full duration)",
                    n=cur_turkeys, entries=total_entries))
        if cfg.get("auto_exchange", True):
            redeem_thanksgiving(b, cfg, check_only=True)
        return _("would run Thanksgiving Hunt {n} time(s)", n=total_entries), True

    # If already have enough turkeys for exchange before starting
    if hunt_mode == "exchange" and target_turkeys > 0 and cur_turkeys >= target_turkeys:
        b.log(_("already have {n} turkeys (target {target}); skipping hunt and proceeding to exchange",
                n=cur_turkeys, target=target_turkeys), speak=True)
        if cfg.get("auto_exchange", True):
            redeem_thanksgiving(b, cfg, check_only=False)
        return _("Thanksgiving event finished (already had required turkeys)"), True

    completed = 0
    for entry_idx in range(total_entries):
        if done_today(chores.who(b)) and not is_in_hunting_map(b):
            b.log(_("Thanksgiving Hunt daily entries (2/2) finished - proceeding to exchange"), speak=True)
            break
        if not is_in_hunting_map(b):
            _check_supplies(b, cfg, check_only=False)
        b.log(_("starting Thanksgiving Hunt entry {cur} of {total}",
                cur=entry_idx + 1, total=total_entries), speak=True)
        if not hunt_one_entry(b, cfg, entry_idx + 1, total_entries):
            b.log(_("could not enter Thanksgiving Hunt (limit reached or entrance failed)"))
            break
        completed += 1

        # If target reached after this entry, no need to do further runs
        if hunt_mode == "exchange" and target_turkeys > 0 and turkey_count(b) >= target_turkeys:
            b.log(_("all required turkeys ({n}/{target}) collected; skipping remaining entries",
                    n=turkey_count(b), target=target_turkeys), speak=True)
            break

    if cfg.get("auto_exchange", True):
        b.log(_("auto-redeeming Thanksgiving gifts at Activity Envoy..."))
        redeem_thanksgiving(b, cfg, check_only=False)

    return _("Thanksgiving event finished ({n} entries)", n=completed), True


def run(b: Bot, cfg: dict[str, Any] | None = None, check_only: bool = False) -> tuple[str, bool]:
    cfg = cfg or config(b.p)
    ev = cfg.get("event", "thanksgiving")
    if ev == "thanksgiving":
        return run_thanksgiving(b, cfg, check_only=check_only)
    name = label(ev)
    b.log(_("unknown event: {name}", name=name))
    return _("unknown event: {name}", name=name), False
