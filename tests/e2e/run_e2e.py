"""End-to-end check: cloudplus inside a real HA, consumed by a real Frigate.

Runs the scenario sleep -> wake -> live -> sleep (repeated), then ends one live
session early with the Sleep button, and fails if
Frigate ever sees the camera offline (``camera_fps == 0``), restarts its
ffmpeg, or logs one of its stream watchdog lines. See docs/e2e.md.

    python tests/e2e/run_e2e.py --mode sim
    python tests/e2e/run_e2e.py --mode real --cycles 1
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "tests" / "e2e"
CACHE = REPO / "logs" / "e2e" / "cache"
HA_URL = "http://127.0.0.1:8123"
FRIGATE_URL = "http://127.0.0.1:5000"
CLIENT_ID = f"{HA_URL}/"
FRIGATE_CAMERA = "cloudplus"
EXPOSE_VERSION = "0.4.0"
EXPOSE_URL = (
    "https://codeload.github.com/felipecrs/hass-expose-camera-stream-source"
    f"/tar.gz/refs/tags/v{EXPOSE_VERSION}"
)
# Frigate's own watchdog lines (frigate/video/ffmpeg.py): each one marks the
# camera offline and restarts its ffmpeg.
FRIGATE_WATCHDOG = (
    "No frames received",
    "exceeded fps limit",
    "Ffmpeg process crashed",
    "No new recording segments",
    "Unable to read frames from ffmpeg",
)
TS_PACKET = 188


class VideoProbe:
    """Watch ``stream_source`` like any viewer; note when each video PES arrives.

    Frigate's ``camera_fps`` only refreshes every stats interval and counts
    frames its ffmpeg duplicates, so it can't time a gap. This can.
    """

    def __init__(self) -> None:
        self.last_video = 0.0
        self.reconnects = -1

    def gap(self) -> float | None:
        return round(time.monotonic() - self.last_video, 1) if self.last_video else None

    def _scan(self, buf: bytes) -> bytes:
        while buf and buf[0] != 0x47:  # resync after a partial packet
            buf = buf[1:]
        end = len(buf) - len(buf) % TS_PACKET
        for i in range(0, end, TS_PACKET):
            pkt = buf[i:i + TS_PACKET]
            if not pkt[1] & 0x40:  # no payload_unit_start: not a new PES
                continue
            off = 4 + (pkt[4] + 1 if pkt[3] & 0x20 else 0)
            if pkt[off:off + 3] == b"\0\0\1" and 0xE0 <= pkt[off + 3] <= 0xEF:
                self.last_video = time.monotonic()
        return buf[end:]

    async def run(self, ha: Ha, session: aiohttp.ClientSession, camera: str) -> None:
        reading = aiohttp.ClientTimeout(total=None, sock_read=30)
        while True:
            self.reconnects += 1
            try:
                url = await ha.stream_source(camera)
                if url:
                    async with session.get(url, timeout=reading) as resp:
                        buf = b""
                        async for chunk in resp.content.iter_any():
                            buf = self._scan(buf + chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)


@dataclass
class Sample:
    """One 1 s observation of Frigate and HA."""

    t: float
    phase: str
    ha_state: str
    fps: float | None
    ffmpeg_pid: int | None
    video_gap_s: float | None


@dataclass
class Run:  # pylint: disable=too-many-instance-attributes
    """One harness run: its arguments, compose env, entities and samples."""

    args: argparse.Namespace
    run_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    token: str = ""
    camera: str = ""
    wake_button: str = ""
    sleep_button: str = ""
    motion_timeout: str = ""
    samples: list[Sample] = field(default_factory=list)
    started: float = 0.0
    # When startup ended, as docker's --since takes it; checks begin here.
    checked_since: str = ""
    probe: VideoProbe = field(default_factory=VideoProbe)

    def compose(self, *cmd: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-f", str(E2E / "docker-compose.yml"), *cmd],
            env={**os.environ, **self.env}, check=check, **kw,
        )


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_env_file() -> dict[str, str]:
    values = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def prepare(args: argparse.Namespace) -> Run:
    run_dir = REPO / "logs" / "e2e" / time.strftime("%Y%m%d-%H%M%S")
    (run_dir / "frigate" / "media").mkdir(parents=True)
    shutil.copytree(E2E / "ha_config", run_dir / "ha")
    shutil.copy(E2E / "frigate" / "config.yml", run_dir / "frigate" / "config.yml")
    if args.mode == "sim":
        with open(run_dir / "ha" / "configuration.yaml", "a", encoding="utf-8") as fh:
            fh.write("\ncloudplus_e2e_sim:\n")
    expose = CACHE / "expose_camera_stream_source"
    if not expose.is_dir():
        log(f"Fetching hass-expose-camera-stream-source v{EXPOSE_VERSION}")
        with urllib.request.urlopen(EXPOSE_URL, timeout=60) as resp:
            data = resp.read()
        prefix = f"hass-expose-camera-stream-source-{EXPOSE_VERSION}/custom_components/"
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = [m for m in tar.getmembers()
                       if m.name.startswith(prefix + expose.name)]
            for member in members:
                member.name = member.name[len(prefix):]
            tar.extractall(CACHE, members=members, filter="data")
    cloudplus = REPO / "custom_components" / "cloudplus"
    if args.ref:
        # The integration as of a git ref (bisecting); the harness stays current.
        archive = subprocess.run(["git", "-C", str(REPO), "archive", args.ref,
                                  "custom_components/cloudplus"],
                                 check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(run_dir / "src", filter="data")
        cloudplus = run_dir / "src" / "custom_components" / "cloudplus"
    run = Run(args, run_dir)
    run.env = {
        "RUN_DIR": str(run_dir),
        "REPO_DIR": str(REPO),
        "CLOUDPLUS_DIR": str(cloudplus),
        "CACHE_DIR": str(CACHE),
        "HA_VERSION": args.ha_version,
        "FRIGATE_VERSION": args.frigate_version,
        "SIM_WAKE_DELAY_S": str(args.sim_wake_delay),
        "SIM_SESSION_CAP_S": str(args.sim_session_cap),
        "FRIGATE_HA_TOKEN": "",
        "FRIGATE_CAMERA_ENTITY": "",
    }
    return run


async def wait_for(what: str, probe, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = await probe()
            if result:
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


class Ha:
    """Just enough of HA's REST + websocket API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.token = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def onboard(self) -> None:
        async def ready():
            async with self.session.get(f"{HA_URL}/api/onboarding") as resp:
                return resp.status == 200
        await wait_for("HA to start", ready, 300)
        async with self.session.post(f"{HA_URL}/api/onboarding/users", json={
            "client_id": CLIENT_ID, "name": "e2e", "username": "e2e",
            "password": "e2e-password", "language": "en",
        }) as resp:
            resp.raise_for_status()
            code = (await resp.json())["auth_code"]
        async with self.session.post(f"{HA_URL}/auth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
        }) as resp:
            resp.raise_for_status()
            self.token = (await resp.json())["access_token"]
        self.token = await self.ws("auth/long_lived_access_token",
                                   client_name="cloudplus-e2e", lifespan=30)

    async def ws(self, msg_type: str, **payload):
        async with self.session.ws_connect(f"{HA_URL}/api/websocket") as sock:
            await sock.receive_json()
            await sock.send_json({"type": "auth", "access_token": self.token})
            if (await sock.receive_json())["type"] != "auth_ok":
                raise RuntimeError("HA websocket auth failed")
            await sock.send_json({"id": 1, "type": msg_type, **payload})
            reply = await sock.receive_json()
        if not reply.get("success"):
            raise RuntimeError(f"{msg_type} failed: {reply.get('error')}")
        return reply["result"]

    async def rest(self, method: str, path: str, **kw):
        async with self.session.request(method, f"{HA_URL}{path}",
                                        headers=self.headers, **kw) as resp:
            resp.raise_for_status()
            return await resp.json() if resp.content_type == "application/json" \
                else await resp.text()

    async def config_flow(self, handler: str, user_input: dict) -> None:
        flow = await self.rest("POST", "/api/config/config_entries/flow",
                               json={"handler": handler})
        while flow["type"] == "form":
            if flow.get("errors"):
                raise RuntimeError(f"{handler} config flow: {flow['errors']}")
            flow = await self.rest("POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
                                   json=user_input)
        if flow["type"] != "create_entry":
            raise RuntimeError(f"{handler} config flow ended with {flow}")

    async def stream_source(self, entity_id: str) -> str | None:
        url = (await self.rest("GET", f"/api/camera_stream_source/{entity_id}")).strip()
        return url if url.startswith("http") else None

    async def state(self, entity_id: str) -> str:
        return (await self.rest("GET", f"/api/states/{entity_id}"))["state"]

    async def service(self, domain: str, service: str, **data) -> None:
        await self.rest("POST", f"/api/services/{domain}/{service}", json=data)


