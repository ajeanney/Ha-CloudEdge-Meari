# End-to-end harness (HA + Frigate)

Runs the integration as a real Home Assistant custom component and has a real
Frigate consume its stream, the way README's "Using the stream in Frigate /
go2rtc" describes. It fails if Frigate ever sees the camera offline while the
camera sleeps, wakes, streams live, or goes back to sleep.

> 🛠 **Agents: keep this file in sync with the code.** If you change
> `tests/e2e/` (scenario, pass/fail rules, fakes, images, ports), or the
> coordinator / streamer seams the sim component patches, update this file in
> the same change. See [AGENTS.md](../AGENTS.md).

## Run it

Needs Docker with compose, the project venv (with `aiohttp`), and free host
ports 8123 (HA) and 5000 / 8554 / 8555 / 1984 / 8971 (Frigate). Both
containers use host networking.

```bash
python tests/e2e/run_e2e.py --mode sim               # ~10 min, no camera or credentials
python tests/e2e/run_e2e.py --mode real --cycles 1   # your camera, .env credentials
python tests/e2e/run_e2e.py --mode real --device <part of SN>   # pick one camera
python tests/e2e/run_e2e.py --mode real --ref v0.4.0-dev.1      # integration as of a git ref
```

`--ref` mounts `custom_components/cloudplus` as of that ref (via `git archive`)
while the harness itself stays current, which is how to bisect a regression.
Real mode exercises the camera's real P2P path from this machine: if the camera
is not on this machine's LAN, every session goes through the TURN relay and the
direct-LAN behaviour is not covered.

Useful flags: `--cycles`, `--sleep-s`, `--live-s` (motion timeout for normal
cycles), `--long-live-s` (last cycle, default 150 s, past the ~118 s live
cap), `--sim-wake-delay`, `--sim-session-cap`, `--keep` (leave containers up:
HA on <http://127.0.0.1:8123>, user `e2e` / `e2e-password`; Frigate on
<http://127.0.0.1:5000>; stop them with
`docker compose -f tests/e2e/docker-compose.yml down`). Images default to
HA 2026.9.2 and Frigate 0.18.0; override with `--ha-version` /
`--frigate-version` or the `HA_VERSION` / `FRIGATE_VERSION` env vars.

## What it does

1. Builds a fresh run dir `logs/e2e/<timestamp>/` with HA's config
   (`tests/e2e/ha_config/`) and Frigate's config (`tests/e2e/frigate/`). On
   first use it caches `hass-expose-camera-stream-source` (pinned release) in
   `logs/e2e/cache/`.
2. Starts HA, onboards it over REST, creates a long-lived token, and adds the
   `expose_camera_stream_source` and `cloudplus` entries through their config
   flows.
3. Starts Frigate. Its go2rtc resolves the camera's `stream_source` with
   `echo:curl …/api/camera_stream_source/<camera>` on every open (Frigate
   0.18 needs `GO2RTC_ALLOW_ARBITRARY_EXEC=true` for `echo:` sources). Frigate
   reads go2rtc's RTSP restream with recording on, so its recording-segment
   watchdog runs as well.
4. Runs the scenario, sampling every second: `startup` (until Frigate first
   reports frames), then `sleep`, then per cycle it sets the motion timeout,
   presses the wake button, and records `awakeN` until HA shows the camera
   `streaming` and then `idle` again, followed by `sleepN`. Last, it presses
   wake, records `awakeS` for `--sim-wake-delay` + 40 s, presses the sleep
   button, and records `sleepS`, which must show HA's camera `idle` within
   30 s.

## Pass / fail

After `startup` (Frigate may start before the integration has its first idle
frame, so its early complaints don't count), the run **fails** on any of:

- a pause longer than `--max-gap-s` (default 5 s) between video frames on
  `stream_source`. The harness reads it like any viewer (HA's stream URL), so
  this is ground truth with 1 s resolution;
- a sample where Frigate's `/api/stats` `camera_fps` is 0 or missing (what
  Frigate's UI shows as offline). These stats refresh only every 15 s and count
  the frames Frigate's ffmpeg duplicates up to detect fps, so they lag;
- a change of Frigate's `ffmpeg_pid` (it restarted the input);
- a Frigate watchdog line in its log (`No frames received`,
  `exceeded fps limit`, `Ffmpeg process crashed`, `No new recording segments`,
  `Unable to read frames from ffmpeg`);
- the scenario not finishing (setup error, a phase timeout).

The report prints per-phase seconds, min / avg Frigate fps, zero-fps seconds and the longest video gap. The run
dir keeps `timeline.csv` (one row per second: phase, HA camera state, fps,
ffmpeg pid, seconds since the last video frame), `ha/home-assistant.log` (cloudplus at debug), and `frigate.log`.

## Sim mode

`tests/e2e/sim/cloudplus_e2e_sim/` is an extra HA component, enabled only in sim
mode (`cloudplus_e2e_sim:` in `configuration.yaml`). In `async_setup` it
replaces `build_api_client` (package and config flow) and the coordinator's
`P2PStreamer` with fakes. The harness
adds the cloudplus entry only after HA has started, so the patches are already
in place.

- `FakeApiClient` answers login, the device list (one battery `snap` camera,
  `SIMCAM0001`), wake, and event polls locally. It has no IoT endpoint and no
  MQTT.
- `FakeP2PStreamer` behaves like a dormant camera. Each session waits
  `SIM_WAKE_DELAY_S` (default 20 s) with `awaiting_wake` set, or only 2 s if the
  previous session ended within 10 s, since the camera is still awake. It then
  plays a ffmpeg-generated `testsrc2` clip as 15 fps H.264 plus 440 Hz G.711
  µ-law. After `SIM_SESSION_CAP_S` (default 118 s) it goes silent like the
  ieGeek live-session cap, then ends the session 1.5 s later, as the real
  engine does (`LIVE_SESSION_CAP_RECONNECT_S`). The coordinator then starts a
  new session, which finds the camera still awake.

Sim mode exercises HA, the coordinator, the idle stream, the muxer, the stream
server, go2rtc and Frigate. It does not exercise P2P, the cloud, or real camera
timing; use `--mode real` for those.
