from typing import Any

from unclefarmer.engine import events


def test_events_config_defaults():
    cfg = events.config({})
    assert cfg["event"] == "thanksgiving"
    assert cfg["enabled"] is True
    assert cfg["entries"] == 1
    assert cfg["hunt_mode"] == "exchange"
    assert cfg["skip_big_mobs"] is True
    assert cfg["max_mob_hp"] == 40000
    assert cfg["auto_exchange"] is True
    assert "arnebia" in cfg["exchange"]
    assert cfg["exchange"]["arnebia"]["enabled"] is True
    assert cfg["exchange"]["arnebia"]["cost"] == 4
    # Default enabled items: arnebia (4), gem_shards (4), res_crystals (4), butterflies (4) -> 16 per round * 2 = 32
    assert events.required_turkeys(cfg) == 32


def test_events_config_custom_cost():
    cfg = events.config({
        "events": {
            "entries": 3,
            "hunt_mode": "full",
            "skip_big_mobs": False,
            "max_mob_hp": 50000,
            "exchange": {"arnebia": {"enabled": True, "cost": 6}},
        }
    })
    assert cfg["entries"] == 3
    assert cfg["hunt_mode"] == "full"
    assert cfg["skip_big_mobs"] is False
    assert cfg["max_mob_hp"] == 50000
    assert cfg["exchange"]["arnebia"]["cost"] == 6


def test_events_can_join_hunt():
    d_open_ok = {
        "open": 1,
        "top": {
            "rows": [
                [{"options": ["The Thanksgiving Hunt event is in progress; participated 1/2 times today; click to join."]}]
            ]
        }
    }
    d_open_exhausted = {
        "open": 1,
        "top": {
            "rows": [
                [{"options": ["The Thanksgiving Hunt event is in progress; participated 2/2 times today; click to join."]}]
            ]
        }
    }
    assert events.can_join_hunt(d_open_ok) is True
    assert events.can_join_hunt(d_open_exhausted) is False
    assert events.can_join_hunt({"open": 0}) is False


def test_events_label():
    assert events.label("thanksgiving") == "Thanksgiving"
    assert "Arnebia" in events.item_name("arnebia")
    assert "Resurrection Crystals" in events.item_name("res_crystals")


def test_events_run_check():
    class DummyBot:
        p = {}
        skillbook = None

        def log(self, _msg: str, **_kw: Any):
            pass

        def map_id(self) -> int:
            return 1021

        def refresh_sheet(self) -> dict[str, Any]:
            return {}

        def me(self) -> dict[str, Any]:
            return {"mp": 0, "maxmp": 0}

        def potion(self, _kind: str) -> tuple[str, str | None]:
            return "", None

        def bag_rows(self) -> list[dict[str, Any]]:
            return []

    bot = DummyBot()
    note, ok = events.run(bot, check_only=True)  # type: ignore
    assert ok is True
    assert "Thanksgiving" in note


def test_picking_max_hp_rule():
    from unclefarmer.engine import picking

    snap = {
        "self": {"x": 50, "y": 50, "id": 1},
        "map": 3404,
        "entities": [
            {"id": 10, "name": "Small Turkey", "kind": "monster", "hp": 5000, "maxhp": 5000, "x": 52, "y": 50},
            {"id": 11, "name": "Big Giant Turkey", "kind": "monster", "hp": 60000, "maxhp": 60000, "x": 53, "y": 50},
        ],
    }
    rules = picking.Rules(max_hp=40000)
    notes = picking.Notes(now=100.0)
    choice = picking.choose(snap, rules, notes, walk=lambda e, lim: 2.0, doomed=lambda e, steps: False)
    assert len(choice.targets) == 1
    assert choice.targets[0]["id"] == 10
    assert choice.left.get("hp") == 1


def test_script_runner_events():
    from unclefarmer.engine import script

    called: list[str] = []

    class DummyBot:
        p = {}
        skillbook = None

        def log(self, msg: str, **_kw: Any):
            called.append(msg)

        def check(self):
            pass

        def map_id(self) -> int:
            return 1021

        def refresh_sheet(self) -> dict[str, Any]:
            return {}

        def me(self) -> dict[str, Any]:
            return {"mp": 0, "maxmp": 0}

        def potion(self, _kind: str) -> tuple[str, str | None]:
            return "", None

        def bag_rows(self) -> list[dict[str, Any]]:
            return []

    bot = DummyBot()
    runner = script.Runner(bot, "events check\n")  # type: ignore
    runner.run()
    assert any("Thanksgiving" in msg for msg in called)