async def setup_ha(run: Run, ha: Ha) -> None:
    log("Starting Home Assistant")
    run.compose("up", "-d", "homeassistant")
    await ha.onboard()
    run.token = ha.token
    if run.args.mode == "sim":
        async def sim_loaded():
            return "cloudplus_e2e_sim" in (await ha.rest("GET", "/api/config"))["components"]
        await wait_for("the sim component", sim_loaded, 120)
    await ha.config_flow("expose_camera_stream_source", {})
    if run.args.mode == "real":
        env = read_env_file()
        creds = {"email": env["EMAIL"], "password": env["PASSWORD"],
                 "country_code": env["COUNTRY_CODE"], "phone_code": env["PHONE_CODE"],
                 "app_profile": env["PROFILE"]}
    else:
        creds = {"email": "sim@example.invalid", "password": "sim"}
    log("Adding the cloudplus account")
    await ha.config_flow("cloudplus", creds)

    async def entities():
        found = {}
        for ent in await ha.ws("config/entity_registry/list"):
            if ent["platform"] != "cloudplus" or run.args.device not in ent["unique_id"]:
                continue
            for suffix in ("_camera", "_wake", "_sleep", "_motion_timeout"):
                if ent["unique_id"].endswith(suffix) and ent["disabled_by"] is None:
                    found.setdefault(suffix, ent["entity_id"])
        return found if len(found) == 4 else None
    found = await wait_for("cloudplus entities", entities, 180)
    run.camera, run.wake_button = found["_camera"], found["_wake"]
    run.sleep_button = found["_sleep"]
    run.motion_timeout = found["_motion_timeout"]
    log(f"Camera {run.camera}, wake {run.wake_button}")

    url = await wait_for("stream_source", lambda: ha.stream_source(run.camera), 120)
    log(f"stream_source: {url}")


