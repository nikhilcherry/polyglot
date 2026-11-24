"""Hub <-> server wire format: one JSON object per line (USB serial or TCP for the simulator).

hub -> server
  {"ev":"rx","src":"rf433","proto":"EV1527","addr":"0x5A3C1","cmd":"0x8","rssi":-54}
  {"ev":"fired","rule":3,"us":812,"actions":[...]}
  {"ev":"state", ...}                       (simulator only: virtual appliance state)
  {"ev":"hello","fw":"polyglot-hub 0.1","rules":2}

server -> hub
  {"op":"tx","src":"ir","proto":"NEC","addr":"0x04","cmd":"0x09"}
  {"op":"servo","angle":90}
  {"op":"play","clip":2}
  {"op":"rule_put","id":3,"when":{...code...},"then":[{"tx":{...}},{"servo":90},{"clip":2}]}
  {"op":"rule_del","id":3}
  {"op":"press","device":"doorbell"}         (simulator only: a human pressing a physical button)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SOURCES = ("rf433", "ir")


@dataclass(frozen=True)
class Code:
    """A decoded signal. Two presses of a fixed-code button give the same Code."""

    src: str
    proto: str
    addr: str
    cmd: str

    @classmethod
    def from_msg(cls, m: dict[str, Any]) -> "Code":
        return cls(m["src"], m["proto"], str(m["addr"]), str(m["cmd"]))

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def key(self) -> str:
        return f"{self.src}:{self.proto}:{self.addr}:{self.cmd}"


@dataclass
class Signal:
    """Something the owner taught Polyglot by pressing it."""

    name: str
    kind: str  # "trigger" (we listen for it) or "action" (we may send it)
    code: Code
    taught_at: float
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["code"] = self.code.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        return cls(d["name"], d["kind"], Code(**d["code"]), d["taught_at"], d.get("aliases", []))


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


def decode(line: bytes | str) -> dict[str, Any] | None:
    line = line.decode(errors="replace") if isinstance(line, bytes) else line
    line = line.strip()
    if not line.startswith("{"):
        return None  # firmware debug prints are allowed on the same port
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
