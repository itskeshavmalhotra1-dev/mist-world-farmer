"""Town errands: the storehouse, shops, buyers - and keeping the bag supplied (restock).

Everything goes through the game's own menus: talk to the NPC (bot.talk), pick an entry with the
walking keys and Enter (bot.choose), and when the game asks "how many?" type the number into its
input box (agent `type` / `typeenter`, the window's own key handlers). Menu texts as the game
shows them:
  storehouse   "item storage" -> "1; 20 Memory Butterfly" ... (pick, then type how many to take out)
               "deposit item" -> your bag rows (pick, then type how many to put in)
  buyers       "Hello <name>, what would you like to sell?" -> "101 Milkfish; Unit price 670 arnebia"
  shops        not logged yet: the bot looks for an entry naming the item, or a "buy" entry first
Every menu met is written to logs/dialogs.log so the unknown ones can be learned from a live run.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .. import atlas, gamelang, jsonio as json, knowledge, paths, stats, travel
from ..clock import clock_for
from ..i18n import _
from . import bagrules, menus, teleskill
from .bot import CONFIRM, Bot, Stopped
from .botparts.travel import rides_free, ways_out
from .transactions import InventoryTransaction


NUMBER_AGENT = 56
INPUT_POLL = 0.025


def counts(b: Bot) -> dict[str, int]:
    """{item name (lower): count} from the bag, summed over its rows."""
    out: dict[str, int] = {}
    rx_ref = re.compile(r"\b(Refined|Flawless|Epic|Legendary|Heavenly|Godbless|Sophisticated|Rare|Great|Super)\b|\+\d+", re.I)

    def _add_item_names(raw: str, cnt: int, r: dict[str, Any] | None = None) -> None:
        n = raw.lower()
        out[n] = out.get(n, 0) + cnt
        if not raw:
            return
        clean = re.sub(r"(lzs\d*)+", "", raw).strip().lower()
        if clean and clean != n:
            out[clean] = out.get(clean, 0) + cnt
        unindexed = re.sub(r"^\d+\s*;\s*", "", clean).strip()
        if unindexed and unindexed != clean and unindexed != n:
            out[unindexed] = out.get(unindexed, 0) + cnt
        base = unindexed.split(";")[0].strip().lower()
        base_clean = re.sub(r"\s*\+\d+$", "", base).strip()
        if base and base != n and base != clean and base != unindexed:
            out[base] = out.get(base, 0) + cnt
        if base_clean and base_clean != base and base_clean != n and base_clean != clean and base_clean != unindexed:
            out[base_clean] = out.get(base_clean, 0) + cnt
        base_pure = re.sub(r"^(?:Refined|Flawless|Epic|Legendary|Great|Heavenly|Godbless|Super|Good|Rare|Common|Sophisticated|Normal)\s+", "", base_clean, flags=re.I).strip()
        if base_pure and base_pure != base_clean and base_pure != base and base_pure != n and base_pure != clean and base_pure != unindexed:
            out[base_pure] = out.get(base_pure, 0) + cnt
        base_norm = re.sub(r"\s+", " ", base_clean).strip()
        if base_norm and base_norm != base_clean and base_norm != base_pure:
            out[base_norm] = out.get(base_norm, 0) + cnt
        is_ref = (rx_ref.search(raw) is not None) or bool(re.search(r"\+\d+", raw)) or bool(re.search(r"enhancement\s+level:\s*[1-9]\d*", raw, re.I))
        if is_ref:
            eff_pure = base_pure or base_clean or base or unindexed
            if eff_pure:
                out["refined " + eff_pure] = out.get("refined " + eff_pure, 0) + cnt
                out["refined  " + eff_pure] = out.get("refined  " + eff_pure, 0) + cnt

    rows = b.bag_rows() if hasattr(b, "bag_rows") else (b.a.json("mem bag") if hasattr(b, "a") and hasattr(b.a, "json") and json.is_array(b.a.json("mem bag")) else [])
    for r in rows:
        raw = str(r.get("name") or "").strip()
        cnt = int(r.get("count") or 0)
        _add_item_names(raw, cnt, r)

    eq_rows = b.a.json("mem equipment") if hasattr(b, "a") and hasattr(b.a, "json") else []
    if json.is_array(eq_rows):
        for r in eq_rows:
            if not json.is_object(r):
                continue
            if int(r.get("equipped") or 0) <= 0:
                raw = str(r.get("name") or "").strip()
                cnt = max(1, int(r.get("count") or 1))
                _add_item_names(raw, cnt, r)
    return out


# The marks the client writes after an item's name in a list it builds itself, any of them
# (gamelang.MARKS): the storehouse's "deposit item" list reads "78 Memory Butterfly (restricted) ".
ITEM_MARKS = r"(\s*\((" + "|".join(re.escape(m.strip("()")) for m in gamelang.MARKS) + r")\))*"

ITEM_PREFIX = (
    r"(?:^|[;|]\s*|\d+\s*;\s*|\d+\s+)"
    r"(?:(?:Refined|Flawless|Epic|Legendary|Great|Heavenly|Godbless|Super|Good|Rare|Common|Sophisticated|Normal)\s+)?"
)


def item_pattern(item: str) -> str:
    """Matches the item in a menu entry ("187 Large Red Potion", "Large Red Potion; Unit price ...",
    "78 Memory Butterfly (restricted) ", "Refined  Bow of chaser; common (bound)") but not a longer
    name containing it ("Pet Large Red Potion"). Without the marks nothing restricted, locked or
    bound could be put into the storehouse: "the menu has no entry like ..."."""
    escaped = r"\s+".join(re.escape(w) for w in item.strip().split())
    return ITEM_PREFIX + escaped + r"(?:\s*\+\d+)?" + ITEM_MARKS + r"\s*($|[|;,])"


# ---------------------------------------------------------------------------------------- getting there
Npc = tuple[int, str, float, float]      # (map, name, x, y)


Place = tuple[int, tuple[float, float]]     # (map, cell)


def nearest(b: Bot, role: str | None, candidates: Sequence[Npc] | None = None,
            back_to: Place | None = None) -> Npc | None:
    """The NPC (of that role, or from candidates) cheapest to reach from here: (map, name, x, y).

    back_to: where the trip goes on to afterwards (a farming run's share trip goes back to its
    farm). The way there AND back is counted then: on the way there alone, the hearthstone's square
    looked cheapest from almost anywhere (one teleport), so a farm next to Tulip City sent the trip
    back to the city the run started in - and then walked the whole way out to the farm again.

    From inside a map we can't plan our way out of (a gang base, a dungeon) every other map costs
    the same unknown "leave", so the first NPC in the data's order used to win: a Gem Maker in the
    gang base entered from central plains went on to Mario Basin's craftsman. There, an NPC on
    another map is judged from where the way out puts us, when that is known (ways_out)."""
    here = b.map_id()
    me = b.me()
    out = ways_out(b).get(here) if travel.is_instance(here) else None
    best: tuple[float, Npc] | None = None
    rows = b.bag_rows()
    purse = travel.arnebia_in(rows)
    rides, off = travel.ride_rules(b.p, int(me.get("level") or 0))
    spells = teleskill.options(b)        # a Mage or an Elf teleports: a far city may be near

    def cost_of(from_map: int, from_cell: tuple[float, float], to_map: int, to_cell: tuple[float, float]) -> float | None:
        legs, cost = travel.plan(from_map, from_cell, to_map, to_cell, with_cost=True,
                                 transport=rides, vip=rides_free(b), avoid=off,
                                 bag=rows if b.p.get("use_travel_items", False) else [],
                                 hearth_home=b.p.get("hearth_home") or None, arnebia=purse, spells=spells)
        return None if legs is None else cost

    for mid, name, x, y in (candidates if candidates is not None else knowledge.service_npcs(role or "")):
        start_map, start_cell, extra = here, (me["x"], me["y"]), 0.0
        if out is not None and mid != here:
            start_map, start_cell, extra = out[0], out[1], travel.LEAVE_COST
        cost = cost_of(start_map, start_cell, mid, (x, y + 1))
        if cost is None:
            continue
        if back_to is not None:
            back = cost_of(mid, (x, y + 1), back_to[0], back_to[1])
            if back is None:
                continue          # no way on from there: a dead end for this trip
            cost += back
        cost += extra
        if best is None or cost < best[0]:
            best = (cost, (mid, name, x, y))
    return best[1] if best else None


def _step_out_first(b: Bot, target: Npc | None) -> bool:
    """In a gang base or a secret room whose way out we haven't seen yet (the run started inside
    it), with the chosen NPC on another map: get out first, so the nearest one is chosen from where
    we come out rather than by the data's order. Both send us back to where we came in from, which
    the map data can't say: from a secret room entered from Tulip City, every Shard Broker cost the
    same "leave", and the one on central plains won - a cable car ride away from Tulip City's.
    (Dungeons are left as travel plans it: a travel item may beat the Action menu there.) True
    when we left (choose again)."""
    here = b.map_id()
    closed = travel.gang_exit(here) is not None or atlas.kind(here) == "secret_room"
    if target is None or target[0] == here or not closed or here in ways_out(b):
        return False
    leave = getattr(b, "leave_here", None)
    if leave is None:
        return False
    b.log(_("leaving {map} first, to find the nearest from outside", map=atlas.label(here)))
    return bool(leave()) and b.map_id() != here


def visit(b: Bot, role: str | None, name: str | None = None, candidates: Sequence[Npc] | None = None,
          back_to: Place | None = None) -> str:
    """Travel to the nearest NPC of a role (or the named one, or the nearest of candidates) and open
    its menu. Returns its name. back_to: where the trip goes on to afterwards (nearest)."""
    def find(among: Sequence[Npc] | None) -> Npc | None:
        return nearest(b, role, among) if back_to is None else nearest(b, role, among, back_to)

    if candidates:
        target = find(candidates)
    elif name:
        named_candidates = [
            (n["map"], n["name"], n["x"], n["y"])
            for n in knowledge.data().get("npcs", [])
            if n["name"].lower() == name.lower()
        ]
        if named_candidates:
            target = find(named_candidates)
        else:
            known = knowledge.describe_npc(name)
            target = (known["map"], known["name"], known["x"], known["y"]) if known else None
    else:
        target = find(None)
    if not name and _step_out_first(b, target):
        target = find(candidates or None)
    if target is None and candidates:
        raise Stopped(_("none of the {n} places that could do it can be reached from {map}",
                        n=len(candidates), map=atlas.label(b.map_id())))
    if target is None:
        raise Stopped(_("I don't know where to find a {role} - visit one with the agent running first",
                        role=(role or name or "").replace("_", " ")))     # a named NPC has no role
    mid, npc, x, y = target
    b.last_visited_npc = (mid, npc, x, y)
    b.log(_("going to {npc} on {map}", npc=npc, map=atlas.label(mid)))
    stats.reading(b)            # the results: loot before the trip, fares on it, the trade after it
    # The walk up to the NPC is travelling too: most visits stay on one map (the next buyer in the
    # same town), where travel() has nothing to plan, so this keeps the Swift fusion beast on for it.
    was = getattr(b, "traveling", False)
    b.traveling = True
    try:
        b.travel(mid)
        stats.reading(b)
        if not b.talk(npc, (x, y)):
            alt_npc = "Ansible Space-Time Storehouse" if "soul" in npc.lower() else ("Soul Storehouse" if "storehouse" in npc.lower() else None)
            if alt_npc and b.talk(alt_npc, (x, y)):
                return npc
            raise Stopped(_("couldn't talk to {npc}", npc=npc))
    finally:
        b.traveling = was
    return npc


# ---------------------------------------------------------------------------------------- menus + typing
def wait_input(b: Bot, want_open: bool = True, seconds: float = 3.0) -> bool:
    end = clock_for(b).time() + seconds
    while clock_for(b).time() < end:
        s = b.a.json("input")
        if s.get("ok") and bool(s.get("open")) == want_open:
            return True
        b.sleep(INPUT_POLL)
    return False


def wait_menu_change(b: Bot, before: dict[str, Any] | None) -> None:
    """Use the state-aware wait when the bot has it, preserving the established fallback otherwise."""
    waiter = getattr(b, "wait_menu_change", None)
    if callable(waiter):
        waiter(before)
    else:
        b.sleep(0.4)


def type_text(b: Bot, text: str) -> bool:
    """Answer a box that asks for a name. True once the game has taken it."""
    if not wait_input(b, True):
        return False
    b.a.send("typeclear")
    r = b.a.send("type %s" % text)
    if not r.startswith("typed"):
        b.log(_("couldn't type into the game: {why}", why=r), speak=True)
        return False
    b.sleep(0.2)
    b.a.send("typeenter")
    return wait_input(b, False)


def type_number(b: Bot, n: int) -> bool:
    """Answer the game's "how many?" box. True once it has taken the number."""
    if not wait_input(b, True):
        return False
    before_menu = b.dialog().get("top")
    if int(getattr(b, "agent_version", 0) or 0) >= NUMBER_AGENT:
        answer = b.a.send("number %d" % int(n))
        if not answer.startswith("entered"):
            b.log(_("couldn't type into the game: {why}", why=answer), speak=True)
            return False
        if not wait_input(b, False):
            b.log(_("the game didn't take the number"), speak=True)
            return False
        wait_menu_change(b, before_menu)
        return True
    b.a.send("typeclear")
    r = b.a.send("type %d" % int(n))
    if not r.startswith("typed"):
        b.log(_("couldn't type into the game: {why}", why=r), speak=True)
        return False
    b.sleep(0.05)
    b.a.send("typeenter")
    if not wait_input(b, False):
        b.log(_("the game didn't take the number"), speak=True)
        return False
    wait_menu_change(b, before_menu)
    # some menus ask "are you sure?" after the number
    d = b.dialog()
    if d["open"] and d.get("top") and b.find_entry(d["top"], CONFIRM) is not None:
        c_loc = b.find_entry(d["top"], CONFIRM)
        if c_loc and menus.choose_at(b, d["top"], c_loc[0], c_loc[1]):
            b.wait_menu_change(d["top"], seconds=0.15)
        else:
            b.choose(CONFIRM)
    return True