async def sample(run: Run, ha: Ha, session: aiohttp.ClientSession, phase: str) -> Sample:
    """Frigate's view, HA's camera state, and the probe's video gap, right now."""
    fps = pid = None
    try:
        async with session.get(f"{FRIGATE_URL}/api/stats",
                               timeout=aiohttp.ClientTimeout(total=3)) as resp:
            cam = (await resp.json())["cameras"][FRIGATE_CAMERA]
            fps, pid = float(cam["camera_fps"]), cam.get("ffmpeg_pid")
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        pass
    try:
        state = await ha.state(run.camera)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        state = "?"
    item = Sample(round(time.monotonic() - run.started, 1), phase, state, fps, pid,
                  run.probe.gap())
    run.samples.append(item)
    return item


async def observe(run, ha, session, phase: str, seconds: float = 0.0, until=None,
                  timeout: float = 0.0) -> None:
    """Sample every second for ``seconds``, or until ``until(sample)`` is true."""
    log(f"Phase {phase}")
    end = time.monotonic() + (seconds or timeout)
    while time.monotonic() < end:
        item = await sample(run, ha, session, phase)
        if until is not None and until(item):
            return
        await asyncio.sleep(1)
    if until is not None:
        raise TimeoutError(f"phase {phase} did not finish within {timeout:.0f}s")


