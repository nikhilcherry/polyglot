"""polyglotd: owns the hub, the learned signals, rule drafts and the live visualiser.

    python -m polyglot.daemon                 # simulated hub + virtual room (no hardware)
    python -m polyglot.daemon --serial /dev/ttyUSB0   # real ESP32 running firmware/polyglot_hub

Web UI + API on http://127.0.0.1:8770 . The MCP server (polyglot.mcp_server) is a thin client of
this API, so the model never talks to the hub directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import nlrule
from .hub import BUTTONS, SerialHub, SimHub
from .protocol import Code, Signal
from .rules import CompiledRule
from .safety import PHYSICAL_ACTIONS, judge_captures

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BUILTIN = {"light_on": {"servo": 90}, "light_off": {"servo": 0}}
BUILTIN_TEXT = {"light_on": "turn the hall light ON (moves the switch servo)",
                "light_off": "turn the hall light OFF (moves the switch servo)"}


class Polyglot:
    def __init__(self, hub, data_dir: Path) -> None:
        self.hub = hub
        self.hub.on_event = self.on_hub_event
        self.data = data_dir
        self.data.mkdir(parents=True, exist_ok=True)
        self.signals: dict[str, Signal] = {}
        self.rules: dict[int, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.clips: dict[int, str] = {}
        self.log: list[dict[str, Any]] = []
        self.learning: dict[str, Any] | None = None
        self.sockets: set[web.WebSocketResponse] = set()
        self.human_token = secrets.token_urlsafe(16)  # only the served page knows it
        self.room: dict[str, Any] = {}
        self._load()

    # ---------- persistence
    def _path(self) -> Path:
        return self.data / "state.json"

    def _load(self) -> None:
        if self._path().exists():
            d = json.loads(self._path().read_text())
            self.signals = {k: Signal.from_dict(v) for k, v in d.get("signals", {}).items()}
            self.rules = {int(k): v for k, v in d.get("rules", {}).items()}
            self.clips = {int(k): v for k, v in d.get("clips", {}).items()}

    def _save(self) -> None:
        self._path().write_text(json.dumps({
            "signals": {k: s.to_dict() for k, s in self.signals.items()},
            "rules": self.rules, "clips": self.clips}, indent=1))

    async def resync_hub(self) -> None:
        for cid, text in self.clips.items():
            await self.hub.send({"op": "clip_put", "id": cid, "text": text})
        for r in self.rules.values():
            await self.hub.send(r["wire"])

    # ---------- events
    def note(self, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        e = {"t": time.time(), "kind": kind, "text": text, **extra}
        self.log.append(e)
        self.log = self.log[-300:]
        return e

    async def broadcast(self, m: dict[str, Any]) -> None:
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_json(m)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    async def push_state(self) -> None:
        await self.broadcast({"ev": "store", "store": self.snapshot()})

    def snapshot(self) -> dict[str, Any]:
        return {
            "signals": [s.to_dict() for s in self.signals.values()],
            "rules": [{k: v for k, v in r.items() if k != "wire"} for r in self.rules.values()],
            "drafts": list(self.drafts.values()),
            "learning": self.learning,
            "log": self.log[-60:],
            "room": self.room,
            "buttons": {k: v["label"] for k, v in BUTTONS.items()} if isinstance(self.hub, SimHub) else {},
            "sim": isinstance(self.hub, SimHub),
        }

    def name_of(self, code: Code) -> str | None:
        return next((s.name for s in self.signals.values() if s.code == code), None)

    async def on_hub_event(self, m: dict[str, Any]) -> None:
        ev = m.get("ev")
        if ev == "state":
            self.room = m["room"]
        elif ev == "rx":
            code = Code.from_msg(m)
            m["name"] = self.name_of(code)
            self.note("rx", f"heard {m['name'] or 'unknown'} · {code.src} {code.proto} {code.addr}/{code.cmd}")
            if self.learning and self.learning["status"] == "waiting":
                await self._learn_capture(code)
        elif ev == "fired":
            r = self.rules.get(int(m["rule"]))
            self.note("fired", f"rule #{m['rule']} fired on the hub in {m['us']} µs (simulator timing)"
                      if isinstance(self.hub, SimHub) else f"rule #{m['rule']} fired on the hub in {m['us']} µs",
                      rule=m["rule"], actions=m.get("actions"), summary=r and r["summary"])
        elif ev == "play":
            m["audio"] = f"/clips/{m['clip']}.mp3" if (self.data / "clips" / f"{m['clip']}.mp3").exists() else None
        await self.broadcast(m)
        if ev in ("state", "rx", "fired", "ack"):
            await self.push_state()

    # ---------- learning (press twice, refuse rolling codes)
    async def learn_start(self, name: str, kind: str) -> dict[str, Any]:
        name = name.strip().lower().replace(" ", "_")
        if not name.replace("_", "").isalnum():
            raise ValueError("name must be letters, digits and _")
        if name in BUILTIN:
            raise ValueError(f"{name} is a built-in action")
        self.learning = {"id": secrets.token_hex(4), "name": name, "kind": kind, "captures": [],
                         "status": "waiting", "msg": f"Press the {name.replace('_', ' ')} button twice.",
                         "started": time.time()}
        self.note("learn", f"learning “{name}”: press it twice")
        await self.push_state()
        return self.learning

    async def _learn_capture(self, code: Code) -> None:
        L = self.learning
        L["captures"].append(code.to_dict())
        if len(L["captures"]) < 2:
            L["msg"] = "Got it once. Press the same button again."
        else:
            v = judge_captures(Code(**L["captures"][0]), Code(**L["captures"][1]))
            if v.ok:
                existing = self.name_of(v.code)
                if existing and existing != L["name"]:
                    L.update(status="refused", msg=f"that button is already taught as “{existing}”")
                else:
                    self.signals[L["name"]] = Signal(L["name"], L["kind"], v.code, time.time())
                    self._save()
                    L.update(status="saved", msg=f"Saved “{L['name']}” · {v.code.src} {v.code.proto} {v.code.addr}/{v.code.cmd}")
            else:
                L.update(status="refused", msg=v.reason)
            self.note("learn", L["msg"], status=L["status"])
        await self.push_state()

    async def learn_wait(self, timeout: float) -> dict[str, Any]:
        t_end = time.time() + timeout
        while self.learning and self.learning["status"] == "waiting" and time.time() < t_end:
            await asyncio.sleep(0.2)
        if self.learning and self.learning["status"] == "waiting" and time.time() - self.learning["started"] > 120:
            self.learning["status"] = "timeout"
        return self.learning or {}

    # ---------- rules (draft -> human approval -> compiled onto the hub)
    def readback(self, when: str, then: list[str], say: str | None) -> str:
        parts = [BUILTIN_TEXT.get(a, f"send {a.replace('_', ' ')}") for a in then]
        if say:
            parts.append(f"say “{say}”")
        acts = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
        return f"When {when.replace('_', ' ')} is pressed, I'll {acts}. Save this rule?"

    async def propose(self, when: str, then: list[str], say: str | None, source: str) -> dict[str, Any]:
        if when not in self.signals:
            raise ValueError(f"unknown trigger “{when}”. Taught signals: {sorted(self.signals)}")
        for a in then:
            if a not in self.signals and a not in BUILTIN:
                raise ValueError(f"unknown action “{a}”. Only taught signals or {sorted(BUILTIN)} can be sent.")
            if a == when:
                raise ValueError("a rule cannot send its own trigger")
        if not then and not say:
            raise ValueError("a rule needs at least one action")
        d = {"id": secrets.token_hex(3), "when": when, "then": then, "say": say, "source": source,
             "physical": any(a in PHYSICAL_ACTIONS for a in then), "status": "pending",
             "readback": self.readback(when, then, say), "created": time.time()}
        self.drafts[d["id"]] = d
        self.note("draft", f"draft {d['id']} from {source}: {d['readback']}")
        await self.push_state()
        await self.broadcast({"ev": "readback", "draft": d})
        return d

    async def approve(self, draft_id: str, physical_ok: bool) -> dict[str, Any]:
        d = self.drafts[draft_id]
        if d["status"] != "pending":
            return d
        if d["physical"] and not physical_ok:
            raise PermissionError("this rule moves something physical; tick the second confirmation")
        rid = max(self.rules, default=0) + 1
        then_ops: list[dict[str, Any]] = []
        for a in d["then"]:
            then_ops.append(dict(BUILTIN[a]) if a in BUILTIN else {"tx": self.signals[a].code.to_dict()})
        if d["say"]:
            cid = max(self.clips, default=0) + 1
            self.clips[cid] = d["say"]
            await self._render_clip(cid, d["say"])
            await self.hub.send({"op": "clip_put", "id": cid, "text": d["say"]})
            then_ops.append({"clip": cid})
        rule = CompiledRule(rid, self.signals[d["when"]].code, then_ops)
        summary = d["readback"].replace(" Save this rule?", "")
        self.rules[rid] = {"id": rid, "when": d["when"], "then": d["then"], "say": d["say"], "summary": summary,
                           "wire": rule.to_wire(), "approved_at": time.time()}
        await self.hub.send(rule.to_wire())
        d.update(status="approved", rule_id=rid)
        self._save()
        self.note("rule", f"rule #{rid} approved by a human and stored on the hub: {summary}")
        await self.push_state()
        return d

    async def reject(self, draft_id: str) -> dict[str, Any]:
        d = self.drafts[draft_id]
        d["status"] = "rejected"
        self.note("draft", f"draft {draft_id} rejected")
        await self.push_state()
        return d

    async def delete_rule(self, rid: int) -> None:
        self.rules.pop(rid)
        await self.hub.send({"op": "rule_del", "id": rid})
        self._save()
        self.note("rule", f"rule #{rid} deleted")
        await self.push_state()

    async def send_signal(self, name: str, physical_ok: bool) -> str:
        if name in BUILTIN:
            if not physical_ok:
                raise PermissionError("moving the light switch needs confirm_physical=true after the user says yes")
            await self.hub.send({"op": "servo", "angle": BUILTIN[name]["servo"]})
        elif name in self.signals:
            await self.hub.send({"op": "tx", **self.signals[name].code.to_dict()})
        else:
            raise ValueError(f"“{name}” was never taught in this home, so Polyglot will not send it.")
        self.note("tx", f"sent {name} on request")
        await self.push_state()
        return f"sent {name}"

    async def _render_clip(self, cid: int, text: str) -> None:
        """Pre-render the announcement with ElevenLabs if a key is set; else the page uses browser TTS."""
        key = os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            return
        import aiohttp

        voice = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers={"xi-api-key": key}, json={"text": text, "model_id": "eleven_multilingual_v2"},
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        (self.data / "clips").mkdir(exist_ok=True)
                        (self.data / "clips" / f"{cid}.mp3").write_bytes(await r.read())
                        self.note("clip", f"clip {cid} rendered with ElevenLabs")
        except Exception as e:  # never block a rule on TTS
            self.note("clip", f"ElevenLabs TTS failed ({e}); using browser voice")


# ---------------------------------------------------------------- HTTP
def make_app(pg: Polyglot) -> web.Application:
    app = web.Application()
    r = app.router

    def ok(x: Any) -> web.Response:
        return web.json_response(x)

    def err(e: Exception, code: int = 400) -> web.Response:
        return web.json_response({"error": str(e)}, status=code)

    async def index(_):
        html = (WEB / "index.html").read_text().replace("__HUMAN_TOKEN__", pg.human_token)
        return web.Response(text=html, content_type="text/html")

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        pg.sockets.add(ws)
        await ws.send_json({"ev": "store", "store": pg.snapshot()})
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
        pg.sockets.discard(ws)
        return ws

    async def state(_):
        return ok(pg.snapshot())

    async def learn(request):
        b = await request.json()
        try:
            return ok(await pg.learn_start(b["name"], b.get("kind", "action")))
        except ValueError as e:
            return err(e)

    async def learn_wait(request):
        return ok(await pg.learn_wait(float(request.query.get("timeout", 25))))

    async def send(request):
        b = await request.json()
        try:
            return ok({"result": await pg.send_signal(b["name"], bool(b.get("confirm_physical")))})
        except PermissionError as e:
            return err(e, 403)
        except ValueError as e:
            return err(e)

    async def propose(request):
        b = await request.json()
        try:
            return ok(await pg.propose(b["when"], list(b.get("then", [])), b.get("say"), b.get("source", "api")))
        except ValueError as e:
            return err(e)

    async def say(request):
        b = await request.json()
        try:
            p = nlrule.parse(b["text"], list(pg.signals.values()))
        except ValueError as e:
            return err(e)
        return ok(await pg.propose(p["when"], p["then"], p["say"], "voice/typed sentence"))

    async def draft_get(request):
        d = pg.drafts.get(request.match_info["id"])
        return ok(d) if d else err(KeyError("no such draft"), 404)

    async def approve(request):
        if request.headers.get("X-Polyglot-Human") != pg.human_token:
            return err(PermissionError("approval must come from a person on the Polyglot page"), 403)
        b = await request.json()
        try:
            return ok(await pg.approve(request.match_info["id"], bool(b.get("physical_ok"))))
        except PermissionError as e:
            return err(e, 403)

    async def reject(request):
        return ok(await pg.reject(request.match_info["id"]))

    async def rule_delete(request):
        try:
            await pg.delete_rule(int(request.match_info["id"]))
            return ok({"deleted": int(request.match_info["id"])})
        except KeyError:
            return err(KeyError("no such rule"), 404)

    async def press(request):
        if not isinstance(pg.hub, SimHub):
            return err(ValueError("only the simulator has virtual buttons"))
        b = await request.json()
        await pg.hub.press(b["device"])
        return ok({"pressed": b["device"]})

    async def clip(request):
        p = pg.data / "clips" / request.match_info["name"]
        return web.FileResponse(p) if p.exists() else web.Response(status=404)

    r.add_get("/", index)
    r.add_get("/ws", ws_handler)
    r.add_get("/api/state", state)
    r.add_post("/api/learn", learn)
    r.add_get("/api/learn/wait", learn_wait)
    r.add_post("/api/send", send)
    r.add_post("/api/rules/propose", propose)
    r.add_post("/api/say", say)
    r.add_get("/api/drafts/{id}", draft_get)
    r.add_post("/api/drafts/{id}/approve", approve)
    r.add_post("/api/drafts/{id}/reject", reject)
    r.add_delete("/api/rules/{id}", rule_delete)
    r.add_post("/api/sim/press", press)
    r.add_get("/clips/{name}", clip)
    r.add_static("/static", WEB)
    return app


async def amain(args) -> None:
    hub = SerialHub(args.serial) if args.serial else SimHub()
    pg = Polyglot(hub, Path(args.data))
    app = make_app(pg)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    await hub.start()
    await pg.resync_hub()
    print(f"polyglotd: {'ESP32 on ' + args.serial if args.serial else 'simulated hub'} · http://{args.host}:{args.port}")
    await asyncio.Event().wait()


def main() -> None:
    ap = argparse.ArgumentParser(description="Polyglot daemon")
    ap.add_argument("--serial", help="ESP32 serial port (omit for the simulator)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data", default=str(ROOT / "data"))
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