def pick(b: Bot, pattern: str, quantity: int | None = None) -> bool:
    """Choose an entry; if the game then asks how many, type quantity."""
    entry = b.choose(pattern)
    if entry is None:
        return False
    if quantity is None:
        return True
    flags = int(entry.get("flags") or 0)
    if "flags" in entry and not (flags & 1):
        if (flags & 2) or entry.get("f"):
            d = b.dialog()
            if d.get("open") and d.get("top") and b.find_entry(d["top"], CONFIRM) is not None:
                c_loc = b.find_entry(d["top"], CONFIRM)
                if c_loc and menus.choose_at(b, d["top"], c_loc[0], c_loc[1]):
                    b.wait_menu_change(d["top"], seconds=0.15)
                else:
                    b.choose(CONFIRM)
        return True
    return type_number(b, quantity)


def open_section(b: Bot, pattern: str) -> bool:
    """Choose a section and wait for its menu, rather than accepting the still-open parent."""
    d = b.dialog()
    if d.get("open") and d.get("top") and b.find_entry(d["top"], pattern) is not None:
        before: dict[str, Any] = d["top"]
        depth = max(1, int(d.get("open") or 1))
        if b.choose(pattern) is None:
            return False
        after = b.wait_menu_change(before, seconds=2.0, depth=depth)
        top: dict[str, Any] = after.get("top") or {}
        return bool(int(after.get("open") or 0) >= depth and top
                    and (top.get("sig") != before.get("sig") or top.get("rows") != before.get("rows")))
    return False


# ---------------------------------------------------------------------------------------- errands
def in_town(b: Bot, npcs: Sequence[Npc]) -> list[Npc]:
    """The NPCs of a restock trip's town (b.restock_town, a map id) - all of them outside a trip:
    a trip never goes on from one town to another for its errands."""
    town_map: int | None = getattr(b, "restock_town", None)
    return list(npcs) if town_map is None else [n for n in npcs if n[0] == town_map]


def _is_equipment(top: dict[str, Any] | None, loc: tuple[int, int] | None) -> bool:
    if not top or not loc:
        return False
    try:
        r, c = loc
        rows = top.get("rows") or []
        e = rows[r][c]
        if isinstance(e, dict) and "flags" in e:
            return not bool(int(e.get("flags") or 0) & 1)
    except (IndexError, TypeError, KeyError):
        pass
    return False


def _select_storehouse_branch(b: Bot, storehouse: str | None) -> bool:
    """If the open storehouse dialog presents choices between Soul Storehouse and
    Ansible Space-Time Storehouse, choose the specified branch."""
    if not storehouse:
        return True
    d = b.dialog()
    if not d.get("open") or not d.get("top"):
        return False
    top = d["top"]
    sh_clean = storehouse.strip()
    pat = r"(?:^|[;|]\s*)" + re.escape(sh_clean) + r"(?:\s*\(restricted\))*\s*($|[|;,])"
    if b.find_entry(top, pat) is not None:
        open_section(b, pat)
        return True
    if b.find_entry(top, re.escape(sh_clean)) is not None:
        open_section(b, re.escape(sh_clean))
        return True
    return False


STOREHOUSE_WITHDRAW_SECTIONS = (
    r"^\s*take\s+out\s+equipment",
    r"^\s*take\s+equipment",
    r"^\s*take\s+out\s+item",
    r"^\s*take\s+item",
    r"^\s*take\s+out",
    r"^\s*take\b",
    r"^\s*withdraw\s+equipment",
    r"^\s*withdraw\s+item",
    r"^\s*withdraw\b",
    r"^\s*equipment\s+storage",
    r"^\s*item\s+storage",
    r"^\s*equipment\s+store",
    r"^\s*item\s+store",
    r"^\s*storage\b",
    r"^\s*store\b",
)

STOREHOUSE_DEPOSIT_SECTIONS = (
    r"^\s*deposit\s+equipment",
    r"^\s*deposit\s+item",
    r"^\s*deposit\b",
    r"^\s*store\s+equipment",
    r"^\s*store\s+item",
    r"^\s*put\s+in",
)


def is_equipment_name(item: str) -> bool:
    clean = re.sub(r"(lzs\d*)+", "", str(item)).strip().lower()
    if any(k in clean for k in ("shard", "potion", "crystal", "scroll", "fish", "ore", "herb", "ticket", "card", "rune", "meat", "box", "key")):
        return False
    if any(k in clean for k in ("bow", "sword", "staff", "blade", "axe", "dagger", "wand", "shield", "helm", "armor", "armour", "boots", "gloves", "belt", "ring", "necklace", "cloak", "robe", "equipment")):
        return True
    return False


def storehouse_sections(item: str, mode: str = "withdraw") -> tuple[str, ...]:
    """Return appropriate storehouse sections prioritizing item storage vs equipment storage."""
    is_eq = is_equipment_name(item)
    if mode == "withdraw":
        if is_eq:
            return (
                r"^\s*take\s+out\s+equipment",
                r"^\s*take\s+equipment",
                r"^\s*withdraw\s+equipment",
                r"^\s*equipment\s+storage",
                r"^\s*equipment\s+store",
                r"^\s*take\s+out",
                r"^\s*take\b",
                r"^\s*withdraw\b",
                r"^\s*item\s+storage",
                r"^\s*take\s+out\s+item",
                r"^\s*take\s+item",
                r"^\s*withdraw\s+item",
                r"^\s*item\s+store",
                r"^\s*storage\b",
                r"^\s*store\b",
            )
        else:
            return (
                r"^\s*item\s+storage",
                r"^\s*take\s+out\s+item",
                r"^\s*take\s+item",
                r"^\s*withdraw\s+item",
                r"^\s*item\s+store",
                r"^\s*take\s+out",
                r"^\s*take\b",
                r"^\s*withdraw\b",
                r"^\s*equipment\s+storage",
                r"^\s*take\s+out\s+equipment",
                r"^\s*take\s+equipment",
                r"^\s*withdraw\s+equipment",
                r"^\s*equipment\s+store",
                r"^\s*storage\b",
                r"^\s*store\b",
            )
    else:  # deposit
        if is_eq:
            return (
                r"^\s*deposit\s+equipment",
                r"^\s*store\s+equipment",
                r"^\s*equipment\s+storage",
                r"^\s*deposit\b",
                r"^\s*put\s+in",
                r"^\s*deposit\s+item",
                r"^\s*store\s+item",
                r"^\s*item\s+storage",
            )
        else:
            return (
                r"^\s*deposit\s+item",
                r"^\s*store\s+item",
                r"^\s*item\s+storage",
                r"^\s*deposit\b",
                r"^\s*put\s+in",
                r"^\s*deposit\s+equipment",
                r"^\s*store\s+equipment",
                r"^\s*equipment\s+storage",
            )


def withdraw(b: Bot, item: str, n: int, *, stock: int | None = None, storehouse: str | None = None):
    """Take up to n of item out of the storehouse (the trip's town's, during a restock). Returns
    how many arrived in the bag, counted at the storehouse: a potion drunk on the way there is not a
    potion the storehouse failed to hand over (that used to send the trip on to a shop)."""
    houses: list[Npc] | None = None
    if storehouse:
        houses = [c for c in knowledge.service_npcs("storehouse") if storehouse.lower() in c[1].lower()]
        if not houses:
            houses = [
                (c["map"], c["name"], c["x"], c["y"])
                for c in knowledge.data().get("npcs", [])
                if storehouse.lower() in c["name"].lower()
            ]
    else:
        houses = in_town(b, knowledge.service_npcs("storehouse"))
    if not houses:
        b.log(_("no storehouse in {map} - not taking {item} out", map=atlas.label(getattr(b, "restock_town", None) or 0),
                item=item))
        return 0
    visit(b, "storehouse", candidates=houses)
    target_npc: Npc = getattr(b, "last_visited_npc", None) or (b.map_id(), "storehouse", 0, 0)
    _tmid, npc_name, nx, ny = target_npc
    before = counts(b).get(item.lower(), 0)
    got_count = 0
    consecutive_fails = 0

    try:
        if stock is not None:
            n = _restock_quantity(b, item, stock)
            if n <= 0:
                return 0
        _select_storehouse_branch(b, storehouse)
        for section in storehouse_sections(item, "withdraw"):
            if open_section(b, section):
                break
        else:
            b.log(_("the storehouse menu has no item storage"), speak=True)
            return 0
        d = b.dialog()
        pat = item_pattern(item)
        loc = b.find_entry(d.get("top"), pat)
        if loc is None:
            b.log(_("no {item} in the storehouse", item=item), speak=True)
            b.__dict__.setdefault("storehouse_lacks", set()).add(item.strip().lower())
            return 0

        is_equip = _is_equipment(d.get("top"), loc)
        if not is_equip:
            # Stackable item: type quantity and take all in one go
            if pick(b, pat, n):
                for _w in range(5):
                    b.sleep(0.02)
                    cur_step = counts(b).get(item.lower(), 0)
                    if cur_step > before:
                        got_count = cur_step - before
                        break
                else:
                    got_count = n
        else:
            # Non-stackable equipment: pick directly via choose_at for ultra-fast withdrawal
            while got_count < n and not b.stop_event.is_set():
                d = b.dialog()
                if not d.get("open"):
                    if hasattr(b, "key") and hasattr(b, "wait_dialog"):
                        b.key(b.p.get("enter_key", "enter"))
                        b.wait_dialog(0.25)
                    if not b.dialog().get("open"):
                        if not b.talk(npc_name, (nx, ny)):
                            consecutive_fails += 1
                            if consecutive_fails >= 2:
                                break
                            continue
                    _select_storehouse_branch(b, storehouse)
                    for section in storehouse_sections(item, "withdraw"):
                        if open_section(b, section):
                            break
                    d = b.dialog()

                top = d.get("top") or {}
                loc = b.find_entry(top, pat)
                if loc is None:
                    # If item not visible directly, try opening withdraw section
                    for section in storehouse_sections(item, "withdraw"):
                        if b.find_entry(top, section) is not None:
                            open_section(b, section)
                            d = b.dialog()
                            top = d.get("top") or {}
                            loc = b.find_entry(top, pat)
                            break

                if loc is None:
                    break

                r, c = loc
                chosen = False
                if menus.choose_at(b, top, r, c):
                    chosen = True
                    b.wait_menu_change(top, seconds=0.15)
                else:
                    chosen = pick(b, pat, 1)

                if not chosen:
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        break
                    continue

                got_count += 1
                consecutive_fails = 0
    finally:
        b.close_dialogs()

    if stock is not None:
        transaction = InventoryTransaction({item.lower(): before})
        transaction.expect(item, n)
        after = transaction.settle(b)
    else:
        after = counts(b)
    got = max(after.get(item.lower(), 0) - before, got_count)
    if stock is not None and got > 0:
        getattr(b, "storehouse_lacks", set[str]()).discard(item.lower())
    if got > 0:
        b.storehouse_is_full = False
        refused(b).clear()          # something came out: there may be room again
    b.log(_("took {n} {item} out of the storehouse", n=got, item=item))
    stats.moved(b, "withdrawn", item, got)
    return got


