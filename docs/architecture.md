# Architecture

How the code is laid out and which file owns what.

> 🛠 **Agents: keep this file in sync with the code.** If you move, rename,
> add or remove a module, or change which file owns a layer, update the
> tables below in the same change. See [AGENTS.md](../AGENTS.md) for the
> full doc-maintenance policy.

## Top level

```
.
├── custom_components/cloudplus/   # The HA integration (HACS-shipped code)
├── debug_tools/                   # Reusable bits of the CLI harness
├── debug.py                       # CLI entry point — `python debug.py …`
├── docs/                          # Protocol + dev docs (this folder)
├── README.md                      # User-facing
├── AGENTS.md                      # AI-assistant onboarding
└── hacs.json / manifest.json      # HACS + HA metadata
```

Some contributors keep local-only evidence and tooling (APK extractions,
packet captures, a sandbox for the official app) outside the repo as ground
truth when the code disagrees with the official app. It's gitignored and
per-contributor — see [`AGENTS.local.md`](../AGENTS.local.md) at the repo
root if present.

## Integration entry points

`custom_components/cloudplus/`

| File | Responsibility |
|------|----------------|
| `__init__.py` | `async_setup_entry` / `async_unload_entry`, account-vs-camera entry split, V1→V2 migration, PTZ service registration. |
| `config_flow.py` | User-facing config + options flows. One account entry, N child camera entries created via `SOURCE_IMPORT`. |
| `const.py` | All config keys, defaults, app-profile list, alarm-type table, IoT codes. |
| `api.py` | Meari HTTP client — login, device list, IoT model fetch, wake, OpenAPI bridge. |
| `manifest.json` | Domain, version, `requirements`, `iot_class`. |
| `services.yaml` + `strings.json` + `translations/` | Service schemas + UI strings. |

### Entity platforms

Each platform file declares a fixed set of "core" entities plus a dynamic set
derived from the camera's IoT model:

| File | Core | IoT-driven |
|------|------|------------|
| `camera.py` | Live + idle MPEG-TS stream. | — |
| `binary_sensor.py` | Motion / Awake / Charging. | — |
| `button.py` | Wake Camera. | — |
| `sensor.py` | Battery + Charge Status. | Temperature, Humidity, WiFi Signal. |
| `number.py` | Motion Timeout. | Sensitivities, intervals, brightness, volume… |
| `select.py` | Stream Host Mode, Stream Quality. | Day/Night, SD record, anti-flicker… |
| `switch.py` | Wake on Motion. | LED, PIR, ONVIF, HomeKit, sirens… |

IoT entities are gated on `coordinator.supports_iot(feature)` or
`coordinator.has_iot_code(code)`. `coordinator.iot_capability(name)` preserves
the distinction between an explicit zero and an unknown flag (missing,
invalid, or the SDK's `-1` default).

- Temperature (`1008`) is converted from milli-degrees Celsius; humidity
  (`1009`) is already whole percent. Native `255` readings are unavailable.
  Explicit `tmpr=0` / `hmd=0` flags suppress stale sensor channels; unknown
  flags retain the IoT-code fallback for legacy cameras.
- Motion sensitivity (`107`) writes IoT values `0/1/2`. The SDK's P2P
  `6/4/2` encoding is a separate transport representation.
- Alarm interval (`178`) options follow the `afq` bitmask for capability
  version `22+`. Older or unadvertised capabilities retain the original
  three intervals; known modern `afq=0` exposes no interval entity.
- Day/night (`113`) and full-color (`209`) options follow `dnm` and the
  ordered `dnm2` profile overrides. Only the active command is exposed when
  a profile is advertised. Schedule and intelligent-color choices require
  their modifier bits; missing profiles retain the base option maps.

Each select uses the same per-camera option map for display and writes;
unsupported choices and out-of-range number writes are rejected locally.

## Coordinator

`custom_components/cloudplus/coordinator/` — the per-camera worker. One
`CloudEdgeMeariCoordinator` is created per camera entry in `async_setup_entry`.

| File | What it does |
|------|--------------|
| `__init__.py` | Lifecycle, IoT cache, wake retry loop, video pipeline glue. |
| `state.py` | Awake / battery / charge state machine, event fan-out. |
| `motion.py` | Translates raw MQTT alarms into HA binary-sensor pulses. |
| `iot.py` | Capability parsing and feature checks, IoT value normalization/lookups. |
| `mpegts.py` + `muxer.py` | ffmpeg-based MPEG-TS muxer (video copy, audio encode). |
| `audio_encoder.py` | G.711 µ-law → AAC. |
| `stream_server.py` + `stream_bootstrap.py` | TCP fan-out of MPEG-TS, PAT/PMT seed, idle-stream loop. |

## P2P streamer

`custom_components/cloudplus/p2p_streamer/` — the protocol stack itself.
Pure-asyncio; can be driven from HA or from `debug.py` without changes.

| File | Layer |
|------|-------|
| `engine.py` | `P2PStreamer` — lifecycle and transport selection (`deviceP2P=ppcs` or modern WebRTC-like signaling). |
| `live_session.py` | `LiveSessionMixin._stream_with_turn` — the per-session ICE → KCP → VVP → media loop (split out to keep files <1000 lines). |
| `ppcs.py` | Legacy PPStrong root rendezvous, direct UDP punching, reliable channels and media reassembly. |
| `session_support.py` | Shared session constants, identity helpers, `SignalingClusterMiss`. |
| `root_discovery.py` | Native UDP root protocol on port 9253. |
| `network.py` | Socket plumbing, packet routing, NAT timers. |
| `ice.py` + `sdp.py` | Candidate gathering + SDP parsing (relay implicit in `m=audio`). |
| `relay_probe.py` | TURN allocation, permissions, channel binding. |
| `lan.py` | Direct-LAN punch (plaintext msgsvr "connect" to host candidates). |
| `kcp_tunnel.py` (sibling under `cloudplus/`) | KCP reliable transport over UDP. |
| `protocol.py` | IVA framing (`0x7010` / `0x7012`). |
| `codec.py` | VVP packet codec (magic `0x56565099`). |
| `quality.py` | Quality-profile → stream-id mapping (modern AUTO/profile ids and raw legacy ids). |
| `keepalive.py` | `0x888E` heartbeat + proactive `START_LIVE` re-issue. |

## Sibling protocol modules

Some lower-level codec / signaling bits live next to the HA glue rather than
inside `p2p_streamer/`, because they're also used by the API client:

- `meari_signaling.py` — MsgSvr (TCP) signaling, candidate exchange.
- `meari_commands.py` — IoT command codes / device-event types.
- `kcp_tunnel.py` — KCP implementation (segments, ACK batching, ARQ).
- `msgsvr_codec.py` — Plaintext msgsvr frame encoder used by the LAN punch.
- `motion_event.py` — Alarm-type classification.
- `turn_client.py` — Long-lived TURN allocation, refresh, ChannelData.

## Debug harness

`debug_tools/` — used by `debug.py` to drive the same coordinator code from
the command line. `auth.py` loads `.env`, `list_cmd.py` prints cameras,
`stream_cmd.py` runs a full session and pipes the muxer output into ffplay
plus optional analysis (TS / PCM / visual reports under `visual.py`,
`ts_analysis.py`, `correlation.py`).

This is the canonical way to repro a bug — anything you see in HA should also
be reproducible with `python debug.py stream …`.
