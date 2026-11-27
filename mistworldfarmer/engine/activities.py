"""Every job the bot does besides farming, in one list (master plan, Phase 4: one Activity shape).

The Tools menu, the Before/After farming triggers, schedules and a player's script all name a job
by the same key and run it through `run`, so a new activity is one entry here: its key (the script
command and the results part), what the log calls it, whether a trigger runs it straight (a town
job with nothing to choose) or through its script command, whether it farms (held to the day's
farming window), and how it runs. Before this, triggers.JOBS/TOOLS/TOWN_JOBS and the script's
`_do_*` commands each kept their own copy.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import stats
from ..i18n import N_, _
from .botparts.base import Stopped

if TYPE_CHECKING:
    from .bot import Bot

Run = Callable[["Bot", bool, Mapping[str, Any]], object]


@dataclass(frozen=True)
class Activity:
    key: str                # the script command, the trigger step and the results part
    label: str              # what the log calls it (N_: translated when shown)
    run: Run                # (bot, check only, the script command's options) -> its result
    direct: bool = False    # a trigger runs it straight (a town job); else through its script command
    farms: bool = False     # it farms: held to the day's farming window, its loot is farming loot
    checks: bool = False    # it has a Check only (walks there, reads, changes nothing)
    town: str = ""          # the engine.town function a town job is
    listed: bool = True     # offered as it is in the trigger and schedule lists


def _town(fn: str) -> Run:
    def run(b: Bot, _check: bool, _o: Mapping[str, Any]) -> object:
        from . import town          # looked up when it runs, so tests can stand in for it
        job: Callable[[Bot], object] = getattr(town, fn)
        return job(b)
    return run


def _zone_rects(skill: str, map_id: int, zone_name: str) -> list[list[int]]:
    import os
    from .. import jsonio, paths
    try:
        with open(os.path.join(paths.DATA, "gathering_zones.json"), "rb") as f:
            zdata = jsonio.load(f)
    except Exception:
        return []
    entry = (zdata.get(skill) or {}).get(str(map_id)) if isinstance(zdata, dict) else None
    if not isinstance(entry, dict):
        return []
    rects: list[list[int]] = []
    for z in entry.get("zones") or []:
        if not zone_name or str(z.get("name") or "").casefold() == zone_name.casefold():
            for r in z.get("rects") or []:
                if isinstance(r, list) and len(r) >= 4:
                    rects.append([int(v) for v in r[:4]])
    return rects


def _multi_gather_config(b: Bot, slot_raw: object, base_cfg: dict[str, Any]) -> dict[str, Any]:
    import os
    from .. import jsonio, paths, settings
    from . import gathering
    path = os.path.join(paths.APP, "multi_gathering.json")
    try:
        with open(path, "rb") as f:
            data = jsonio.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return base_cfg
    cfg = dict(base_cfg)
    cfg["gang_fee_max"] = 1000000000
    cfg["outside"] = bool(data.get("outside", True))
    cfg["known_places"] = True
    saved_places = list(cfg.get("places") or [])
    for prof in settings.profiles():
        g = settings.load_profile(prof).get("gathering")
        if isinstance(g, dict) and isinstance(g.get("places"), list):
            for p in g["places"]:
                if isinstance(p, dict) and p not in saved_places:
                    saved_places.append(p)
    cfg["places"] = saved_places
    cfg["stop_vitality"] = int(data.get("stop_vitality") or 0)
    cfg["sell"] = bool(data.get("sell", True))
    cfg["minutes"] = int(data.get("minutes") or 0)
    try:
        idx = int(str(slot_raw))
    except (TypeError, ValueError):
        idx = -1
    accounts = data.get("accounts") or []
    if 0 <= idx < len(accounts) and isinstance(accounts[idx], dict):
        slot = accounts[idx]
        gangs: list[str] = []
        if slot.get("own_gang", data.get("own_gang", True)):
            gangs.append("")
        if slot.get("allied_gang") and str(slot.get("allied_name") or "").strip():
            gangs.append(str(slot.get("allied_name")).strip())
        if slot.get("other_gang") and str(slot.get("other_name") or "").strip():
            gangs.append(str(slot.get("other_name")).strip())
        cfg["gang"] = bool(gangs)
        cfg["gangs"] = gangs or [""]

        place_val = str(slot.get("place") or "")
        zone_val = str(slot.get("zone") or "").strip()
        x_val = int(slot.get("x") or 0)
        y_val = int(slot.get("y") or 0)
        actual_skill, _rank = gathering.read_skill(b)
        wanted_skill = str(slot.get("skill") or "")
        if wanted_skill and wanted_skill != "auto":
            if actual_skill and actual_skill != wanted_skill:
                b.log(_("Note: Multi Gathering set {wanted}, but character has {actual}",
                        wanted=_(wanted_skill), actual=_(actual_skill)))
        use_skill = actual_skill or (wanted_skill if wanted_skill != "auto" else "")
        map_id: int | None = None
        if place_val.startswith("map:"):
            try:
                map_id = int(place_val.split(":", 1)[1])
            except ValueError:
                map_id = None
        elif place_val:
            import re
            m = re.search(r"\((\d+)\)", place_val)
            if m:
                map_id = int(m.group(1))
        if (x_val > 0 or y_val > 0) and map_id and use_skill:
            custom = {"skill": use_skill, "map": map_id, "x": x_val, "y": y_val}
            cfg["places"] = [custom] + saved_places
            cfg["place"] = gathering.label(custom)
        elif map_id and use_skill:
            rects = _zone_rects(use_skill, map_id, zone_val)
            if zone_val and rects:
                search_place = {"skill": use_skill, "map": map_id, "x": 0, "y": 0, "search": True, "rects": rects}
                cfg["places"] = [search_place] + saved_places
                cfg["place"] = gathering.label(search_place)
            else:
                matched = ""
                for p in gathering.places(cfg, use_skill):
                    if int(p.get("map") or 0) == map_id:
                        matched = str(p.get("label") or "")
                        break
                if matched:
                    cfg["place"] = matched
                else:
                    search_place = {"skill": use_skill, "map": map_id, "x": 0, "y": 0, "search": True, "rects": rects}
                    cfg["places"] = [search_place] + saved_places
                    cfg["place"] = gathering.label(search_place)
        else:
            cfg["place"] = place_val
    return cfg


def _gather(b: Bot, check: bool, o: Mapping[str, Any]) -> object:
    from . import gathering
    cfg = gathering.config(b.p)
    if o.get("multi_gather_slot") is not None:
        cfg = _multi_gather_config(b, o.get("multi_gather_slot"), cfg)
    return gathering.run(b, cfg, check)


def _lose_xp(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
    from . import xploss
    return xploss.run(b, xploss.config(b.p), check)


def _chores(b: Bot, check: bool, o: Mapping[str, Any]) -> object:
    from . import chores
    return chores.run(b, chores.config(b.p), check, o.get("only") or None)


def _events(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
    from . import events
    return events.run(b, events.config(b.p), check)


def _bounties(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
    from . import chores
    return chores.collect_bounties(b, chores.config(b.p), check)


def _construction_scan(b: Bot, _check: bool, o: Mapping[str, Any]) -> object:
    from . import constructionscan
    if o.get("auto"):
        return constructionscan.scan_and_publish(b, o["auto"])
    if o.get("publish"):
        return constructionscan.publish(b, o["publish"])
    return constructionscan.scan(b)


def _crafting(key: str, setting: str, tool: str) -> Run:
    def run(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
        from . import crafting
        cfg: dict[str, Any] = b.p.get(setting) or {}
        if not cfg:
            raise Stopped(_("set it up first: Tools, {tool}", tool=_(tool)))
        return (crafting.gem_maker if key == "gem_maker" else crafting.enhance)(b, cfg, check)
    return run


def _gem_breaker(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
    from . import gembreak          # (it says "set it up first" itself)
    return gembreak.run(b, gembreak.config(b.p), check)


def _potion_maker(b: Bot, check: bool, _o: Mapping[str, Any]) -> object:
    from . import potionmaker      # (it says "set it up first" itself)
    return potionmaker.run(b, potionmaker.config(b.p), check)


def _dungeon(b: Bot, check: bool, o: Mapping[str, Any]) -> object:
    from . import dungeon
    return dungeon.run(b, dungeon.config(b.p), check, o.get("cave"))


def _sell_collection(b: Bot, _check: bool, o: Mapping[str, Any]) -> object:
    from . import town
    sold = town.sell_collection(b, o["kinds"])
    if not sold:
        b.log(_("nothing of the collection was sold"))
    return sold


# In the order the Tools menu and the trigger lists show them (the first seven: the old JOBS order)
ACTIVITIES: tuple[Activity, ...] = (
    Activity("sell_shards", N_("selling shards"), _town("sell_shards_now"), direct=True, town="sell_shards_now"),
    Activity("clean_bag", N_("cleaning the bag"), _town("clean_bag"), direct=True, town="clean_bag"),
    Activity("update_shard_prices", N_("updating shard prices"), _town("update_shard_prices"), direct=True, town="update_shard_prices"),
    Activity("gather", N_("gathering"), _gather, direct=True, checks=True),
    Activity("store_items", N_("storing items"), _town("store_items"), direct=True, town="store_items"),
    Activity("restock", N_("restocking supplies"), _town("restock_now"), direct=True, town="restock_now"),
    Activity("lose_xp", N_("losing experience"), _lose_xp, direct=True, checks=True),
    Activity("chores", N_("daily chores"), _chores, checks=True),
    Activity("gem_maker", N_("making gems"), _crafting("gem_maker", "gem_maker", N_("Gem Maker")), checks=True),
    Activity("gem_breaker", N_("breaking gems"), _gem_breaker, checks=True),
    Activity("potion_maker", N_("making potions"), _potion_maker, checks=True),
    Activity("enhance", N_("enhancing equipment"), _crafting("enhance", "enhancer", N_("Equipment Enhancer")),
             checks=True),
    # a dungeon farms: its loot is farming loot, so it has no results part of its own
    Activity("dungeon", N_("running a dungeon"), _dungeon, farms=True, checks=True),
    Activity("events", N_("running events"), _events, checks=True),
    Activity("sell_collection", N_("selling the collection"), _sell_collection),
    # on request only (Tools, Hunting bounties); the scanner is listed by how it publishes (triggers.TOOLS)
    Activity("bounties", N_("asking for hunting bounties"), _bounties, checks=True, listed=False),
    Activity("construction_scan", N_("scanning the constructions"), _construction_scan, listed=False),
)
BY_KEY: dict[str, Activity] = {a.key: a for a in ACTIVITIES}


def get(key: str) -> Activity:
    return BY_KEY[key]


def run(b: Bot, key: str, check: bool = False, options: Mapping[str, Any] | None = None) -> object:
    """One job, the same whoever asks for it (the Tools menu, a trigger, a schedule, a script):
    inside its own results part unless it farms."""
    a = BY_KEY[key]
    if a.farms:
        return a.run(b, check, options or {})
    with stats.doing(b, key):
        return a.run(b, check, options or {})