def storehouse_count(b: Bot, item: str = "", *, storehouse: str | None = None) -> int:
    """Count matching items (or all occupied slots if item is empty) in the storehouse equipment storage."""
    houses: list[Npc] | None = None
    if storehouse:
        houses = [c for c in knowledge.service_npcs("storehouse") if storehouse.lower() in c[1].lower()]
        if not houses:
            houses = [
                (c["map"], c["name"], c["x"], c["y"])
                for c in knowledge.data().get("npcs", [])
                if storehouse.lower() in c["name"].lower()
            ]
    else:
        houses = in_town(b, knowledge.service_npcs("storehouse"))
    if not houses:
        return 0

    visit(b, "storehouse", candidates=houses)
    total_found = 0
    total_occupied = 0
    pattern = item_pattern(item) if item else None
    try:
        _select_storehouse_branch(b, storehouse)
        for section in storehouse_sections(item, "withdraw"):
            if open_section(b, section):
                break
        else:
            return 0

        d = b.dialog()
        top = d.get("top") or {}
        rows = top.get("rows") or []
        for row in rows:
            for e in row:
                if not e:
                    continue
                texts = b.entry_texts(e)
                if not any(texts):
                    continue
                total_occupied += 1
                if pattern:
                    if any(travel.matches(pattern, t) for t in texts):
                        total_found += 1
                else:
                    total_found += 1

        is_soul = bool(storehouse and "soul" in storehouse.lower())
        if total_occupied >= (30 if is_soul else 60):
            b.storehouse_is_full = True
        else:
            b.storehouse_is_full = False
    finally:
        b.close_dialogs()

    return total_found


# What the game says when the storehouse has no free place for an item it doesn't hold yet (seen
# 2026-11-18, the menu closing after it as after every deposit). An item it already holds a stack
# of still goes in: only that item is refused, not the rest of the trip.
STOREHOUSE_FULL = re.compile(
    r"\b(?:item|equipment|soul|character|account|vault|warehouse|storehouse)?\s*"
    r"(?:storage|warehouse|storehouse|space)?\s*"
    r"(?:is\s+full|is\s+already\s+full|full|no\s+free\s+space|not\s+enough\s+space|has\s+no\s+room|no\s+room|cannot\s+store)\b",
    re.I
)
FULL_HEARD = 0.5      # seconds the game is given to say so after a deposit that took nothing


def refused(b: Bot) -> set[str]:
    """The items (lower case) the storehouse turned away as full this run: not taken there again
    until something comes out of it (withdraw)."""
    got: set[str] = b.__dict__.setdefault("storehouse_refused", set())
    return got


def _heard_full(b: Bot, since: int) -> bool:
    from .chores import said_since
    clock = clock_for(b)
    end = clock.time() + FULL_HEARD
    while True:
        if any(STOREHOUSE_FULL.search(line) for line in said_since(b, since)):
            return True
        if clock.time() >= end:
            return False
        b.sleep(0.25)


def deposit(b: Bot, item: str, n: int | None=None, *, storehouse: str | None = None):
    """Put n (default: all) of item into the storehouse. One the storehouse turned away as full
    this run isn't taken there again (refused)."""
    item_clean = re.sub(r"\s+", " ", item.strip().lower())
    have = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
    base_pure = ""
    if have <= 0:
        base_pure = re.sub(r"^(?:refined|flawless|epic|legendary|great|heavenly|godbless|super|good|rare|common|sophisticated|normal)\s+", "", item_clean).strip()
        have = counts(b).get(base_pure, 0)
        if have > 0:
            item = base_pure
    if have <= 0:
        return 0
    if item.strip().lower() in refused(b):
        b.log(_("not taking {item} to the storehouse: it is full and holds none yet", item=item))
        return 0
    target_count = have if n is None else min(n, have)

    houses: list[Npc] | None = None
    if storehouse:
        houses = [c for c in knowledge.service_npcs("storehouse") if storehouse.lower() in c[1].lower()]
        if not houses:
            houses = [
                (c["map"], c["name"], c["x"], c["y"])
                for c in knowledge.data().get("npcs", [])
                if storehouse.lower() in c["name"].lower()
            ]
    else:
        houses = in_town(b, knowledge.service_npcs("storehouse"))

    visit(b, "storehouse", candidates=houses)
    target_npc: Npc = getattr(b, "last_visited_npc", None) or (b.map_id(), "storehouse", 0, 0)
    _tmid, npc_name, nx, ny = target_npc

    from .chores import said_mark
    since = said_mark(b)
    put_count = 0
    consecutive_fails = 0

    try:
        def _ensure_deposit_section() -> bool:
            d = b.dialog()
            if not d.get("open"):
                if nx and ny:
                    b.talk(npc_name, (nx, ny))
                else:
                    b.talk(npc_name)
                b.wait_dialog(1.5)
                d = b.dialog()
                if not d.get("open"):
                    return False
            _select_storehouse_branch(b, storehouse)
            top = b.dialog().get("top") or {}
            pat = item_pattern(item)
            if b.find_entry(top, pat) is not None or (base_pure and b.find_entry(top, item_pattern(base_pure)) is not None):
                return True
            for section in storehouse_sections(item, "deposit"):
                if b.find_entry(top, section) is not None:
                    if open_section(b, section):
                        return True
            return False

        if not _ensure_deposit_section():
            b.log(_("the storehouse menu has no deposit"), speak=True)
            return 0

        while put_count < target_count and not b.stop_event.is_set():
            curr_have = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
            if curr_have <= 0 and base_pure:
                curr_have = counts(b).get(base_pure, 0)
            if curr_have <= have - target_count or curr_have <= 0:
                break

            if not _ensure_deposit_section():
                consecutive_fails += 1
                if consecutive_fails >= 2:
                    break
                continue

            top = b.dialog().get("top") or {}
            pat = item_pattern(item)
            loc = b.find_entry(top, pat)
            if loc is None and base_pure:
                pat = item_pattern(base_pure)
                loc = b.find_entry(top, pat)
            if loc is None:
                break

            is_eq = _is_equipment(top, loc)
            step_qty = 1 if is_eq else (target_count - put_count)
            before_step = curr_have

            if not pick(b, pat, step_qty):
                consecutive_fails += 1
                if consecutive_fails >= 2 or _heard_full(b, since):
                    refused(b).add(item.strip().lower())
                    b.storehouse_is_full = True
                    b.log(_("the storehouse is full: {item} has no place in it", item=item))
                    break
                continue

            for _conf in range(3):
                d_conf = b.dialog()
                if not d_conf.get("open") or not d_conf.get("top"):
                    break
                if b.find_entry(d_conf["top"], CONFIRM) is not None:
                    b.choose(CONFIRM)
                    b.sleep(0.04)
                else:
                    break

            deposited = False
            for _w in range(15):
                b.sleep(0.02)
                after_step = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
                if after_step <= 0 and base_pure:
                    after_step = counts(b).get(base_pure, 0)
                if after_step < before_step:
                    put_count += (before_step - after_step)
                    deposited = True
                    consecutive_fails = 0
                    break
                if not b.dialog().get("open"):
                    b.sleep(0.02)
                    after_step = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
                    if after_step <= 0 and base_pure:
                        after_step = counts(b).get(base_pure, 0)
                    if after_step < before_step:
                        put_count += (before_step - after_step)
                        deposited = True
                        consecutive_fails = 0
                    break

            if not deposited:
                if not is_eq:
                    break
                consecutive_fails += 1
                if consecutive_fails >= 2:
                    break
    finally:
        b.close_dialogs()

    curr_now = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
    if curr_now <= 0 and base_pure:
        curr_now = counts(b).get(base_pure, 0)
    put = max(have - curr_now, put_count)
    if (curr_now > have - target_count and consecutive_fails > 0) or _heard_full(b, since):
        refused(b).add(item.strip().lower())
        b.storehouse_is_full = True
        b.log(_("the storehouse is full: {item} has no place in it", item=item))
    if put > 0:
        b.__dict__.setdefault("storehouse_lacks", set()).discard(item.strip().lower())   # it has some now
    b.log(_("put {n} {item} into the storehouse", n=put, item=item))
    stats.moved(b, "stored", item, put)
    return put


# Which shops turned out to stock what, learned on the way: {"<map>|<npc>": {"sells": [...],
# "lacks": [...]}} (lower-cased items). The game data records one potion seller's menu (Apothecary
# Phillips, Giant Rock City); every town has one, and the game describes nearly all of them alike.
SHOPS_SEEN = "_learned_shops.json"   # in the profiles folder (the player's own, never in a release)
BUY_TRIES = 3           # shops tried, nearest first, before an item is given up as not for sale
_shops_lock = threading.Lock()


def _shop_key(npc: Npc) -> str:
    return "%d|%s" % (npc[0], npc[1].strip().lower())


def _shops_seen() -> dict[str, dict[str, list[str]]]:
    try:
        with open(os.path.join(paths.PROFILES, SHOPS_SEEN), encoding="utf-8") as f:
            got = json.load(f)
        return got if json.is_object(got) else {}
    except (OSError, ValueError):
        return {}