def _back_asleep():
    """Phase end test: HA showed the camera streaming, then idle again."""
    seen: set[str] = set()

    def done(item: Sample) -> bool:
        seen.add(item.ha_state)
        return "streaming" in seen and item.ha_state == "idle"
    return done


async def scenario(run: Run, ha: Ha, session: aiohttp.ClientSession) -> None:
    run.env.update(FRIGATE_HA_TOKEN=run.token, FRIGATE_CAMERA_ENTITY=run.camera)
    log("Starting Frigate")
    run.compose("up", "-d", "frigate")
    run.started = time.monotonic()
    probe = asyncio.create_task(run.probe.run(ha, session, run.camera))
    try:
        await _scenario_phases(run, ha, session)
    finally:
        probe.cancel()


async def _scenario_phases(run: Run, ha: Ha, session: aiohttp.ClientSession) -> None:
    args = run.args
    await observe(run, ha, session, "startup",
                  until=lambda s: (s.fps or 0) > 0 and s.video_gap_s is not None,
                  timeout=args.startup_timeout)
    run.checked_since = datetime.now(UTC).isoformat()
    await observe(run, ha, session, "sleep", args.sleep_s)
    for cycle in range(1, args.cycles + 1):
        last = cycle == args.cycles
        live_s = args.long_live_s if last else args.live_s
        await ha.service("number", "set_value", entity_id=run.motion_timeout, value=live_s)
        await ha.service("button", "press", entity_id=run.wake_button)
        await observe(run, ha, session, f"awake{cycle}", until=_back_asleep(),
                      timeout=live_s + args.sim_wake_delay + 240)
        await observe(run, ha, session, f"sleep{cycle}", args.sleep_s)
    # Sleep Camera ends a live session before its motion timeout.
    await ha.service("button", "press", entity_id=run.wake_button)
    await observe(run, ha, session, "awakeS", args.sim_wake_delay + 40)
    await ha.service("button", "press", entity_id=run.sleep_button)
    await observe(run, ha, session, "sleepS", until=lambda s: s.ha_state == "idle",
                  timeout=30)
    await observe(run, ha, session, "sleepS", args.sleep_s)


