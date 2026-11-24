import asyncio

import pytest

from polyglot import nlrule
from polyglot.daemon import Polyglot
from polyglot.hub import SimHub
from polyglot.protocol import Code, Signal, decode, encode
from polyglot.rules import CompiledRule, RuleEngine
from polyglot.safety import judge_captures

BELL = Code("rf433", "EV1527", "0x5A3C1", "0x8")
MUTE = Code("ir", "NEC", "0x04", "0x09")


def run(coro):
    return asyncio.run(coro)


def test_wire_roundtrip():
    m = {"op": "tx", **MUTE.to_dict()}
    assert decode(encode(m)) == m
    assert decode(b"boot: debug text") is None


def test_fixed_code_accepted_rolling_refused():
    assert judge_captures(BELL, BELL).ok
    v = judge_captures(Code("rf433", "KeeLoq", "0x1111111", "0xAAAA0001"), Code("rf433", "KeeLoq", "0x2222222", "0xBBBB0002"))
    assert not v.ok and "rolling" in v.reason.lower()
    assert not judge_captures(MUTE, Code("ir", "NEC", "0x04", "0x08")).ok  # two different buttons


def test_engine_matches_and_debounces():
    e = RuleEngine()
    e.put(CompiledRule(1, BELL, [{"tx": MUTE.to_dict()}]))
    assert [r.id for r in e.match(BELL, now=100.0)] == [1]
    assert e.match(BELL, now=100.3) == []  # same press repeating on air
    assert [r.id for r in e.match(BELL, now=102.0)] == [1]
    assert e.match(MUTE, now=103.0) == []


def test_nl_parser_uses_only_taught_names():
    sigs = [Signal("doorbell", "trigger", BELL, 0), Signal("tv_mute", "action", MUTE, 0)]
    p = nlrule.parse("When the doorbell rings, mute the TV, switch on the hall light and tell Amma someone is at the door", sigs)
    assert p == {"when": "doorbell", "then": ["tv_mute", "light_on"], "say": "Someone is at the door"}
    with pytest.raises(ValueError):
        nlrule.parse("When the doorbell rings, unlock the front door", sigs)


async def _learn(pg, hub, name, kind, device, presses=2):
    await pg.learn_start(name, kind)
    for _ in range(presses):
        await hub.press(device)
        await asyncio.sleep(1.05)  # past the hub's debounce
    return pg.learning


def test_end_to_end_on_simulated_hub(tmp_path):
    async def go():
        hub = SimHub()
        pg = Polyglot(hub, tmp_path)
        await hub.start()
        assert (await _learn(pg, hub, "doorbell", "trigger", "doorbell"))["status"] == "saved"
        assert (await _learn(pg, hub, "tv_mute", "action", "tv_mute"))["status"] == "saved"
        hub.room["tv"]["muted"] = False
        # car keys are refused
        assert (await _learn(pg, hub, "car", "trigger", "car_key"))["status"] == "refused"
        assert "car" not in pg.signals
        # the model can only send taught names
        with pytest.raises(ValueError):
            await pg.send_signal("garage_door", physical_ok=False)
        with pytest.raises(PermissionError):
            await pg.send_signal("light_on", physical_ok=False)
        # draft -> needs physical confirmation -> stored on hub
        d = await pg.propose("doorbell", ["tv_mute", "light_on"], "Someone is at the door", "test")
        assert d["physical"] and not hub.engine.rules
        with pytest.raises(PermissionError):
            await pg.approve(d["id"], physical_ok=False)
        await pg.approve(d["id"], physical_ok=True)
        assert len(hub.engine.rules) == 1
        # now the hub runs it with nothing else involved
        await asyncio.sleep(1.05)
        await hub.press("doorbell")
        assert hub.room["tv"]["muted"] is True
        assert hub.room["light"]["on"] is True
        assert hub.room["speaker"]["last"] == "Someone is at the door"
        assert any(e["kind"] == "fired" for e in pg.log)
    run(go())


def test_approval_endpoint_requires_the_page_token(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from polyglot.daemon import make_app

    async def go():
        hub = SimHub()
        pg = Polyglot(hub, tmp_path)
        pg.signals["doorbell"] = Signal("doorbell", "trigger", BELL, 0)
        pg.signals["tv_mute"] = Signal("tv_mute", "action", MUTE, 0)
        async with TestClient(TestServer(make_app(pg))) as c:
            r = await c.post("/api/rules/propose", json={"when": "doorbell", "then": ["tv_mute"]})
            did = (await r.json())["id"]
            r = await c.post(f"/api/drafts/{did}/approve", json={})  # what a model/client without the page could do
            assert r.status == 403
            r = await c.post(f"/api/drafts/{did}/approve", json={}, headers={"X-Polyglot-Human": pg.human_token})
            assert r.status == 200 and (await r.json())["status"] == "approved"
    run(go())
