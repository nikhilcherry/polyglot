"""Polyglot MCP server (stdio). Gives any MCP client the house as tools.

Register in Claude Code:   claude mcp add polyglot -- /path/to/polyglot/.venv/bin/python -m polyglot.mcp_server
It talks to polyglotd (python -m polyglot.daemon) at POLYGLOT_URL (default http://127.0.0.1:8770).

Controlled by design: the model passes names, never raw codes; it can draft rules but only a person
can approve them on the Polyglot page; anything physical needs explicit confirmation.
"""
from __future__ import annotations

import os
from typing import Any

import aiohttp
from fastmcp import Context, FastMCP

URL = os.environ.get("POLYGLOT_URL", "http://127.0.0.1:8770")

mcp = FastMCP(
    "polyglot",
    instructions=(
        "Polyglot controls ordinary IR and 433 MHz devices in the user's home (TV, fan, doorbell, a light switch "
        "moved by a servo). Workflow: list_signals -> learn_signal for anything missing (the user presses the real "
        "button twice) -> propose_rule -> tell the user to approve it on the Polyglot page -> rule_status. "
        "Never claim a rule is active until rule_status says approved. Never retry a refused rolling code."
    ),
)
_dynamic: set[str] = set()


async def _call(method: str, path: str, **kw) -> Any:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=70)) as s:
        async with s.request(method, URL + path, **kw) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(data.get("error", str(data)))
            return data


def _fmt_signal(s: dict) -> str:
    c = s["code"]
    return f"{s['name']} ({s['kind']}) · {c['src']} {c['proto']} {c['addr']}/{c['cmd']}"


@mcp.tool
async def list_signals() -> dict:
    """Everything taught in this home, the active rules on the hub, and built-in actions."""
    st = await _call("GET", "/api/state")
    return {
        "signals": [_fmt_signal(s) for s in st["signals"]],
        "builtin_actions": ["light_on (servo, physical)", "light_off (servo, physical)"],
        "rules": [f"#{r['id']}: {r['summary']}" for r in st["rules"]],
        "pending_drafts": [f"{d['id']}: {d['readback']}" for d in st["drafts"] if d["status"] == "pending"],
    }


@mcp.tool
async def learn_signal(name: str, kind: str = "action", ctx: Context | None = None) -> dict:
    """Teach a button. Tell the user to press the real button TWICE now. kind = "trigger" for things
    Polyglot should listen for (doorbell), "action" for buttons it may send (tv_mute, fan_power).
    Rolling codes (car keys, gates) are refused by design."""
    await _call("POST", "/api/learn", json={"name": name, "kind": kind})
    if ctx:
        await ctx.info(f"Press the {name} button twice")
    res = await _call("GET", "/api/learn/wait", params={"timeout": 55})
    if res.get("status") == "saved" and kind == "action" and name not in _dynamic:
        _register_send_tool(name)
    return {"status": res.get("status"), "message": res.get("msg")}


def _register_send_tool(name: str) -> None:
    """A per-device tool appears for clients that honour tools/list_changed; send_signal always works."""

    async def _send() -> str:
        return (await _call("POST", "/api/send", json={"name": name}))["result"]

    _send.__name__ = f"send_{name}"
    _send.__doc__ = f"Send the taught '{name}' button (learned in this home)."
    try:
        mcp.tool(_send, name=f"send_{name}")
        _dynamic.add(name)
    except Exception:
        pass


@mcp.tool
async def send_signal(name: str, confirm_physical: bool = False) -> str:
    """Send one taught button now (e.g. tv_mute). light_on/light_off move a physical switch: only pass
    confirm_physical=true after the user explicitly said yes in this conversation."""
    return (await _call("POST", "/api/send", json={"name": name, "confirm_physical": confirm_physical}))["result"]


@mcp.tool
async def propose_rule(when: str, then: list[str], say: str | None = None) -> dict:
    """Draft an automation that will run ON THE HUB, offline. when = a taught signal name;
    then = taught action names and/or light_on/light_off; say = optional sentence the hub speaker plays.
    Returns a read-back sentence. The rule is NOT active until a person approves it on the Polyglot page."""
    d = await _call("POST", "/api/rules/propose", json={"when": when, "then": then, "say": say, "source": "MCP client"})
    return {"draft_id": d["id"], "readback": d["readback"], "physical": d["physical"],
            "next": "Read this back to the user and ask them to press Approve on the Polyglot page"
                    + (" (it moves a physical switch, so they must also tick the second confirmation)." if d["physical"] else ".")}


@mcp.tool
async def rule_status(draft_id: str) -> dict:
    """Check whether the user approved or rejected a drafted rule."""
    d = await _call("GET", f"/api/drafts/{draft_id}")
    return {"status": d["status"], "rule_id": d.get("rule_id"), "readback": d["readback"]}


@mcp.tool
async def delete_rule(rule_id: int) -> dict:
    """Remove a rule from the hub."""
    return await _call("DELETE", f"/api/rules/{rule_id}")


@mcp.tool
async def recent_activity(limit: int = 15) -> list[str]:
    """What the hub heard, sent and fired recently (the audit log)."""
    st = await _call("GET", "/api/state")
    return [e["text"] for e in st["log"][-limit:]]


@mcp.resource("polyglot://signals")
async def signals_resource() -> str:
    st = await _call("GET", "/api/state")
    return "\n".join(_fmt_signal(s) for s in st["signals"]) or "(nothing taught yet)"


@mcp.resource("polyglot://rules")
async def rules_resource() -> str:
    st = await _call("GET", "/api/state")
    return "\n".join(f"#{r['id']}: {r['summary']}" for r in st["rules"]) or "(no rules)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