def report(run: Run, frigate_log: str, completed: bool) -> bool:
    with open(run.run_dir / "timeline.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t", "phase", "ha_state", "camera_fps", "ffmpeg_pid",
                         "video_gap_s"])
        for s in run.samples:
            writer.writerow([s.t, s.phase, s.ha_state, s.fps, s.ffmpeg_pid, s.video_gap_s])
    checked = [s for s in run.samples if s.phase not in ("startup", "aborted")]
    problems = [] if completed else ["the scenario did not complete (see above)"]
    print(f"\n{'phase':<10}{'secs':>6}{'min fps':>9}{'avg fps':>9}{'zero-fps s':>12}"
          f"{'max gap s':>11}")
    for phase in dict.fromkeys(s.phase for s in run.samples):
        rows = [s for s in run.samples if s.phase == phase]
        fps = [s.fps or 0.0 for s in rows]
        zero = sum(1 for f in fps if f == 0)
        gap = max((s.video_gap_s or 0.0 for s in rows), default=0.0)
        print(f"{phase:<10}{len(rows):>6}{min(fps):>9.1f}{sum(fps) / len(fps):>9.1f}"
              f"{zero:>12}{gap:>11.1f}")
    stalls = [s for s in checked if (s.video_gap_s or 0.0) > run.args.max_gap_s]
    if stalls:
        worst = max(stalls, key=lambda s: s.video_gap_s or 0.0)
        problems.append(f"no video on stream_source for up to {worst.video_gap_s} s "
                        f"(limit {run.args.max_gap_s} s), worst at t={worst.t}s in phase "
                        f"{worst.phase}; first over the limit at t={stalls[0].t}s")
    offline = [s for s in checked if not s.fps]
    if offline:
        problems.append(f"Frigate saw the camera offline for {len(offline)} s, first at "
                        f"t={offline[0].t}s in phase {offline[0].phase}")
    pids = list(dict.fromkeys(s.ffmpeg_pid for s in checked if s.ffmpeg_pid))
    if len(pids) > 1:
        problems.append(f"Frigate restarted its ffmpeg {len(pids) - 1} time(s)")
    watchdog = [line.split("|", 1)[-1].strip() for line in frigate_log.splitlines()
                if any(phrase in line for phrase in FRIGATE_WATCHDOG)]
    if watchdog:
        problems.append(f"Frigate logged {len(watchdog)} watchdog line(s), first: "
                        f"{watchdog[0]}")
    print()
    for problem in problems:
        print(f"FAIL  {problem}")
    print(f"{'FAIL' if problems else 'PASS'}  logs in {run.run_dir}")
    return not problems


async def main_async(run: Run) -> bool:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ha = Ha(session)
        try:
            await setup_ha(run, ha)
            await scenario(run, ha, session)
        except (TimeoutError, RuntimeError, aiohttp.ClientError) as exc:
            log(f"Aborted: {exc}")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--mode", choices=("sim", "real"), default="sim")
    parser.add_argument("--device", default="", help="real mode: part of the camera's SN")
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--sleep-s", type=float, default=60)
    parser.add_argument("--live-s", type=int, default=30, help="motion timeout per cycle")
    parser.add_argument("--long-live-s", type=int, default=150,
                        help="last cycle's motion timeout, past the ~118 s session cap")
    parser.add_argument("--startup-timeout", type=float, default=300)
    parser.add_argument("--max-gap-s", type=float, default=5,
                        help="longest allowed pause between video frames on stream_source")
    parser.add_argument("--sim-wake-delay", type=float, default=20)
    parser.add_argument("--sim-session-cap", type=float, default=118)
    parser.add_argument("--ha-version", default=os.environ.get("HA_VERSION", "2026.9.2"))
    parser.add_argument("--frigate-version",
                        default=os.environ.get("FRIGATE_VERSION", "0.18.0"))
    parser.add_argument("--ref", default="",
                        help="test custom_components/cloudplus as of this git ref")
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    args = parser.parse_args()

    run = prepare(args)
    log(f"Run dir {run.run_dir}")
    completed = False
    frigate_log = checked_log = ""
    try:
        completed = asyncio.run(main_async(run))
    except KeyboardInterrupt:
        log("Interrupted")
    finally:
        frigate_log = run.compose("logs", "--no-color", "frigate", check=False,
                                  capture_output=True, text=True).stdout
        (run.run_dir / "frigate.log").write_text(frigate_log, encoding="utf-8")
        if run.checked_since:
            checked_log = run.compose("logs", "--no-color", "--since", run.checked_since,
                                      "frigate", check=False, capture_output=True,
                                      text=True).stdout
        # The containers run as root; hand the run dir back to the caller.
        for service in ("homeassistant", "frigate"):
            run.compose("exec", "-T", service, "chown", "-R",
                        f"{os.getuid()}:{os.getgid()}", "/config", check=False,
                        capture_output=True)
        if not args.keep:
            run.compose("down", check=False, capture_output=True)
    return 0 if report(run, checked_log, completed) else 1


if __name__ == "__main__":
    sys.exit(main())
