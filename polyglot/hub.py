"""Hub transports with one interface: `await hub.send(op_dict)`; events arrive via `on_event`.

SimHub   - an in-process ESP32 stand-in with a virtual living room (default, no hardware).
SerialHub - the real ESP32 running firmware/polyglot_hub over USB serial (same JSON lines).
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

from .protocol import Code, decode, encode
from .rules import CompiledRule, RuleEngine

EventCB = Callable[[dict[str, Any]], Awaitable[None]]

# Physical things a person can press in the simulated room. Codes are illustrative but use
# real protocol shapes (EV1527 = common fixed-code 433 MHz doorbell chip, NEC = common IR).
BUTTONS: dict[str, dict[str, Any]] = {
    "doorbell": {"label": "Wireless doorbell", "src": "rf433", "proto": "EV1527", "addr": "0x5A3C1", "cmd": "0x8"},
    "tv_mute": {"label": "TV remote · MUTE", "src": "ir", "proto": "NEC", "addr": "0x04", "cmd": "0x09"},
    "tv_power": {"label": "TV remote · POWER", "src": "ir", "proto": "NEC", "addr": "0x04", "cmd": "0x08"},
    "fan_power": {"label": "Fan remote · ON/OFF", "src": "ir", "proto": "NEC", "addr": "0x00", "cmd": "0x45"},
    "fan_speed": {"label": "Fan remote · SPEED", "src": "ir", "proto": "NEC", "addr": "0x00", "cmd": "0x46"},
    "car_key": {"label": "Car key (rolling code)", "src": "rf433", "proto": "KeeLoq", "rolling": True},
}


class SimHub:
    """Behaves like the ESP32: decodes presses, runs rules locally, drives IR/servo/speaker."""

    def __init__(self) -> None:
        self.engine = RuleEngine()
        self.on_event: EventCB | None = None
        self.clips: dict[int, str] = {}
        self.room = {"tv": {"power": True, "muted": False, "volume": 34}, "fan": {"on": False, "speed": 1},
                     "light": {"on": False, "servo": 0}, "speaker": {"last": None}}

    async def start(self) -> None:
        await self._emit({"ev": "hello", "fw": "polyglot-hub-sim 0.1", "rules": 0})
        await self._state()

    async def _emit(self, m: dict[str, Any]) -> None:
        if self.on_event:
            await self.on_event(m)

    async def _state(self) -> None:
        await self._emit({"ev": "state", "room": self.room})

    # ---- a person presses something in the room -> radio/IR burst reaches the hub
    async def press(self, device: str) -> None:
        b = BUTTONS[device]
        if b.get("rolling"):  # every press transmits a new encrypted counter
            addr, cmd = f"0x{random.getrandbits(28):07X}", f"0x{random.getrandbits(32):08X}"
        else:
            addr, cmd = b["addr"], b["cmd"]
        code = Code(b["src"], b["proto"], addr, cmd)
        t0 = time.perf_counter()
        await self._emit({"ev": "rx", **code.to_dict(), "rssi": random.randint(-62, -48), "pulses": _pulses(code)})
        # direct effect of the original remote on its own appliance (IR remotes still work as before)
        self._apply_ir(code)
        for rule in self.engine.match(code):
            done = []
            for act in rule.then:
                done.append(await self._do(act))
            us = int((time.perf_counter() - t0) * 1e6)
            await self._emit({"ev": "fired", "rule": rule.id, "us": us, "actions": done})
        await self._state()

    def _apply_ir(self, code: Code) -> str | None:
        if code.src != "ir":
            return None
        k = (code.addr, code.cmd)
        tv, fan = self.room["tv"], self.room["fan"]
        if k == ("0x04", "0x09"):
            tv["muted"] = not tv["muted"]; return "TV mute toggled"
        if k == ("0x04", "0x08"):
            tv["power"] = not tv["power"]; return "TV power toggled"
        if k == ("0x00", "0x45"):
            fan["on"] = not fan["on"]; return "fan toggled"
        if k == ("0x00", "0x46"):
            fan["speed"] = fan["speed"] % 5 + 1; return "fan speed"
        return "no appliance answered"

    async def _do(self, act: dict[str, Any]) -> str:
        if "tx" in act:
            c = Code(**act["tx"])
            await self._emit({"ev": "tx", **c.to_dict(), "pulses": _pulses(c)})
            return f"IR {c.proto} {c.addr}/{c.cmd} → {self._apply_ir(c)}"
        if "servo" in act:
            ang = int(act["servo"])
            self.room["light"] = {"on": ang >= 45, "servo": ang}
            return f"servo {ang}° → light {'ON' if ang >= 45 else 'OFF'}"
        if "clip" in act:
            text = self.clips.get(int(act["clip"]), "")
            self.room["speaker"]["last"] = text
            await self._emit({"ev": "play", "clip": int(act["clip"]), "text": text})
            return f"speaker: “{text}”"
        return "unknown action"

    # ---- server -> hub
    async def send(self, op: dict[str, Any]) -> None:
        o = op.get("op")
        if o == "rule_put":
            self.engine.put(CompiledRule.from_wire(op))
            await self._emit({"ev": "ack", "op": o, "id": op["id"], "rules": len(self.engine.rules)})
        elif o == "rule_del":
            self.engine.delete(int(op["id"]))
            await self._emit({"ev": "ack", "op": o, "id": op["id"], "rules": len(self.engine.rules)})
        elif o == "clip_put":
            self.clips[int(op["id"])] = op["text"]
        elif o == "tx":
            await self._do({"tx": {k: op[k] for k in ("src", "proto", "addr", "cmd")}})
        elif o == "servo":
            await self._do({"servo": op["angle"]})
        elif o == "play":
            await self._do({"clip": op["clip"]})
        elif o == "press":
            await self.press(op["device"])
            return
        await self._state()


class SerialHub:
    """Real ESP32 over USB serial (115200 baud, JSON lines)."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port, self.baud = port, baud
        self.on_event: EventCB | None = None
        self._w: asyncio.StreamWriter | None = None

    async def start(self) -> None:
        import serial  # pyserial

        ser = serial.Serial(self.port, self.baud, timeout=0)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()

        def readable() -> None:
            data = ser.read(4096)
            if data:
                reader.feed_data(data)

        loop.add_reader(ser.fileno(), readable)
        self._ser = ser
        asyncio.create_task(self._pump(reader))

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            m = decode(line)
            if m and self.on_event:
                await self.on_event(m)

    async def send(self, op: dict[str, Any]) -> None:
        self._ser.write(encode(op))


def _pulses(code: Code) -> list[int]:
    """A plausible microsecond pulse train for the visualiser (deterministic per code)."""
    rnd = random.Random(code.key())
    if code.src == "ir":  # NEC: 9 ms mark, 4.5 ms space, 32 bits of 560 us mark + 560/1690 us space
        out = [9000, 4500]
        for _ in range(32):
            out += [560, 1690 if rnd.random() > 0.5 else 560]
        return out + [560]
    out = []  # EV1527-like OOK: short/long marks
    for _ in range(24):
        out += ([350, 1050] if rnd.random() > 0.5 else [1050, 350])
    return out