def _save_shops_seen(seen: dict[str, dict[str, list[str]]]) -> None:
    try:
        with open(os.path.join(paths.PROFILES, SHOPS_SEEN), "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def note_shop(npc: Npc, item: str, sells: bool) -> None:
    """Remember that this shop's menu listed the item (or didn't). A menu that lists it also wipes
    out the misses counted against it (note_miss)."""
    with _shops_lock:
        seen = _shops_seen()
        row = seen.setdefault(_shop_key(npc), {"sells": [], "lacks": []})
        want, other = ("sells", "lacks") if sells else ("lacks", "sells")
        name = item.strip().lower()
        drop = [other, "missed", "missed_twice"] if sells else [other]
        for key in drop:
            if key in row:
                row[key] = [x for x in row[key] if x != name]
        if name not in row.setdefault(want, []):
            row[want].append(name)
        _save_shops_seen(seen)


# A town shop whose list has the item is taken off the list of shops for it only when its menu
# didn't show it this many times in a row. The lists were read in two towns (2026-11-19) and every
# town's shop of the kind is assumed alike: one misread menu mustn't hide a shop that sells it, but
# a shop that really doesn't stock it mustn't be walked to on every restock either.
MISSES_TO_LACK = 2


def note_miss(npc: Npc, item: str) -> int:
    """A listed shop's menu didn't show the item: count it. Returns the misses in a row so far
    (MISSES_TO_LACK or more: the shop is left out for it, shops_for)."""
    with _shops_lock:
        seen = _shops_seen()
        row = seen.setdefault(_shop_key(npc), {"sells": [], "lacks": []})
        name = item.strip().lower()
        once, twice = row.setdefault("missed", []), row.setdefault("missed_twice", [])
        if name in twice:
            return MISSES_TO_LACK
        if name in once:
            row["missed"] = [x for x in once if x != name]
            twice.append(name)
            _save_shops_seen(seen)
            return MISSES_TO_LACK
        once.append(name)
        _save_shops_seen(seen)
        return 1


def listed_shop(npc: Npc, item: str) -> bool:
    """The item is on the list every town shop of this NPC's kind sells (knowledge.shop_lists): one
    menu read without it doesn't make this shop one that lacks it (note_miss). The gang camps'
    sellers aren't town shops - their menus were never read."""
    role = knowledge.listed_role(item)
    return bool(role) and atlas.kind(npc[0]) != "gang" and npc in knowledge.service_npcs(role or "")


def shops_for(item: str) -> list[Npc]:
    """Where the item may be bought for arnebia: the shops the game data has seen selling it, and
    every NPC of the kind that sells it (every town's potion seller or grocer). A town shop whose
    list has it stays one until its menu didn't show it MISSES_TO_LACK times in a row (a "lacks"
    note an older bot wrote from one reading doesn't count - a Tulip City Elf that once misread
    Pharmacist Evan's menu went to Violet City for its potions for good); any other is left out
    once its menu turned out not to stock it. The nearest one is visited first (buy)."""
    name = item.strip().lower()
    seen = _shops_seen()
    out: list[Npc] = [t[:4] for t in knowledge.traders(item, "sell") if t[5] == "arnebia"]
    role = knowledge.shop_role(item)
    for npc in knowledge.service_npcs(role) if role else []:
        row = seen.get(_shop_key(npc), {})
        gone = (name in row.get("missed_twice", []) if listed_shop(npc, item)
                else name in row.get("lacks", []))
        if npc not in out and not gone:
            out.append(npc)
    return out


def _buy_menu(b: Bot, item: str) -> bool:
    """With a shop's menu open: get to the list that names the item. True when it is there."""
    d = b.dialog()
    if not d.get("open") or not d.get("top") or b.find_entry(d["top"], item_pattern(item)) is None:
        # a top menu first ("buy goods", "buy", "shop")
        for section in (r"^\s*buy\b", r"\bbuy goods", r"\bshop\b", r"\bpurchase"):
            if open_section(b, section):
                break
        d = b.dialog()
    if not d.get("open") or not d.get("top") or b.find_entry(d["top"], item_pattern(item)) is None:
        b.note_dialog(d, "looking for %s to buy" % item)
        return False
    return True


def _not_on_menu(b: Bot, shop: Npc, item: str) -> None:
    """The shop's menu didn't list the item. A town shop whose list has it is noted as lacking it
    only after MISSES_TO_LACK misses in a row (one may be a misread menu, or not its own); any other
    is at once, and isn't tried for it again."""
    if listed_shop(shop, item):
        if note_miss(shop, item) < MISSES_TO_LACK:
            b.log(_("{npc}'s menu didn't show {item} - see logs\\dialogs.log; trying the next nearest shop",
                    npc=shop[1], item=item))
            return
    else:
        note_shop(shop, item, False)
    b.log(_("{npc} doesn't sell {item} - trying the next nearest shop", npc=shop[1], item=item))


def buy(b: Bot, item: str, n: int, *, stock: int | None = None) -> int:
    """Buy n of item at the nearest shop that sells it: the shops seen selling it and every one of
    the kind that should (a potion seller, a grocer), nearest by travel first. Before, only the one
    shop whose menu the game data held was ever used - a Priest farming Flaming Mountain went to
    Giant Rock City for potions every time. A shop whose menu turns out not to list it is
    remembered and the next nearest tried (BUY_TRIES). Returns how many arrived."""
    if knowledge.shop_role(item) == "shard_market" or item.strip().lower().endswith("shard"):
        got_storage = 0
        if item.strip().lower() not in getattr(b, "storehouse_lacks", set()):
            b.log(_("checking storehouse for {item} before buying", item=item))
            got_storage = withdraw(b, item, n)
        if got_storage >= n:
            b.log(_("got all {n} {item} from storehouse, skipping market purchase", n=got_storage, item=item), speak=True)
            return got_storage
        needed = n - got_storage
        if got_storage > 0:
            b.log(_("got {got} {item} from storehouse; buying remaining {rem} at market",
                    got=got_storage, item=item, rem=needed), speak=True)
        return got_storage + buy_shards_at_market(b, item, needed)
    shops = shops_for(item)
    if not shops:
        if knowledge.category(item) == "weapon":
            shops = knowledge.service_npcs("weapon_merchant")
    if not shops:
        raise Stopped(_("I don't know which shop sells {item}", item=item))
    shops = in_town(b, shops)
    if not shops:
        b.log(_("no shop in {map} sells {item} - not going to another town for it",
                map=atlas.label(getattr(b, "restock_town", None) or 0), item=item), speak=True)
        return 0
    have = counts(b)
    before = have.get(item.lower(), 0)
    if stock is None:
        n = _affordable_quantity(b, item, n, have)
        if n <= 0:
            return 0
    tried: list[Npc] = []
    silent: list[Npc] = []
    for _try in range(BUY_TRIES):
        target = nearest(b, None, [s for s in shops if s not in tried])
        if target is None:
            break
        tried.append(target)
        try:
            visit(b, None, candidates=[target])
        except Stopped as e:
            if b.stop_event.is_set() or e.final:
                raise
            # a shop that doesn't answer is not a shop that doesn't sell it: not noted, next one
            b.log(_("{npc} didn't answer ({why}) - trying the next nearest shop", npc=target[1], why=e))
            silent.append(target)
            continue
        try:
            if stock is not None:
                n = _affordable_quantity(b, item, _restock_quantity(b, item, stock), counts(b))
                if n <= 0:
                    return 0
                before = counts(b).get(item.lower(), 0)
            # a potion seller's verified list: the item by its index, no walk down the list
            if menus.pick(b, menus.POTION_SHOP, name=item) is not None:
                note_shop(target, item, True)
                type_number(b, n)
            elif not _buy_menu(b, item):
                _not_on_menu(b, target, item)
                continue
            else:
                note_shop(target, item, True)
                picked_count = 0
                if pick(b, item_pattern(item), n):
                    picked_count += 1
                if n > 1 and (counts(b).get(item.lower(), 0) - before < n):
                    pat = item_pattern(item)
                    while (counts(b).get(item.lower(), 0) - before < n) and (picked_count < n) and not b.stop_event.is_set():
                        if not _buy_menu(b, item):
                            break
                        d = b.dialog()
                        top = d.get("top") or {}
                        loc = b.find_entry(top, pat)
                        if loc is None:
                            break
                        r, c = loc
                        chosen = False
                        if menus.choose_at(b, top, r, c):
                            chosen = True
                            d = b.wait_menu_change(top, seconds=0.15)
                            if d.get("open") and d.get("top") and b.find_entry(d["top"], CONFIRM) is not None:
                                c_loc = b.find_entry(d["top"], CONFIRM)
                                if c_loc and menus.choose_at(b, d["top"], c_loc[0], c_loc[1]):
                                    b.wait_menu_change(d["top"], seconds=0.12)
                                else:
                                    b.choose(CONFIRM)
                        else:
                            chosen = pick(b, pat, 1)

                        if not chosen:
                            break
                        picked_count += 1
                        b.sleep(0.02)
        finally:
            b.close_dialogs()
        if stock is not None:
            transaction = InventoryTransaction({item.lower(): before})
            transaction.expect(item, n)
            after = transaction.settle(b)
        else:
            after = counts(b)
        got = max(after.get(item.lower(), 0) - before, picked_count)
        b.log(_("bought {n} {item}", n=got, item=item))
        stats.moved(b, "bought", item, got)
        return got
    if tried and silent == tried:
        # not "no shop lists it": none of them got to say (the restock says so and goes on)
        raise Stopped(_("none of the shops that sell {item} answered", item=item))
    b.log(_("no shop near enough lists {item} - see logs\\dialogs.log", item=item), speak=True)
    return 0


def _affordable_quantity(b: Bot, item: str, n: int, have: dict[str, int]) -> int:
    """Apply the shop's existing currency limit to a requested quantity."""
    price = knowledge.unit_price(item)
    money = have.get(stats.ARNEBIA)
    keep = int(b.p.get("gem_keep_arnebia", 0) or 0)
    if keep > 0 and money is not None:
        money = max(0, money - keep)
    if price and money is not None and n * price > money:
        afford = money // price
        if afford <= 0:
            b.log(_("not enough arnebia for one {item}: {price} each, {money} in the bag",
                    item=item, price=price, money=have.get(stats.ARNEBIA)), speak=True)
            return 0
        b.log(_("buying {n} {item}, not {want}: that is what {money} arnebia pays for at {price} each",
                n=afford, item=item, want=n, money=money, price=price))
        return afford
    return n


def sell(b: Bot, item: str, n: int | None=None):
    """Sell n (default: all) of item to an NPC the game has shown buying it (best price), or else
    the kind of NPC that should."""
    buyers = [t[:4] for t in sorted(knowledge.traders(item, "buy"),
                                    key=lambda t: (t[5] != "arnebia", -t[4]))]
    role = knowledge.buyer_role(item)
    if not buyers and role is None:
        raise Stopped(_("I don't know who buys {item}", item=item))
    before = counts(b)
    have = before.get(item.lower(), 0)
    if have <= 0:
        return 0
    n = have if n is None else min(n, have)
    visit(b, role, candidates=buyers or None)
    try:
        d = b.dialog()
        if not d.get("open") or not d.get("top") or b.find_entry(d["top"], item_pattern(item)) is None:
            open_section(b, r"\bsell\b")
            d = b.dialog()
        if not d.get("open") or not d.get("top") or b.find_entry(d["top"], item_pattern(item)) is None:
            b.note_dialog(d, "looking for %s to sell" % item)
            b.log(_("{item} isn't on the buyer's list", item=item), speak=True)
            return 0
        pick(b, item_pattern(item), n)
    finally:
        b.close_dialogs()
    after = counts(b)
    sold = have - after.get(item.lower(), 0)
    if sold > 0:
        InventoryTransaction(before).paid(b, after)     # the money comes in after the items go
    b.log(_("sold {n} {item}", n=sold, item=item))
    stats.moved(b, "sold", item, sold)
    return sold


def breakdown(b: Bot, item: str | None = None, n: int | None = None) -> int:
    """Breakdown up to n equipment (or matching item) at the Blacksmith into Arnebia."""
    item_clean = ""
    base_pure = ""
    if item:
        item_clean = re.sub(r"\s+", " ", item.strip().lower())
        have = counts(b).get(item.lower(), 0) or counts(b).get(item_clean, 0)
        if have <= 0:
            base_pure = re.sub(r"^(?:refined|flawless|epic|legendary|great|heavenly|godbless|super|good|rare|common|sophisticated|normal)\s+", "", item_clean).strip()
            have = counts(b).get(base_pure, 0)
            if have > 0:
                item = base_pure
        if have <= 0:
            b.log(_("no {item} in the bag to break down", item=item), speak=True)
            return 0
    visit(b, "blacksmith")
    pattern = item_pattern(item) if item else None
    done = 0
    npc_name = "blacksmith"
    nx, ny = None, None
    if getattr(b, "last_visited_npc", None):
        _mid, npc_name, nx, ny = b.last_visited_npc

    BREAKDOWN_SECTIONS = (
        r"^\s*(?:Breakdown|Decompose)\s*(?:Equipment)?",
        r"^\s*Breakdown\b",
        r"^\s*Decompose\b",
    )
    try:
        d = b.dialog()
        top = d.get("top") or {}
        if not (pattern and b.find_entry(top, pattern) is not None):
            for sec in BREAKDOWN_SECTIONS:
                if b.find_entry(top, sec) is not None:
                    open_section(b, sec)
                    break

        consecutive_fails = 0
        while not b.stop_event.is_set():
            if n is not None and done >= n:
                break
            d = b.dialog()
            if not d.get("open"):
                # Dialog closed after decomposing equipment; talk to Blacksmith to reopen
                if hasattr(b, "key") and hasattr(b, "wait_dialog"):
                    b.key(b.p.get("enter_key", "enter"))
                    b.wait_dialog(0.3)
                if not b.dialog().get("open"):
                    if nx is not None and ny is not None:
                        b.talk(npc_name, (nx, ny))
                    else:
                        b.talk(npc_name)
                d = b.dialog()
                if not d.get("open"):
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        break
                    continue

            top = d.get("top") or {}
            # If at NPC root menu, open Breakdown Equipment
            for sec in BREAKDOWN_SECTIONS:
                if b.find_entry(top, sec) is not None:
                    open_section(b, sec)
                    d = b.dialog()
                    top = d.get("top") or {}
                    break

            where = b.find_entry(top, pattern) if pattern else None
            if pattern and where is None:
                # No more matching weapons in picker
                break
            if not pattern:
                rows = top.get("rows") or []
                found = False
                for r, row in enumerate(rows):
                    for c, e in enumerate(row):
                        if e and (int(e.get("flags") or 0) & 8):
                            where = (r, c)
                            found = True
                            break
                    if found:
                        break
                if not found:
                    break
            if where is None:
                break

            target_entry = top["rows"][where[0]][where[1]]
            target_pattern = item_pattern(item) if pattern else re.escape(target_entry.get("text") or target_entry.get("raw") or "")
            if not target_pattern:
                break

            chosen = b.choose(target_pattern)
            if chosen is None:
                consecutive_fails += 1
                if consecutive_fails >= 2:
                    break
                continue

            # Handle confirmation popups:
            # 1 popup for normal gear ("Are you sure...").
            # 2 popups for refined/enhanced gear ("Please reconfirm...").
            for _conf in range(3):
                b.sleep(0.06)
                d_conf = b.dialog()
                if not d_conf.get("open") or not d_conf.get("top"):
                    break
                if b.find_entry(d_conf["top"], CONFIRM) is not None:
                    b.choose(CONFIRM)
                else:
                    break

            done += 1
            consecutive_fails = 0

            if item:
                rem = counts(b).get(item.lower(), 0) or (counts(b).get(item_clean, 0) if item_clean else 0)
                if rem <= 0 and base_pure:
                    rem = counts(b).get(base_pure, 0)
                if rem <= 0:
                    break
    finally:
        b.close_dialogs()
    b.log(_("broke down {n} equipment at Blacksmith", n=done), speak=True)
    return done


# ---------------------------------------------------------------------------------------- the collection
# What gathering brings in, and who buys it. The buyers stand side by side in the capitals.
COLLECTION_BUYERS = {"fish": "fish_buyer", "ore": "ore_buyer", "herb": "herb_buyer"}
# A buyer's row for one kind in the bag: "101 Milkfish; Unit price 670 arnebia"
BUYER_ROW = re.compile(r"^\s*(\d+)\s+(.+?)\s*;\s*Unit price\s+(\d+)\s+arnebia", re.I)


def buyer_rows(b: Bot) -> list[tuple[int, str, int]]:
    """[(count, item, price)] of the open buyer's menu."""
    d = b.dialog()
    out: list[tuple[int, str, int]] = []
    top: dict[str, Any] = d.get("top") or {}
    rows: list[list[Any]] = top.get("rows", []) if d.get("open") else []
    for row in rows:
        for e in row:
            for t in (b.entry_texts(e) if e else []):
                m = BUYER_ROW.match(t)
                if m:
                    out.append((int(m.group(1)), m.group(2).strip(), int(m.group(3))))
    return out


def collection_left(p: dict[str, Any], rows: list[tuple[int, str, int]]) -> list[tuple[str, int]]:
    """[(item, how many to sell)] of a buyer's rows: everything it lists, but never what the player
    told the bag manager to keep, and never below the number they keep of an item."""
    out: list[tuple[str, int]] = []
    for count, item, _price in rows:
        r = bagrules.rule(p, item)
        if r["chosen"] and r["action"] == "keep":
            continue
        n = count - (r["keep"] if r["chosen"] else 0)
        if n > 0:
            out.append((item, n))
    return out


def _collection_kinds(b: Bot) -> tuple[str, ...]:
    """Route an automatic sale by profession, or by known goods when no profession is learned."""
    from . import gathering

    skill, _rank = gathering.read_skill(b)
    if skill:
        return (gathering.kind_of(skill),)
    known_buyers = {npc["name"]: npc["role"] for npc in knowledge.data().get("npcs", [])
                    if npc.get("role") in COLLECTION_BUYERS.values()}
    roles: set[str] = set()
    for item, count in counts(b).items():
        if count <= 0 or not item:
            continue
        role = knowledge.buyer_role(item)
        if role and role in COLLECTION_BUYERS.values():
            roles.add(role)
        else:
            roles.update(known_buyers[name] for _, name, *_ in knowledge.traders(item, "buy") if name in known_buyers)
    return tuple(kind for kind, role in COLLECTION_BUYERS.items() if role in roles)


def _sell_to_collection_buyer(b: Bot, role: str, candidates: Sequence[Npc]) -> InventoryTransaction:
    transaction = InventoryTransaction.begin(b)
    attempted: set[str] = set()
    npc = visit(b, role, candidates=candidates)
    if not b.wait_dialog(3.0):
        return transaction
    for _round in range(60):
        rows = buyer_rows(b)
        if not rows and attempted:
            # A sale can close the menu or leave a message. Reopen this buyer where we stand.
            b.close_dialogs()
            if not b.talk(npc) or not b.wait_dialog(3.0):
                break
            rows = buyer_rows(b)
        todo = [(item, n) for item, n in collection_left(b.p, rows) if item.lower() not in attempted]
        if not todo:
            break
        item, n = todo[0]
        if not pick(b, r"^\s*\d+\s+" + re.escape(item) + r"\s*;\s*Unit price", n):
            b.log(_("the {role} didn't take {item}", role=role.replace("_", " "), item=item))
            break
        attempted.add(item.lower())
        transaction.expect(item, -n)
    return transaction


def sell_collection(b: Bot, kinds: tuple[str, ...] | None = None) -> int:
    """Sell the specified collection, or choose its buyer from the character's activity.

    The buyer lists the qualifying bag rows. Apply the bag manager's keep rules and count the
    arnebia received for verified sales.
    """
    earned = 0
    for kind in _collection_kinds(b) if kinds is None else kinds:
        role = COLLECTION_BUYERS[kind]
        candidates = knowledge.service_npcs(role)
        if not candidates:
            b.log(_("I don't know where a {role} is", role=role.replace("_", " ")))
            continue
        try:
            transaction = _sell_to_collection_buyer(b, role, candidates)
        finally:
            b.close_dialogs()
        before = transaction.before.get("arnebia", 0)
        after = transaction.settle(b) if transaction.expected else transaction.before
        moved = transaction.removed(after)
        if moved:
            after = transaction.paid(b, after)      # the money comes in after the items go
        got = after.get("arnebia", 0) - before
        for item, n in moved:
            b.log(_("sold {n} {item}", n=n, item=item))
            stats.moved(b, "sold", item, n)
        if moved:
            earned += max(0, got)
            b.log(_("sold the {kind} for {n} arnebia", kind=_collection_word(kind), n=max(0, got)), speak=True)
    return earned


def _collection_word(kind: str):
    return {"fish": _("fish"), "ore": _("ore"), "herb": _("herbs")}[kind]


# ---------------------------------------------------------------------------------------- restock
Need = tuple[dict[str, Any], int, int]     # (restock rule, in the bag, wanted)


RESTOCK_RETRY = 1800.0   # seconds before a restock that got nothing is tried again (or sooner: more arnebia)


def needs(b: Bot, rules: Sequence[dict[str, Any]] | None, start: bool = False) -> list[Need]:
    """[(rule, have, want)] for every rule that should be topped up now. A rule triggers once the
    item is down to its min ("refill when fewer than": 30 means 30 or fewer); its stock ("fill up
    to") is only how far a trip fills it. With start (an explicit restock, or a trip happening
    anyway) anything below its stock is topped up too - never a reason to leave on its own: 199 of
    200 above a min of 30 is no trip."""
    c = counts(b)
    out: list[Need] = []
    for r in rules or []:
        item = (r.get("item") or "").strip()
        if not item or not r.get("on", True):
            continue
        have = c.get(item.lower(), 0)
        stock = int(r.get("stock") or 0)
        low = int(r.get("min") or 0)
        if (low > 0 and have <= low or start) and have < stock:
            out.append((r, have, stock))
    return out


def _sources(r: dict[str, Any]) -> tuple[bool, bool]:
    """(from the storehouse?, from a shop?) for a rule."""
    source = bagrules.supply_rule(r)
    return source in ("both", "storage"), source in ("both", "buy")


def possible(b: Bot, r: dict[str, Any], why: list[str], *, retry: bool = False) -> bool:
    """Whether a trip could get any of this rule's item: the storehouse isn't known to be out of it,
    or there is arnebia to buy it; and a trip that got none of it lately (RESTOCK_RETRY) isn't
    made again before more arnebia has come in. Why not goes into why."""
    item = r["item"].strip().lower()
    store, shop = _sources(r)
    bag = counts(b)
    money = bag.get(stats.ARNEBIA)          # None: the bag shows no arnebia row - not known to be broke
    failed: dict[str, tuple[float, int]] = getattr(b, "restock_failed", {})
    last = failed.get(item)
    now = clock_for(b).time()
    if not retry and last is not None and now - last[0] < RESTOCK_RETRY and (money or 0) <= last[1]:
        why.append(_("the last trip for {item} got none, and nothing has changed since", item=r["item"]))
        return False
    lacks: set[str] = getattr(b, "storehouse_lacks", set())
    from_store = store and (retry or item not in lacks)
    price = knowledge.unit_price(item) or 1
    # no shop sells it (a grocer has no food or teleport stones): no trip for it, said once
    sold = shop and bool(shops_for(r["item"]))
    from_shop = sold and (money is None or money >= price)
    if from_store or from_shop:
        return True
    if shop and not sold:
        why.append(_("no shop sells {item}", item=r["item"]) if not store
                   else _("the storehouse has no {item}, and no shop sells it", item=r["item"]))
    elif store and not shop:
        why.append(_("the storehouse has no {item}", item=r["item"]))
    elif shop and money is not None and 0 < money < price:
        why.append(_("not enough arnebia to buy {item}: {price} each, {money} in the bag",
                     item=r["item"], price=price, money=money))
    elif shop and money is not None and money <= 0:
        why.append(_("no arnebia to buy {item}", item=r["item"]))
    else:
        why.append(_("the storehouse has no {item} and there is no arnebia to buy it", item=r["item"]))
    return False


def restock_town(b: Bot, todo: Sequence[Need]) -> int | None:
    """The one town (a map) a restock trip goes to: the nearest that has what the trip needs - a
    storehouse for the rules that take from it, a shop of the right kind for those that buy -
    preferring one with all of it. The town we are in first, whatever else is near. None when no
    town has any of it."""
    want_store = any(_sources(r)[0] for r, _h, _w in todo)
    shop_items = [r["item"] for r, _h, _w in todo if _sources(r)[1]]
    towns: dict[int, list[Npc]] = {}
    for npc in (knowledge.service_npcs("storehouse") if want_store else []):
        towns.setdefault(npc[0], []).append(npc)
    for item in shop_items:
        for npc in shops_for(item):
            towns.setdefault(npc[0], []).append(npc)
    if not towns:
        return None

    def covers(mid: int) -> int:
        here = towns[mid]
        store = want_store and any(n in knowledge.service_npcs("storehouse") for n in here)
        shops = sum(1 for item in shop_items if any(n in here for n in shops_for(item)))
        return int(store) + shops

    best = max(covers(m) for m in towns)
    full = [m for m in towns if covers(m) == best]
    here = b.map_id()
    if here in full:
        return here
    near = nearest(b, None, [npc for m in full for npc in towns[m]])
    return near[0] if near else None


def restock_now(b: Bot) -> bool:
    """The explicit Tools, trigger and script job, protected by the bot's restock guard."""
    return b.restock_now(start=True)


def restock(b: Bot, rules: list[dict[str, Any]], start: bool = False) -> bool:
    """Top up every rule that needs it: storehouse first (free), then the shop, all in one town
    (restock_town) - never on to another town. A rule that can't be met (no arnebia, the
    storehouse out of it, a trip that got none) makes no trip, and says why once. True if a trip
    was made."""
    todo = needs(b, rules, start)
    told: set[str] = getattr(b, "supply_told", set())
    b.supply_told = told
    # "just tell me" rules never make a trip: say it once, then leave them
    for r, have, _w in todo:
        if bagrules.supply_rule(r) == "tell" and r["item"] not in told:
            told.add(r["item"])
            b.log(_("low on {item}: {have} left", item=r["item"], have=have), speak=True)
    todo = [t for t in todo if bagrules.supply_rule(t[0]) != "tell"]
    skipped: set[str] = getattr(b, "restock_skipped", set())
    b.restock_skipped = skipped
    able: list[Need] = []
    for t in todo:
        why: list[str] = []
        if possible(b, t[0], why, retry=start):
            able.append(t)
            skipped.discard(t[0]["item"])
        elif start or t[0]["item"] not in skipped:
            skipped.add(t[0]["item"])
            b.log(_("not restocking {item}: {why}", item=t[0]["item"], why=why[0] if why else "?"), speak=True)
    if not able:
        if start:
            message = (_("Restock now: no supplies could be restocked.") if todo
                       else _("Restock now: nothing to restock; no supplies need purchasing or withdrawal."))
            b.log(message, speak=True)
        return False
    # once we're going anyway, top up everything that's short (and can be had)
    todo = [t for t in needs(b, rules, True) if bagrules.supply_rule(t[0]) != "tell" and possible(b, t[0], [], retry=start)]
    where = restock_town(b, todo)
    what = ", ".join("%s %d/%d" % (r["item"], h, w) for r, h, w in todo)
    if where is None:
        # no town found from here by the data: the errands look for their NPCs themselves
        b.log(_("restocking: {what}", what=what), speak=True)
    else:
        b.log(_("restocking in {map}: {what}", map=atlas.label(where), what=what), speak=True)
    b.restock_town = where
    try:
        for r, have, want in todo:
            # a shop or storehouse that doesn't answer skips the item, never the run: the farm
            # loop's own restock (start=False) used to end the whole run on one silent NPC
            if start:
                got = _try_trip(b, _("couldn't restock {item}: {why}"), r["item"],
                                lambda r=r, want=want: _fill(b, r, want - counts(b).get(r["item"].strip().lower(), 0), stock=want)) or 0
            else:
                got = _try_trip(b, _("couldn't restock {item}: {why}"), r["item"],
                                lambda r=r, have=have, want=want: _fill(b, r, want - have)) or 0
            item = r["item"].strip().lower()
            if got <= 0 and counts(b).get(item, 0) <= int(r.get("min") or 0):
                b.__dict__.setdefault("restock_failed", {})[item] = (clock_for(b).time(),
                                                                     counts(b).get(stats.ARNEBIA) or 0)
            elif got > 0:
                getattr(b, "restock_failed", {}).pop(item, None)
    finally:
        b.restock_town = None
    still = [(r["item"], h) for r, h, _w in needs(b, rules, start)
             if not start or bagrules.supply_rule(r) != "tell"]
    if still:
        b.log(_("still short after restocking: {what}", what=", ".join("%s %d" % s for s in still)), speak=True)
    elif start:
        b.log(_("Restock now: supplies are at their configured targets."), speak=True)
    return True


def _restock_quantity(b: Bot, item: str, stock: int) -> int:
    """Recount at the service and cap the top-up by the game's known bag load and learned weight."""
    sheet: dict[str, Any] = b.me().get("sheet") or {}
    if getattr(b, "agent_sheet", False):
        fresh = b.ask_sheet(force=True)
        if isinstance(fresh, dict):
            sheet = fresh
    missing = max(0, stock - counts(b).get(item.strip().lower(), 0))
    maximum, load = sheet.get("maxload"), sheet.get("load")
    if maximum is not None and maximum > 0 and load is not None:
        room = max(0, maximum - load)
        weight = bagrules.weights().get(item.strip().lower())
        if room <= 0 or weight and weight > 0:
            fits = int(room / weight) if weight and weight > 0 else 0
            if fits < missing:
                b.log(_("bag capacity allows {n} more {item}", n=fits, item=item), speak=True)
            missing = min(missing, fits)
    return missing


def _fill(b: Bot, r: dict[str, Any], missing: int, *, stock: int | None = None) -> int:
    """Withdraw, then buy what is still missing, as the rule's source allows. How many came in."""
    if missing <= 0:
        return 0
    store, shop = _sources(r)
    item = r["item"].strip() if stock is not None else r["item"]
    got = 0
    options: dict[str, int] = {"stock": stock} if stock is not None else {}
    if store:
        # Normal and restricted items can occupy separate storage rows. One
        # withdrawal takes only one row; reopen the list before resorting to a shop.
        while missing > 0:
            # a storehouse that doesn't answer leaves the item to the shop
            taken = _try_trip(b, _("couldn't take {item} out of the storehouse: {why}"), item,
                              lambda missing=missing: withdraw(b, item, missing, **options))
            if taken is None:
                break
            got += taken
            remaining = (max(0, stock - counts(b).get(item.lower(), 0)) if stock is not None
                         else max(0, missing - taken))
            progressed = taken > 0 and remaining < missing
            missing = remaining
            if not progressed:
                break
    if stock is not None:
        missing = max(0, stock - counts(b).get(item.lower(), 0))
    if missing > 0 and shop and (shops_for(item) or not store):
        # from the storehouse and the shop, but no shop sells it: what the storehouse had is all
        # (possible() said so); buying alone still says it doesn't know a shop (Stopped)
        got += buy(b, item, missing, **options) or 0
    return got


# ---------------------------------------------------------------------------------------- shards
def shard_catalog() -> tuple[dict[str, dict[str, Any]], dict[str, list[float]]]:
    """{shard name: {"price", "makes"}} and {map: [x, y]} of the Shard Markets (data/shards.json,
    built from the market's own menus)."""
    try:
        with open(os.path.join(paths.DATA, "shards.json"), encoding="utf-8") as f:
            d: dict[str, Any] = json.load(f)
        return d.get("shards", {}), d.get("markets", {})
    except (OSError, ValueError):
        return {}, {}


def market_candidates(markets: dict[str, list[float]]) -> list[Npc]:
    """(map, name, x, y) for each Shard Market, under the name the game uses there now: the survey
    found the "Shard Marketplace" renamed "Shard Broker" in every city (2026-10-11), and talk()
    finds an NPC by its exact name.

    Every city's market, not only those in shards.json: that file holds the cities whose market
    menu was read (its prices), and leaving the others out sent a character in Mario Basin or
    Holy Blood City to sell shards in another city past their own market."""
    out: list[Npc] = []
    npcs: list[dict[str, Any]] = knowledge.data().get("npcs", [])

    def surveyed(n: dict[str, Any]) -> bool:
        return any(str(src).startswith("survey") for src in n.get("sources", []))

    for m, xy in markets.items():
        here = [n for n in npcs if n.get("map") == int(m) and n.get("role") == "shard_market" and surveyed(n)]
        best = min(here, key=lambda n: abs(n["x"] - xy[0]) + abs(n["y"] - xy[1]), default=None)
        out.append((int(m), best["name"] if best else "Shard Broker", xy[0], xy[1]))
    seen = {npc[0] for npc in out}
    for mid, name, x, y in knowledge.service_npcs("shard_market"):
        if mid in seen or atlas.kind(mid) == "gang":
            continue
        seen.add(mid)
        known = next((n for n in npcs if n.get("map") == mid and n.get("name") == name), None)
        out.append((mid, name if known is not None and surveyed(known) else "Shard Broker", x, y))
    return out


TRADE_MOST = 2000      # the Shard Market's "how many" box takes at most this many at a time
MARKET_OPEN = 0.3      # a broker's fixed market list follows their conversation promptly


def market_lists(b: Bot, name: str, wait: float = 0.0) -> bool:
    """The Shard Market's list is open and has the shard: by the verified layout when the agent
    reads it (agent 53), else by the entries' text. `wait` gives the completed previous trade a
    moment to show the market after opening a conversation."""
    if menus.find(b, menus.SHARD_MARKET, name=name, wait=wait) is not None:
        return True
    d = b.dialog()
    return bool(d["open"] and d.get("top") and b.find_entry(d["top"], item_pattern(name)) is not None)


def shard_trade(b: Bot, name: str, action: str, n: int,
                reopen: Callable[[], bool] | None = None) -> int:
    """With the Shard Market's list open: buy or sell n of a shard, in steps the "how many" box
    takes (TRADE_MOST); the list opens again after each. Each step goes by the verified index when
    the agent can (the shard's row, then the buy or sell row of that same shard's id), else by the
    entries' text as before. A step that began by index never goes on by text: a buy/sell menu
    that isn't the one for the shard just chosen is left unanswered, so the wrong shard is never
    bought or sold. Returns how many were asked for in the steps taken."""
    done = 0
    while done < n:
        # A completed sale closes the broker in the real client. Reopen the fixed market before
        # another 2,000-item batch instead of waiting for a list that cannot return by itself.
        if done and reopen is not None and not reopen():
            break
        step = min(n - done, TRADE_MOST)
        # The agent's one-command sale sells only: a buy always goes the menu way below.
        item_id = menus.item_id(b, menus.SHARD_MARKET, name) if action == "sell" else None
        if item_id is not None:
            sold = menus.shard_sell(b, item_id, step)
            if sold == menus.SOLD:
                done += step
                continue
            if sold == menus.UNSURE:
                done += step          # the bag check afterwards says what really went
                break
        # Each choice is direct (the agent verifies the fixed market's item id and row), so do
        # not pause after it.  The next direct choice waits for the game's real next menu instead.
        listed = menus.pick(b, menus.SHARD_MARKET, name=name, settle=False)
        if listed is not None:
            if menus.pick(b, menus.SHARD_TRADE, item_id=int(listed["id"]), action=action, wait=3.0,
                          then_text=False, settle=False) is None:
                b.log(_("the Shard Market's buy and sell menu for {item} didn't come up as expected - "
                        "nothing chosen there, see logs\\dialogs.log", item=name), speak=True)
                b.close_dialogs()
                break
            if not type_number(b, step):
                break
        else:
            if b.choose(item_pattern(name)) is None or not b.wait_dialog(3.0):
                break
            if not pick(b, r"\b%s\b" % action, step):
                break
        done += step
    return done


def _sell_shard_batch(b: Bot, todo: list[tuple[str, int]], open_market: Callable[[str], bool]) -> bool:
    """Hand one verified plan to the agent in a single command. True when it was sent - not proof
    that the server sold it all: the caller reads the bag afterwards and sells only what is left."""
    if not open_market(todo[0][0]):
        return False
    ids = menus.market_item_ids(b, [name for name, _n in todo])
    if ids is None:
        return False
    plan = [(item_id, min(remaining, TRADE_MOST))
            for item_id, (_name, count) in zip(ids, todo)
            for remaining in range(count, 0, -TRADE_MOST)]
    return menus.shard_sell_direct_all(b, plan)


def _left_to_sell(todo: list[tuple[str, int]], before: dict[str, int], now: dict[str, int]) -> list[tuple[str, int]]:
    """What of the plan the bag says is still unsold: never more than was meant to go, so a keep
    rule ("sell N, keep the rest") holds even when part of a batch went through."""
    left: list[tuple[str, int]] = []
    for name, n in todo:
        gone = max(0, before.get(name.lower(), 0) - now.get(name.lower(), 0))
        if n - gone > 0:
            left.append((name, n - gone))
    return left


def _sell_shard_steps(b: Bot, todo: list[tuple[str, int]], open_market: Callable[[str], bool]) -> None:
    """The per-shard path: for an older agent, and for whatever a batch left unsold."""
    for name, n in todo:
        if not open_market(name):
            b.log(_("the Shard Market didn't list {item}", item=name))
            continue
        shard_trade(b, name, "sell", n, reopen=lambda name=name: open_market(name))
    b.close_dialogs()


def sell_shards(b: Bot, names: Iterable[str], at_most: dict[str, int] | None = None) -> int:
    """Sell shards of the given types at the nearest Shard Market (live menu, seen in our logs:
    talk -> a row per shard "<name> | Unit price N arnebia" -> "buy" / "sell" -> "Please enter the
    quantity to sell, up to 2000"). at_most: {lower-cased name: how many} - the bag rule's "sell N,
    keep the rest". Returns how many kinds were sold."""
    _s, markets = shard_catalog()
    transaction = InventoryTransaction.begin(b)
    have = transaction.before
    todo: list[tuple[str, int]] = []
    for n in names:
        in_bag = have.get(n.lower(), 0)
        if in_bag > 0:
            todo.append((n, min(in_bag, (at_most or {}).get(n.lower(), in_bag))))
    if not todo:
        return 0
    if not markets:
        b.log(_("I don't know where a Shard Market is"), speak=True)
        return 0
    candidates = market_candidates(markets)
    broker: str | None = None

    def open_market(name: str) -> bool:
        """Open the broker's fixed market only when it is not already on screen."""
        nonlocal broker
        # With a current agent the pick that follows checks the whole list itself, so the screen's
        # own text is enough here (no extra `menu` read) - but it must list the shard: a popup left
        # on screen is not the market and would stop every sale.
        if menus.can_pick(b):
            d = b.dialog()
            if d.get("open") and d.get("top") and b.find_entry(d["top"], item_pattern(name)) is not None:
                return True
        elif market_lists(b, name):
            return True
        b.close_dialogs()
        if broker is None:
            broker = visit(b, None, candidates=candidates)
        elif not b.talk(broker):
            return False
        return market_lists(b, name, MARKET_OPEN)

    # A whole market plan in one agent command is fastest. The bag then says what really went,
    # and only that which is still unsold goes the step-by-step way.
    left = todo
    if _sell_shard_batch(b, todo, open_market):
        for name, n in todo:
            transaction.expect(name, -n)
        left = _left_to_sell(todo, transaction.before, transaction.settle(b))
        if left:
            b.log(_("the Shard Market didn't take all of it at once - selling the rest one by one"))
    if left:
        _sell_shard_steps(b, left, open_market)
    b.close_dialogs()
    return _note_shard_sales(b, transaction, todo)


def _note_shard_sales(b: Bot, transaction: InventoryTransaction, todo: list[tuple[str, int]]) -> int:
    """What the bag says was sold, logged and counted. Returns how many kinds went."""
    if not transaction.expected:
        for name, n in todo:
            transaction.expect(name, -n)
    after = transaction.settle(b)
    moved = transaction.removed(after)
    if moved:
        transaction.paid(b, after)      # the money comes in after the shards go: count it here
    for name, gone in moved:
        b.log(_("sold {n} {item} at the Shard Market", n=gone, item=name))
        stats.moved(b, "sold", name, gone)
    return len(moved)


def buy_shards_at_market(b: Bot, item: str, n: int) -> int:
    """Buy n shards of item at the nearest Shard Market."""
    _s, markets = shard_catalog()
    if markets:
        candidates = market_candidates(markets)
    else:
        candidates = knowledge.service_npcs("shard_market")
    if not candidates:
        raise Stopped(_("I don't know where a Shard Market is"))
    have = counts(b)
    before = have.get(item.lower(), 0)
    broker = visit(b, None, candidates=candidates)
    done = 0
    try:
        def open_market() -> bool:
            if not market_lists(b, item):
                b.close_dialogs()
                if not b.talk(broker):
                    return False
            return market_lists(b, item, MARKET_OPEN)

        while done < n and not b.stop_event.is_set():
            if not open_market():
                break
            step = min(n - done, TRADE_MOST)
            traded = shard_trade(b, item, "buy", step, reopen=open_market)
            if traded <= 0:
                break
            done += traded
    finally:
        b.close_dialogs()
    after = counts(b)
    got = max(after.get(item.lower(), 0) - before, done)
    b.log(_("bought {n} {item}", n=got, item=item))
    stats.moved(b, "bought", item, got)
    return got


# ---------------------------------------------------------------------------------------- gems
GEM_SHARD = re.compile(r"^\s*Level (\d+) (.+?) Shard\s*$", re.I)
SHARDS_PER_GEM = 4      # the game's own instructions: four shards of a colour and level make a gem


def gem_of(shard: str | None) -> str | None:
    """("Level 1 Obsidian Shard") -> "Level 1 Obsidian"; None for anything else (Enhance Crystal
    Shards make crystals, not gems)."""
    m = GEM_SHARD.match(shard or "")
    return "Level %s %s" % (m.group(1), m.group(2)) if m else None


def menu_text(b: Bot) -> str:
    d = b.dialog()
    top: dict[str, Any] = d.get("top") or {}
    return " | ".join(t for row in top.get("rows", []) for e in row if e for t in b.entry_texts(e))


def _open_shard_synthesis(b: Bot, opener: Callable[[], object] | None = None) -> bool:
    """The Gem Craftsman's shard list, open. Reuses an open one, else opens one: opener(), or the
    nearest Gem Craftsman."""
    for _try in range(2):
        d = b.dialog()
        top = d.get("top") if d["open"] else None
        if top and b.find_entry(top, r"^\s*shard synthesis") is not None:
            b.choose(r"^\s*shard synthesis")
            return bool(b.wait_dialog(3.0))
        if top and b.find_entry(top, r"Shard\s*\d+\s*$") is not None:
            return True                       # already in the shard list
        b.close_dialogs()
        if opener:
            opener()
        else:
            visit(b, "gem_craftsman")
    return False


def _page_naming(b: Bot, pattern: str, seconds: float = 3.0) -> str | None:
    """The open menu's text once it matches pattern, or None when it doesn't within `seconds`."""
    clock = clock_for(b)
    end = clock.time() + seconds
    while True:
        text = menu_text(b)
        if re.search(pattern, text, re.I):
            return text
        if clock.time() >= end:
            return None
        b.sleep(0.15)


def make_gems(b: Bot, wanted: dict[str, int], opener: Callable[[], object] | None = None) -> int:
    """Turn shards into gems at the Gem Craftsman ("shard synthesis"), for the Gem Maker
    (engine/crafting.py). wanted: {shard name: how many of it may be used}. Four shards make one gem
    of their level and this never fails; leftovers (fewer than four) stay in the bag. The menu is read live: the
    shard list ("Level 1 Obsidian Shard42"), then the craftsman's own line "synthesize 1 Level 1
    Obsidian needs 4 Level 1 Obsidian Shard; fee N arnebia" and the "please enter" box, so the
    number of shards and the fee come from the game, not from here. Returns the gems made."""
    have = counts(b)
    spelled = {(r.get("name") or "").strip().lower(): (r.get("name") or "").strip() for r in b.bag_rows()}
    todo: list[tuple[str, str, int]] = []
    for name, n in wanted.items():
        name = spelled.get(name.strip().lower(), name.strip())      # the bag's own spelling
        gem = gem_of(name)
        n = min(n, have.get(name.lower(), 0))
        if gem and n >= SHARDS_PER_GEM:
            todo.append((name, gem, n))
    if not todo:
        return 0
    b.log(_("making gems: {list}", list=", ".join("%d %s" % (n, s) for s, _g, n in todo)), speak=True)
    made = 0
    for shard, gem, n in todo:
        if not _open_shard_synthesis(b, opener):
            b.log(_("couldn't open the Gem Craftsman's shard synthesis"), speak=True)
            break
        row = r"^\s*" + re.escape(shard) + r"\s*\d+\s*$"
        if b.choose(row) is None or not b.wait_dialog(3.0):
            b.log(_("the Gem Craftsman didn't list {item}", item=shard))
            continue
        # some shards first ask which level to make ("synthesize Level 3 ..."): take this shard's own
        top = b.dialog().get("top")
        if top and b.find_entry(top, r"^\s*synthesize Level \d") is not None:
            if b.choose(r"^\s*synthesize\s+" + re.escape(gem) + r"\s*$") is None or not b.wait_dialog(3.0):
                b.log(_("the Gem Craftsman offered no way to make {gem}", gem=gem))
                continue
        text = _page_naming(b, r"\bsynthesize\s+\d+\s+" + re.escape(gem) + r"\s+needs\b")
        if text is None:              # still the page before: its numbers aren't this gem's
            b.log(_("the Gem Craftsman's page for {gem} didn't come up - nothing was spent", gem=gem), speak=True)
            b.close_dialogs()
            continue
        need = re.search(r"needs\s+(\d+)", text, re.I)
        need = int(need.group(1)) if need else SHARDS_PER_GEM
        fee = re.search(r"fee\s+(\d+)\s+arnebia", text, re.I)
        fee = int(fee.group(1)) if fee else 0
        b.p.setdefault("gem_fees", {})[gem.lower()] = fee      # so shards are only bought when the fee is affordable too
        k = n // max(1, need)
        if fee:
            spare = counts(b).get("arnebia", 0) - int(b.p.get("gem_keep_arnebia", 20000))
            k = min(k, max(0, spare) // fee)
        if k <= 0:
            b.log(_("not enough spare arnebia to make {gem} (fee {fee} each)", gem=gem, fee=fee), speak=True)
            b.close_dialogs()
            continue
        before = counts(b).get(gem.lower(), 0)
        if not pick(b, r"please enter|enter quantity|enter the number", k):
            b.log(_("the Gem Craftsman didn't ask how many {gem} to make", gem=gem))
            b.close_dialogs()
            continue
        b.sleep(0.6)
        got = counts(b).get(gem.lower(), 0) - before
        made += max(0, got)
        b.log(_("made {n} {gem} from {used} {shard}", n=got, gem=gem, used=got * need, shard=shard))
    b.close_dialogs()
    return made



def load_now(b: Bot) -> int | None:
    """The bag's load right now (opens and closes the character sheet), or None."""
    sheet: dict[str, Any] = b.refresh_sheet(force=True) or {}
    return sheet.get("load") if sheet.get("maxload", 0) > 0 else None


def unload(b: Bot) -> int:
    """A bag trip by the bag manager's rules (engine/bagrules.py): for each item, keep / store /
    sell / throw away, keeping the number the player set. Shards are sold at the Shard Market,
    other things to an NPC the game has shown buying them; what can't be sold is stored instead.
    Every action is weighed (sheet load before and after) so the bag manager learns item weights.
    Returns how many kinds of item left the bag."""
    scan: dict[str, Any] = b.p.get("scan") or {}
    hotkeys: list[dict[str, Any]] = scan.get("hotkeys", [])
    hot: list[str] = [h["item"] for h in hotkeys if h.get("item")]
    bag: dict[str, int] = {}
    for r in b.bag_rows():
        if r.get("name") and r.get("count"):
            bag[r["name"]] = bag.get(r["name"], 0) + int(r["count"])
    start: dict[str, Any] = getattr(b, "bag_start", None) or {}
    gained = {k.lower(): v - start.get(k.lower(), 0) for k, v in bag.items()}
    todo = bagrules.plan(b.p, bag, hot, gained)
    if not todo:
        b.log(_("the bag is heavy, but no rule says to store, sell or throw anything in it - "
                "open the bag manager to choose"), speak=True)
        return 0
    b.log(_("bag trip: {items}", items=", ".join("%s %d %s" % (a, n, k) for k, a, n in todo)), speak=True)
    done = 0
    _shards, _markets = shard_catalog()
    shard_names = {n.lower() for n in _shards}
    # 1. shards in one visit to the market
    to_market = [(k, n) for k, a, n in todo if a == "sell" and k.lower() in shard_names]
    for name, n in to_market:
        # a Shard Broker that doesn't answer skips the shard, not the run
        if _try_trip(b, _("couldn't sell {item}: {why}"), name,
                     lambda name=name, n=n: _weighed(b, name, lambda: _sell_one_shard(b, name, n))):
            done += 1
    # 2. other sales to NPC buyers; what nobody buys falls back to the storehouse
    store = [(k, n) for k, a, n in todo if a == "store"]
    done += _sell_to_buyers(b, [(k, n) for k, a, n in todo if a == "sell" and k.lower() not in shard_names], store)
    # 3. the storehouse
    for name, n in store:
        if _try_trip(b, _("couldn't store {item}: {why}"), name,
                     lambda name=name, n=n: _weighed(b, name, lambda: deposit(b, name, n))):
            done += 1
    full = [name for name, _n in store if name.lower() in refused(b)]
    if full:
        b.log(_("the storehouse is full - these stay in the bag until there is room in it: {list}",
                list=", ".join(full)), speak=True)
    # 4. throwing away, last, only what the player said
    for name, a, n in todo:
        if a == "throw":
            if _weighed(b, name, lambda name=name, n=n: b.throw_item(name, n)):
                done += 1
    return done


def _weighed(b: Bot, name: str, action: Callable[[], int]) -> int:
    """Do it, and learn the item's weight from the load before and after."""
    before = load_now(b)
    moved = action()
    if moved:
        w = bagrules.learn_weight(name, before, load_now(b), moved)
        if w is not None:
            b.log(_("{item} weighs {w} each", item=name, w=w))
    return moved


def _try_trip(b: Bot, failed: str, name: str, action: Callable[[], int]) -> int | None:
    """action(); a Stopped from it is said (failed: "couldn't ... {item}: {why}") and passed over,
    unless the player pressed Stop."""
    try:
        return action()
    except Stopped as e:
        if b.stop_event.is_set() or e.final:
            raise
        b.log(failed.format(item=name, why=e))
        return None


def _sell_to_buyers(b: Bot, sales: Iterable[tuple[str, int]], store: list[tuple[str, int]]) -> int:
    """Sell to NPC buyers; what nobody buys (or that didn't sell) goes onto store instead."""
    done = 0
    for name, n in sales:
        if bagrules.buyer(name) or knowledge.buyer_role(name):
            if _try_trip(b, _("couldn't sell {item}: {why}"), name,
                         lambda name=name, n=n: _weighed(b, name, lambda: sell(b, name, n))):
                done += 1
                continue
        b.log(_("nobody I know buys {item} - storing it instead", item=name))
        store.append((name, n))
    return done


# ---------------------------------------------------------------------------------------- Tools: by hand
def _bag_and_hotkeys(b: Bot) -> tuple[dict[str, int], list[str]]:
    """{item as the bag spells it: count} and the items on hotkeys, as a bag trip reads them."""
    scan: dict[str, Any] = b.p.get("scan") or {}
    hotkeys: list[dict[str, Any]] = scan.get("hotkeys", [])
    bag: dict[str, int] = {}
    for r in b.bag_rows():
        if r.get("name") and r.get("count"):
            bag[r["name"]] = bag.get(r["name"], 0) + int(r["count"])
    return bag, [h["item"] for h in hotkeys if h.get("item")]


def shards_to_sell(b: Bot) -> tuple[list[tuple[str, int]], list[str]]:
    """What the game menu's Sell shards now would sell: [(shard, how many)] that the bag manager says to sell
    (keeping the number its rule keeps) and the Shard Market buys; and the shards set to sell that
    the market doesn't list (not sold, said)."""
    bag, hot = _bag_and_hotkeys(b)
    market, _m = shard_catalog()
    buys = {n.lower() for n in market}
    sell: list[tuple[str, int]] = []
    other: list[str] = []
    for name, action, n in bagrules.plan(b.p, bag, hot, None):
        if action != "sell" or "shard" not in name.lower():
            continue
        if name.lower() in buys:
            sell.append((name, n))
        else:
            other.append(name)
    return sell, other


def sell_shards_now(b: Bot) -> int:
    """The game menu's Sell shards now: sell the shards the bag manager says to sell at the nearest Shard
    Market - the one in this city when it has one - and nothing else (the bag trip's shard step on
    its own). Says what was sold and what wasn't. Returns how many kinds were sold."""
    todo, other = shards_to_sell(b)
    for name in other:
        b.log(_("not selling {item}: the Shard Market doesn't buy it", item=name))
    if not todo:
        b.log(_("no shards to sell: nothing in the bag is on the bag manager's sell list"), speak=True)
        return 0
    before = counts(b)
    b.log(_("selling shards: {list}", list=", ".join("%d %s" % (n, s) for s, n in todo)))
    sell_shards(b, [s for s, _n in todo], at_most={s.lower(): n for s, n in todo})
    after = counts(b)
    sold = [(s, before.get(s.lower(), 0) - after.get(s.lower(), 0)) for s, _n in todo]
    done = [(s, k) for s, k in sold if k > 0]
    left = [s for s, k in sold if k <= 0]
    if left:
        b.log(_("not sold: {list}", list=", ".join(left)), speak=True)
    if done:
        b.log(_("sold {list}", list=", ".join("%d %s" % (k, s) for s, k in done)), speak=True)
    return len(done)


def throw_list(p: dict[str, Any]) -> list[tuple[str, int]]:
    """The bag manager's throw-away list: [(item, how many it keeps)], only what the player put
    there themselves."""
    rules: dict[str, object] = p.get("bag_rules") or {}
    return sorted((name, int(r.get("keep") or 0)) for name, r in rules.items()
                  if json.is_object(r) and r.get("action") == "throw")


def clean_bag(b: Bot) -> int:
    """The game menu's Clean the bag now: throw away, right where we stand, what the bag manager's
    throw-away list says (keeping the number each rule keeps) - only what is on that list, never
    anything else. Says what went, what couldn't be thrown and what wasn't there. Returns how many
    kinds of item left the bag."""
    listed = {name for name, _keep in throw_list(b.p)}
    if not listed:
        b.log(_("the throw-away list is empty - choose items in Character, Bag and supplies"), speak=True)
        return 0
    bag, hot = _bag_and_hotkeys(b)
    if not bag:
        b.log(_("the bag is empty - nothing to throw away"), speak=True)
        return 0
    todo = [(name, n) for name, action, n in bagrules.plan(b.p, bag, hot, None)
            if action == "throw" and name.lower() in listed and bagrules.rule(b.p, name)["chosen"]]
    if not todo:
        b.log(_("nothing in the bag is on the throw-away list (or only what its rules keep)"), speak=True)
        return 0
    if b.stop_event.is_set():
        raise Stopped(_("stopped"))
    discard = getattr(b, "throw_items", None)
    direct = discard(todo) if discard else None
    if direct is not None:
        thrown = direct
        failed = [name for name, _n in todo if not any(done == name for done, _gone in thrown)]
        for name, gone in thrown:
            stats.moved(b, "thrown", name, gone)
        if failed:
            b.log(_("couldn't throw away: {list}", list=", ".join(failed)), speak=True)
        if thrown:
            b.log(_("threw away {list}", list=", ".join("%d %s" % (k, s) for s, k in thrown)), speak=True)
        return len(thrown)
    thrown: list[tuple[str, int]] = []
    failed: list[str] = []
    for name, n in todo:
        if b.stop_event.is_set():
            raise Stopped(_("stopped"))
        gone = b.throw_item(name, n)
        if gone > 0:
            thrown.append((name, gone))
        else:
            failed.append(name)
    if failed:
        b.log(_("couldn't throw away: {list}", list=", ".join(failed)), speak=True)
    if thrown:
        b.log(_("threw away {list}", list=", ".join("%d %s" % (k, s) for s, k in thrown)), speak=True)
    return len(thrown)


def store_list(p: dict[str, Any]) -> list[tuple[str, int]]:
    """The bag manager's store list ("Move to storehouse"): [(item, how many it keeps in the bag)],
    only what the player put there themselves."""
    rules: dict[str, object] = p.get("bag_rules") or {}
    return sorted((name, int(r.get("keep") or 0)) for name, r in rules.items()
                  if json.is_object(r) and r.get("action") == "store")


def store_items(b: Bot) -> int:
    """The game menu's Store items now: put into the storehouse what the bag manager's store list says
    (keeping the number each rule keeps in the bag) - only what is on that list, read as it is now,
    never anything else. The bag trip's storehouse step on its own (deposit, at the nearest
    storehouse). Says what went in, what the storehouse didn't take (full, or refused) and what
    wasn't in the bag. Returns how many kinds of item were stored."""
    listed = store_list(b.p)
    if not listed:
        b.log(_("the storage list is empty - choose items to move to the storehouse in Character, Bag and supplies"),
              speak=True)
        return 0
    bag, hot = _bag_and_hotkeys(b)
    names = {name for name, _keep in listed}
    todo = [(name, n) for name, action, n in bagrules.plan(b.p, bag, hot, None)
            if action == "store" and name.lower() in names and bagrules.rule(b.p, name)["chosen"]]
    have = {k.lower() for k, v in bag.items() if v > 0}
    missing = sorted(name for name in names if name not in have)
    kept = sorted(name for name in names if name in have and name not in {t.lower() for t, _n in todo})
    if missing:
        b.log(_("not in the bag: {list}", list=", ".join(missing)))
    if kept:
        b.log(_("kept in the bag, as their rules say: {list}", list=", ".join(kept)))
    if not todo:
        b.log(_("nothing in the bag is on the storage list (or only what its rules keep)"), speak=True)
        return 0
    b.log(_("storing: {list}", list=", ".join("%d %s" % (n, s) for s, n in todo)))
    stored: list[tuple[str, int]] = []
    failed: list[str] = []
    for name, n in todo:
        if b.stop_event.is_set():
            raise Stopped(_("stopped"))
        put = _try_trip(b, _("couldn't store {item}: {why}"), name, lambda name=name, n=n: deposit(b, name, n))
        if put:
            stored.append((name, put))
            if put < n:
                failed.append(_("{item} ({n} of {want}: the storehouse took no more)", item=name, n=put, want=n))
        else:
            failed.append(name)
    full = [name for name in failed if name.lower() in refused(b)]
    failed = [name for name in failed if name not in full]
    if full:
        b.log(_("the storehouse is full - these stay in the bag until there is room in it: {list}",
                list=", ".join(full)), speak=True)
    if failed:
        b.log(_("not stored (the storehouse may be full): {list}", list=", ".join(failed)), speak=True)
    if stored:
        b.log(_("stored {list}", list=", ".join("%d %s" % (k, s) for s, k in stored)), speak=True)
    return len(stored)


# ---------------------------------------------------------------------------------------- shard prices
# The Shard Market's prices change; data/shards.json keeps the last ones read, which the bag manager
# and the results use. The game menu's Update shard prices reads the market's list again.
SHARDS_FILE = "shards.json"
PRICE_MOST = 100_000_000          # no shard has cost anywhere near this: a reading above it is wrong
SHOWN_PRICE = re.compile(r"\bunit price\s+(\d{1,9})\s+arnebia", re.I)


class PriceReading:
    """What the market's list gave: valid prices by the name shards.json uses, and what was left out."""

    def __init__(self) -> None:
        self.prices: dict[str, int] = {}
        self.bad: list[str] = []          # rows that were read but not used, with why
        self.unknown: list[str] = []      # shards the list shows that shards.json doesn't have


def read_market_prices(top: dict[str, Any] | None, known: Iterable[str]) -> PriceReading:
    """The prices on the Shard Market's list as the game shows it (logs/dialogs.log): row 0 a header
    with no command, then one row per shard, options [name, "Unit price N arnebia; ..."] and cmd
    [1, item id, N]. A price is used only when the text and the command agree, it is a whole number
    from 1 to PRICE_MOST, the shard is one shards.json has, and no other row gives it another
    price. Anything else is left out with the reason; a menu that isn't the market's list gives
    nothing at all."""
    out = PriceReading()
    rows: list[list[dict[str, Any] | None]] = (top or {}).get("rows") or []
    names = {" ".join(k.split()).casefold(): k for k in known}
    head = rows[0][0] if rows and rows[0] else None
    if head is None or head.get("cmd") is not None or len(rows) < 2:
        out.bad.append(_("the open menu isn't the Shard Market's list"))
        return out
    seen: dict[str, int] = {}
    clash: set[str] = set()
    for r, row in enumerate(rows[1:], start=1):
        e = row[0] if len(row) == 1 else None
        options: list[Any] = (e or {}).get("options") or []
        name = str(options[0]).strip() if options and options[0] else ""
        cmd: list[Any] = (e or {}).get("cmd") or []
        label = name or _("row {n}", n=r)
        if e is None or not name or len(cmd) != 3 or cmd[0] != 1:
            out.bad.append(_("{row}: not a shard row", row=label))
            continue
        shown = [int(m.group(1)) for t in options[1:] if isinstance(t, str) for m in [SHOWN_PRICE.search(t)] if m]
        sent = cmd[2]
        if not shown:
            out.bad.append(_("{row}: no price shown", row=label))
            continue
        if not isinstance(sent, int) or isinstance(sent, bool) or sent != shown[0]:
            out.bad.append(_("{row}: the price shown ({shown}) isn't the one the menu sends ({sent})",
                             row=label, shown=shown[0], sent=sent))
            continue
        if not 1 <= sent <= PRICE_MOST:
            out.bad.append(_("{row}: {price} isn't a price", row=label, price=sent))
            continue
        key = names.get(" ".join(name.split()).casefold())
        if key is None:
            out.unknown.append(name)
            continue
        if key in seen and seen[key] != sent:
            clash.add(key)
        seen[key] = sent
    for key in sorted(clash):
        out.bad.append(_("{row}: listed twice with different prices", row=key))
        seen.pop(key, None)
    out.prices = seen
    return out


def save_shard_prices(prices: Mapping[str, int]) -> list[tuple[str, int, int]]:
    """Write the new prices into data/shards.json, every other field and shard as it was, in the
    file's own layout. Only valid prices of shards it has are written (read_market_prices); one that
    isn't a positive whole number is never written over a good one. Returns [(shard, old, new)] of
    the prices that changed."""
    path = os.path.join(paths.DATA, SHARDS_FILE)
    # The whole read-change-save holds the file's lock: two markets read at once each wrote their
    # prices over the other's.
    with json.lock_for(path):
        return _save_shard_prices(path, prices)


def _save_shard_prices(path: str, prices: Mapping[str, int]) -> list[tuple[str, int, int]]:
    text = json.read_text(path)
    data: object = json.loads(text)
    shards: object = data.get("shards") if json.is_object(data) else None
    if not json.is_object(data) or not json.is_object(shards):
        raise ValueError(_("{file} isn't a shard price list", file=SHARDS_FILE))
    changed: list[tuple[str, int, int]] = []
    for name, price in prices.items():
        row: object = shards.get(name)
        if not json.is_object(row) or isinstance(price, bool) or not 1 <= price <= PRICE_MOST:
            continue
        old = row.get("price")
        if old == price:
            continue
        # only the number changes: the file keeps its own layout, byte for byte
        at = re.compile(r"(%s\s*:\s*\{\s*\"price\"\s*:\s*)-?\d+(?=\s*[,}])" % re.escape(json.dumps(name)))
        text, n = at.subn(lambda m: m.group(1) + str(price), text, count=1)
        if n != 1:
            raise ValueError(_("{file}: the price of {shard} isn't where it should be", file=SHARDS_FILE, shard=name))
        row["price"] = price
        changed.append((name, old if isinstance(old, int) else 0, price))
    if changed:
        if json.loads(text) != data:       # the edit must say exactly what was meant, or nothing is saved
            raise ValueError(_("{file}: the new prices didn't come out right - nothing saved", file=SHARDS_FILE))
        json.save_text(path, text)
    return changed


# When the Shard Market's prices were last read, for every character on this PC (the prices in
# data/shards.json are shared too): {"at": <clock time>}. The Before and After farming lists don't
# go back to the Shard Broker while the prices are fresher than SHARD_PRICES_FRESH; the Tools menu
# and a script's update_shard_prices always do.
SHARD_PRICES_READ = "_shard_prices_read.json"   # in the profiles folder
SHARD_PRICES_FRESH = 8 * 3600.0                 # seconds


def shard_prices_age(b: Bot) -> float | None:
    """Seconds since the shard prices were last read from a Shard Market, None if never (or the
    note can't be read, or it is from the future: a clock set back)."""
    try:
        with open(os.path.join(paths.PROFILES, SHARD_PRICES_READ), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    at = got.get("at") if json.is_object(got) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    age = clock_for(b).time() - at
    return age if age >= 0 else None


def _note_shard_prices_read(b: Bot) -> None:
    try:
        with open(os.path.join(paths.PROFILES, SHARD_PRICES_READ), "w", encoding="utf-8") as f:
            json.dump({"at": clock_for(b).time()}, f)
    except OSError:
        pass


def update_shard_prices(b: Bot) -> int:
    """The game menu's Update shard prices: read the nearest Shard Market's list and keep its prices in
    data/shards.json. Says what changed, what stayed, and every row or shard that couldn't be read
    (those keep their old price). Returns how many prices changed."""
    shards, markets = shard_catalog()
    if not shards:
        b.log(_("the shard price list (data\\shards.json) can't be read"), speak=True)
        return 0
    if not markets:
        b.log(_("I don't know where a Shard Market is"), speak=True)
        return 0
    visit(b, None, candidates=market_candidates(markets))
    try:
        d = b.dialog()
        b.note_dialog(d, "reading the shard prices")
        got = read_market_prices(d.get("top"), shards)
    finally:
        b.close_dialogs()
    for why in got.bad:
        b.log(_("shard price not used: {why}", why=why))
    if got.unknown:
        b.log(_("not in the price list, left out: {list}", list=", ".join(got.unknown)))
    missing = sorted(set(shards) - set(got.prices))
    if missing:
        b.log(_("not read, old price kept: {list}", list=", ".join(missing)))
    if not got.prices:
        b.log(_("no shard prices could be read - nothing changed"), speak=True)
        return 0
    try:
        changed = save_shard_prices(got.prices)
    except (OSError, ValueError) as e:
        b.log(_("couldn't save the shard prices: {err}", err=e), speak=True)
        return 0
    _note_shard_prices_read(b)
    if changed:
        stats.forget_prices()           # the results cost old records at the new prices from now on
    for name, old, new in changed:
        b.log(_("{shard}: {old} -> {new} arnebia", shard=name, old=old, new=new))
    b.log(_("shard prices: {changed} changed, {same} unchanged, {missing} not read",
            changed=len(changed), same=len(got.prices) - len(changed), missing=len(missing)), speak=True)
    return len(changed)


def _sell_one_shard(b: Bot, name: str, n: int) -> int:
    before = counts(b).get(name.lower(), 0)
    sell_shards(b, [name], at_most={name.lower(): n})
    return before - counts(b).get(name.lower(), 0)
