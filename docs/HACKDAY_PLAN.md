# Hack-day plan (1–2 Dec 2026, 24 h)

**The moment:** a judge presses a ₹250 doorbell with the laptop shut. The TV mutes, the hall-light servo flips, and a voice says someone is at the door.

## Lanes
| Lane | Scope |
|---|---|
| 01 Firmware | CC1101 + IR decode, on-hub rule engine (NVS), IR transmit, servo, I2S clips |
| 02 MCP server | FastMCP tools, safety checks (rolling-code diff, confirmations, log), rule compiler |
| 03 Visualiser | WebSocket page: live pulse timing, decoded address vs command, rules firing |
| 04 Voice + rig | ElevenLabs STT/TTS voice app (Claude API + MCP), doorbell, TV/IR bulb, switch rig |

## Order
- **0–1.5 h:** risk test: 10/10 doorbell presses decode identically at 3 m; IR dump of every demo remote.
- **1.5–3 h:** freeze the serial JSON contract and folder owners.
- **3–12 h:** one rule end to end on the hub, laptop shut.
- **12–18 h:** English rule authoring through MCP; read-back + confirm.
- **18–22 h:** ElevenLabs voice in/out, AWS codebook + alert.
- **22–24 h:** record a fallback of the full demo, rehearse.

## Cut list (first to go → last)
per-device runtime tools (keep `send_signal`) → AWS → speech input (keep TTS read-back) → LoRa node → servo → visualiser hex diff.
**Never cut:** doorbell → TV mute with the laptop closed.

## Targets to measure (not results)
- trigger → action latency on the hub: < 100 ms
- decode reliability: 10/10 presses at 3 m per demo device
- time to teach a device and create a rule: < 2 min