def test_find_envoy_in_sight():
    class DummyBot:
        def snap(self) -> dict[str, Any]:
            return {
                "entities": [
                    {"id": 5, "name": "Other NPC", "x": 10.0, "y": 10.0},
                    {"id": 6, "name": "activity envoys", "x": 201.0, "y": 200.0},
                ]
            }

    found = events.find_envoy_in_sight(DummyBot())  # type: ignore
    assert found is not None
    assert found["name"] == "activity envoys"


class _DummyRedeemBot:
    p: dict[str, Any] = {}

    def __init__(self) -> None:
        self.turkeys = 16
        self.dialog_open = False
        self.in_redeem = False
        self.counts = {"Arnebia": 0, "Gem Shards": 0}
        self.logs: list[str] = []

    def log(self, msg: str, **_kw: Any) -> None:
        self.logs.append(msg)

    def bag_rows(self) -> list[dict[str, Any]]:
        return [{"name": "turkey", "count": self.turkeys}] if self.turkeys > 0 else []

    def snap(self) -> dict[str, Any]:
        return {"entities": [{"id": 1, "name": "activity envoy", "x": 75.0, "y": 281.0}]}

    def close_dialogs(self) -> None:
        self.dialog_open = False
        self.in_redeem = False

    def talk(self, _name: str, **_kw: Any) -> bool:
        self.dialog_open = True
        self.in_redeem = False
        return True

    def wait_dialog(self, _sec: float) -> dict[str, Any]:
        return self.dialog()

    def sleep(self, _sec: float) -> None:
        pass

    def dialog(self) -> dict[str, Any]:
        if not self.dialog_open:
            return {"open": 0}
        if not self.in_redeem:
            return {"open": 1, "top": {"rows": [[{"options": ["Redeem Thanksgiving Gifts"]}]]}}
        return {
            "open": 1,
            "top": {
                "rows": [
                    [{"options": [f"Exchange 80,000 Arnebia; today's exchanges: {self.counts['Arnebia']}/2 times"]}],
                    [{"options": [f"Exchange eight sets of Gem Shards; today's exchanges: {self.counts['Gem Shards']}/2 times"]}],
                ]
            },
        }

    def choose(self, pat: str) -> dict[str, Any] | None:
        if "Redeem" in pat:
            self.in_redeem = True
            return {"ok": True}
        if "Arnebia" in pat:
            self.counts["Arnebia"] += 1
            self.turkeys -= 4
            self.dialog_open = False
            return {"ok": True}
        if "Gem Shards" in pat:
            self.counts["Gem Shards"] += 1
            self.turkeys -= 4
            self.dialog_open = False
            return {"ok": True}
        return None


def test_redeem_reopens_closed_dialog():
    cfg = events.config({
        "events": {
            "exchange": {
                "arnebia": {"enabled": True, "cost": 4},
                "gem_shards": {"enabled": True, "cost": 4},
                "res_crystals": {"enabled": False, "cost": 4},
                "butterflies": {"enabled": False, "cost": 4},
            }
        }
    })
    bot = _DummyRedeemBot()
    spent, redeemed = events.redeem_thanksgiving(bot, cfg)  # type: ignore
    assert spent == 16
    assert redeemed == ["arnebia", "arnebia", "gem_shards", "gem_shards"]
    assert bot.turkeys == 0


def test_done_today_skips_hunt_and_redeems(monkeypatch: Any):
    bot = _DummyRedeemBot()
    bot.turkeys = 8

    def _done(_name: Any) -> bool:
        return True

    def _not_in_map(_b: Any) -> bool:
        return False

    monkeypatch.setattr(events, "done_today", _done)
    monkeypatch.setattr(events, "is_in_hunting_map", _not_in_map)

    cfg = events.config({
        "events": {
            "entries": 2,
            "auto_exchange": True,
            "exchange": {
                "arnebia": {"enabled": True, "cost": 4},
                "gem_shards": {"enabled": False, "cost": 4},
                "res_crystals": {"enabled": False, "cost": 4},
                "butterflies": {"enabled": False, "cost": 4},
            },
        }
    })
    note, ok = events.run(bot, cfg)  # type: ignore
    assert ok is True
    assert "2/2" in note
    assert bot.counts["Arnebia"] == 2
    assert bot.turkeys == 0


def test_sold_out_item_is_skipped():
    d_sold_out = {
        "open": 1,
        "top": {
            "rows": [
                [{"options": ["Sold out today; 80,000 Arnebia"]}],
                [{"options": ["Exchange eight sets of Gem Shards; requires 4 turkey; today's exchanges: 1/2 times"]}],
            ]
        },
    }
    assert events._can_redeem(d_sold_out, r"Arnebia") is False  # pyright: ignore[reportPrivateUsage]
    assert events._can_redeem(d_sold_out, r"Gem Shards") is True  # pyright: ignore[reportPrivateUsage]
