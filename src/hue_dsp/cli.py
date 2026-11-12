#!/usr/bin/env python3
"""hue-dsp — Real-Time 48 kHz CoreAudio vDSP + 50 Hz DTLS 1.2 Philips Hue Entertainment Engine."""

from __future__ import annotations

import argparse
import colorsys
import http.client
import json
import math
import os
import re
import shlex
import signal
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PID_FILE = Path(os.environ.get("HUE_PID_FILE", "/tmp/hue-spotify-sync.pid"))
LOG_FILE = Path(os.environ.get("HUE_LOG_FILE", "/tmp/hue-spotify-sync.log"))
DSP_READY_BANNER = "[DSP 50Hz -> Hue] Live CoreAudio Tap"

EC_UUID_RE = re.compile(r"\bec=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b")


def load_secrets() -> None:
    """Load HUE_* credentials from environment, ~/.config/hue-dsp/config.env, or ~/.config/agents/secrets.env."""
    candidates: list[Path] = []
    custom = os.environ.get("HUE_CONFIG_FILE")
    if custom:
        candidates.append(Path(custom).expanduser())
    candidates.extend(
        [
            Path.home() / ".config/hue-dsp/config.env",
            Path.home() / ".config/agents/secrets.env",
        ]
    )
    for cfg in candidates:
        if not cfg.exists():
            continue
        for raw in cfg.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                key_name = k.strip()
                if key_name.startswith("HUE_"):
                    os.environ.setdefault(key_name, v.strip().strip('"').strip("'"))


def _require_bridge_config(require_client_key: bool = False) -> tuple[str, str, str]:
    load_secrets()
    ip = os.environ.get("HUE_BRIDGE_IP", "").strip()
    key = os.environ.get("HUE_APP_KEY", "").strip()
    client_key = os.environ.get("HUE_CLIENT_KEY", "").strip()
    if not ip:
        sys.exit(
            "Error: HUE_BRIDGE_IP not set. Run 'hue-dsp discover' / 'hue-dsp pair' or set HUE_BRIDGE_IP in ~/.config/hue-dsp/config.env"
        )
    if not key:
        sys.exit("Error: HUE_APP_KEY not set. Run 'hue-dsp pair' or set HUE_APP_KEY in ~/.config/hue-dsp/config.env")
    if require_client_key and not client_key:
        sys.exit(
            "Error: HUE_CLIENT_KEY not set. Run 'hue-dsp pair' or set HUE_CLIENT_KEY in ~/.config/hue-dsp/config.env"
        )
    return ip, key, client_key


def discover_bridges(timeout_s: float = 3.5) -> list[dict[str, str]]:
    """Discover Philips Hue Bridges via the MeetHue discovery endpoint and configured environment."""
    found: list[dict[str, str]] = []
    seen_ips: set[str] = set()

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request("https://discovery.meethue.com", method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s) as resp:
            items = json.loads(resp.read().decode("utf-8", errors="replace"))
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    ip = str(item.get("internalipaddress", "")).strip()
                    bid = str(item.get("id", "")).strip()
                    if ip and ip not in seen_ips:
                        seen_ips.add(ip)
                        found.append({"ip": ip, "id": bid})
    except Exception:
        pass

    load_secrets()
    env_ip = os.environ.get("HUE_BRIDGE_IP", "").strip()
    env_id = os.environ.get("HUE_BRIDGE_ID", "").strip()
    if env_ip and env_ip not in seen_ips:
        seen_ips.add(env_ip)
        found.append({"ip": env_ip, "id": env_id})

    return found


def _write_secure_config(dest: Path, updates: dict[str, str]) -> None:
    """Atomically write HUE_* configuration with 0600 permissions via temp file + os.replace while preserving extra keys."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if dest.exists():
        try:
            for raw in dest.read_text(errors="replace").splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k_clean = k.strip()
                    if k_clean.startswith("HUE_"):
                        existing[k_clean] = v.strip().strip('"').strip("'")
        except OSError:
            pass
    for k, v in updates.items():
        if v:
            existing[k] = v

    lines = ["# hue-dsp configuration (chmod 600)"]
    for k, v in existing.items():
        lines.append(f'{k}="{v}"')
    payload = ("\n".join(lines) + "\n").encode("utf-8")

    tmp_dest = dest.parent / f".{dest.name}.tmp.{os.getpid()}"
    fd = os.open(str(tmp_dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_dest, dest)


def pair_bridge(ip: str | None = None, config_path: Path | None = None) -> dict[str, str]:
    """Pair with a Philips Hue Bridge (generating both V1 app key and DTLS 1.2 clientkey)."""
    target_ip = (ip or "").strip()
    bridge_id = ""
    if not target_ip:
        bridges = discover_bridges()
        if not bridges:
            sys.exit("Error: No Hue Bridge discovered on LAN. Specify --ip <bridge_ip>.")
        target_ip = bridges[0]["ip"]
        bridge_id = bridges[0].get("id", "")

    url = f"https://{target_ip}/api"
    body = json.dumps({"devicetype": "hue-dsp#macos", "generateclientkey": True}).encode("utf-8")
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=5.0) as resp:
            reply = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        sys.exit(f"Error: Failed to reach Hue Bridge at {target_ip} ({exc})")

    if not isinstance(reply, list) or not reply:
        sys.exit(f"Error: Unexpected pairing response from {target_ip}: {reply}")

    first = reply[0]
    if "error" in first:
        desc = first["error"].get("description", "link button not pressed")
        sys.exit(
            f"Error: Hue Bridge at {target_ip} rejected pairing ({desc}). Press the physical Link button on the Hue Bridge and re-run 'hue-dsp pair --ip {target_ip}'."
        )

    success = first.get("success") or {}
    username = str(success.get("username", "")).strip()
    clientkey = str(success.get("clientkey", "")).strip()
    if not username or not clientkey:
        sys.exit(f"Error: Incomplete credentials returned by Hue Bridge: {reply}")

    dest = config_path or Path(os.environ.get("HUE_CONFIG_FILE", str(Path.home() / ".config/hue-dsp/config.env")))
    _write_secure_config(
        dest,
        {
            "HUE_BRIDGE_IP": target_ip,
            "HUE_BRIDGE_ID": bridge_id,
            "HUE_APP_KEY": username,
            "HUE_CLIENT_KEY": clientkey,
        },
    )
    return {
        "ip": target_ip,
        "id": bridge_id,
        "username": username,
        "clientkey": clientkey,
        "config_path": str(dest),
    }


def api(method: str, path: str, payload: dict | None = None):
    ip, key, _ = _require_bridge_config()
    url = f"https://{ip}/api/{key}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
        return json.loads(resp.read().decode())


def _resolve_default_group(explicit_group: str | None) -> str:
    if explicit_group:
        return explicit_group
    load_secrets()
    env_group = os.environ.get("HUE_DEFAULT_GROUP", "").strip()
    if env_group:
        return env_group
    try:
        groups = api("GET", "/groups")
        if isinstance(groups, dict) and groups:
            for gid, g in groups.items():
                if isinstance(g, dict) and g.get("type") == "Room":
                    return str(gid)
            return str(next(iter(groups.keys())))
    except Exception:
        pass
    return "1"


def rgb_to_xy(r: int, g: int, b: int) -> list[float]:
    """Convert 8-bit sRGB (0..255) to CIE 1931 (x, y) chromaticity coordinates (Wide RGB D65)."""

    def gamma(c: float) -> float:
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r_lin, g_lin, b_lin = gamma(r / 255.0), gamma(g / 255.0), gamma(b / 255.0)
    x_val = r_lin * 0.664511 + g_lin * 0.154324 + b_lin * 0.162028
    y_val = r_lin * 0.283881 + g_lin * 0.668433 + b_lin * 0.047685
    z_val = r_lin * 0.000088 + g_lin * 0.072310 + b_lin * 0.986039
    total = x_val + y_val + z_val
    if total <= 1e-6:
        return [0.3127, 0.3290]
    return [round(x_val / total, 4), round(y_val / total, 4)]


NAMED_COLORS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 80, 255),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "purple": (160, 32, 240),
    "orange": (255, 120, 0),
    "yellow": (255, 220, 0),
    "pink": (255, 60, 150),
    "gold": (255, 180, 30),
    "white": (255, 255, 255),
}


def parse_color(val: str) -> dict:
    v = val.strip().lower()
    if v in ("warm", "relax"):
        return {"ct": 400}
    if v in ("neutral", "reading", "read"):
        return {"ct": 300}
    if v in ("cool", "concentrate", "daylight", "energize"):
        return {"ct": 175}
    if v.endswith("k") and v[:-1].isdigit():
        kelvin = max(2000, min(6500, int(v[:-1])))
        return {"ct": int(round(1_000_000 / kelvin))}
    if v in NAMED_COLORS:
        return {"xy": rgb_to_xy(*NAMED_COLORS[v])}
    h = v.lstrip("#")
    if len(h) == 6:
        try:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return {"xy": rgb_to_xy(r, g, b)}
        except ValueError:
            pass
    sys.exit(
        f"Unknown color '{val}'. Use hex (#ff6600), Kelvin (2700K..6500K), warm/neutral/cool, or {', '.join(NAMED_COLORS)}"
    )


def get_spotify_live() -> dict | None:
    script = (
        'tell application "System Events" to set isRunning to (name of processes) contains "Spotify"\n'
        'if not isRunning then return "STOPPED"\n'
        'tell application "Spotify"\n'
        "  set pState to player state as string\n"
        '  if pState is "stopped" then return "STOPPED"\n'
        "  set tId to id of current track\n"
        "  set tName to name of current track\n"
        "  set tArtist to artist of current track\n"
        "  set tAlbum to album of current track\n"
        "  set tArt to artwork url of current track\n"
        "  set pPos to player position\n"
        "  set tDur to (duration of current track) / 1000.0\n"
        '  return pState & "|||" & tId & "|||" & tName & "|||" & tArtist & "|||" & tAlbum & "|||" & tArt & "|||" & (pPos as string) & "|||" & (tDur as string)\n'
        "end tell"
    )
    try:
        out = subprocess.check_output(["osascript", "-e", script], text=True, timeout=2).strip()
        if out == "STOPPED" or "|||" not in out:
            return None
        parts = out.split("|||")
        return {
            "state": parts[0],
            "track_id": parts[1].split(":")[-1],
            "title": parts[2],
            "artist": parts[3],
            "album": parts[4],
            "artwork_url": parts[5],
            "position_s": float(parts[6].replace(",", ".")),
            "duration_s": float(parts[7].replace(",", ".")),
        }
    except Exception:
        return None


def analyze_track_signature(track_id: str, artwork_url: str) -> dict:
    """Extract 5 vivid stage colors + BPM & beat energy profile from Spotify embed & artwork."""
    palette_rgb: list[tuple[int, int, int]] = []
    bpm = 118.0
    energy = 0.68
    envelope: list[float] = []

    try:
        html = urllib.request.urlopen(f"https://open.spotify.com/embed/track/{track_id}", timeout=4).read().decode()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        preview_url = None
        if m:
            d = json.loads(m.group(1))
            entity = d.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            vi = entity.get("visualIdentity") or {}
            for key in (
                "backgroundBase",
                "backgroundTintedBase",
                "textBrightAccent",
                "textSubdued",
            ):
                c = vi.get(key)
                if isinstance(c, dict) and "red" in c:
                    palette_rgb.append((int(c["red"]), int(c["green"]), int(c["blue"])))
            preview_url = (entity.get("audioPreview") or {}).get("url")

        if artwork_url:
            with tempfile.TemporaryDirectory() as td:
                jpg = Path(td) / "art.jpg"
                bmp = Path(td) / "art.bmp"
                urllib.request.urlretrieve(artwork_url, jpg)
                subprocess.run(
                    [
                        "sips",
                        "-z",
                        "24",
                        "24",
                        "-s",
                        "format",
                        "bmp",
                        str(jpg),
                        "--out",
                        str(bmp),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                if bmp.exists():
                    raw = bmp.read_bytes()
                    offset = struct.unpack_from("<I", raw, 10)[0]
                    buckets: dict[int, list[tuple[float, int, int, int]]] = {}
                    for i in range(offset, len(raw) - 3, 3):
                        b_val, g_val, r_val = raw[i], raw[i + 1], raw[i + 2]
                        h, s, v = colorsys.rgb_to_hsv(r_val / 255.0, g_val / 255.0, b_val / 255.0)
                        if v < 0.15:
                            continue
                        bucket = int(h * 12) % 12
                        score = (s + 0.25) * (v + 0.2)
                        buckets.setdefault(bucket, []).append((score, r_val, g_val, b_val))
                    ranked = sorted(
                        buckets.values(),
                        key=lambda items: sum(x[0] for x in items),
                        reverse=True,
                    )
                    for group in ranked[:5]:
                        best = max(group, key=lambda x: x[0])
                        palette_rgb.insert(0, (best[1], best[2], best[3]))

        if preview_url and Path("/opt/homebrew/bin/ffmpeg").exists():
            proc = subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    preview_url,
                    "-ac",
                    "1",
                    "-ar",
                    "4000",
                    "-f",
                    "s16le",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            pcm = proc.stdout
            if len(pcm) > 8000:
                samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
                win = 200
                rms_frames = []
                for idx in range(0, len(samples) - win, win):
                    chunk = samples[idx : idx + win]
                    rms = math.sqrt(sum(s * s for s in chunk) / win) / 32768.0
                    rms_frames.append(rms)
                if rms_frames:
                    max_rms = max(rms_frames) or 1.0
                    envelope = [min(1.0, x / max_rms) for x in rms_frames]
                    energy = min(1.0, max(0.35, sum(envelope) / len(envelope) * 1.25))
                    onsets = [max(0.0, envelope[i] - envelope[i - 1]) for i in range(1, len(envelope))]
                    best_lag, best_corr = 10, -1.0
                    for lag in range(7, 16):
                        corr = sum(onsets[i] * onsets[i - lag] for i in range(lag, len(onsets)))
                        if corr > best_corr:
                            best_corr, best_lag = corr, lag
                    bpm = round(60.0 / (best_lag / 20.0), 1)
    except Exception:
        pass

    vivid_palette: list[tuple[int, int, int]] = []
    seen_hues: list[float] = []
    for r, g, b in palette_rgb:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if s < 0.55:
            s = min(1.0, s + 0.52)
        v = max(0.75, min(1.0, v))
        if all(min(abs(h - sh), 1.0 - abs(h - sh)) > 0.07 for sh in seen_hues) or len(vivid_palette) < 2:
            seen_hues.append(h)
            nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
            vivid_palette.append((int(nr * 255), int(ng * 255), int(nb * 255)))
        if len(vivid_palette) == 5:
            break

    base_h = seen_hues[0] if seen_hues else 0.08
    golden_offsets = [0.0, 0.16, 0.52, 0.68, 0.84]
    while len(vivid_palette) < 5:
        h = (base_h + golden_offsets[len(vivid_palette)]) % 1.0
        nr, ng, nb = colorsys.hsv_to_rgb(h, 0.88, 0.95)
        vivid_palette.append((int(nr * 255), int(ng * 255), int(nb * 255)))

    return {
        "palette_rgb": vivid_palette[:5],
        "palette_hex": [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in vivid_palette[:5]],
        "palette_xy": [rgb_to_xy(r, g, b) for r, g, b in vivid_palette[:5]],
        "bpm": bpm,
        "energy": round(energy, 2),
        "envelope": envelope or [0.55, 0.85, 0.45, 0.95, 0.60, 0.80, 0.50, 1.0],
    }


def run_spotify_sync(once: bool = False, duration_s: float = 0.0) -> None:
    ip, key, _ = _require_bridge_config()

    lights = ["1", "2", "3", "4", "5"]
    try:
        all_lights = api("GET", "/lights")
        if isinstance(all_lights, dict) and all_lights:
            lights = sorted(all_lights.keys(), key=lambda x: int(x) if x.isdigit() else 999)[:5]
    except Exception:
        pass

    ctx = ssl._create_unverified_context()
    conns: dict[str, http.client.HTTPSConnection] = {}

    def put_light(item: tuple[str, dict]) -> None:
        lid, payload = item
        body = json.dumps(payload)
        path = f"/api/{key}/lights/{lid}/state"
        for _ in range(2):
            try:
                c = conns.get(lid)
                if c is None:
                    c = http.client.HTTPSConnection(ip, timeout=2.5, context=ctx)
                    conns[lid] = c
                c.request(
                    "PUT",
                    path,
                    body,
                    {"Content-Type": "application/json", "Connection": "keep-alive"},
                )
                c.getresponse().read()
                return
            except Exception:
                old = conns.pop(lid, None)
                if old:
                    try:
                        old.close()
                    except Exception:
                        pass
        try:
            subprocess.run(
                [
                    "/usr/bin/curl",
                    "-sk",
                    "-m",
                    "2",
                    "-X",
                    "PUT",
                    f"https://{ip}{path}",
                    "-d",
                    body,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.5,
            )
        except Exception:
            pass

    current_track_id = None
    sig = None
    tick = 0
    start_ts = time.time()

    with ThreadPoolExecutor(max_workers=5) as pool:
        while True:
            sp = get_spotify_live()
            if not sp or sp["state"] != "playing":
                if once:
                    print("Spotify is not currently playing.")
                    return
                time.sleep(1.5)
                continue

            if sp["track_id"] != current_track_id or sig is None:
                current_track_id = sp["track_id"]
                sig = analyze_track_signature(current_track_id, sp["artwork_url"])
                msg = (
                    f"[Spotify -> Hue] Track: '{sp['title']}' — {sp['artist']} ({sp['album']}) | "
                    f"BPM={sig['bpm']} Energy={sig['energy']} | Palette={','.join(sig['palette_hex'])}"
                )
                print(msg, flush=True)

            beat_period = 60.0 / max(60.0, sig["bpm"])
            env = sig["envelope"]
            pos = sp["position_s"]
            env_idx = int((pos * 4.0) + tick) % len(env)
            beat_pulse = env[env_idx]
            transition = max(2, min(4, int(round(beat_period * 4.5))))

            tasks: list[tuple[str, dict]] = []
            for idx, lid in enumerate(lights):
                color_idx = (idx + tick) % 5
                xy = sig["palette_xy"][color_idx]
                phase = math.sin((tick * 1.05) + (idx * 1.25))
                base_bri = 115 + int(sig["energy"] * 65)
                dynamic_swing = int((beat_pulse - 0.48) * 115 + phase * 68)
                bri = max(45, min(254, base_bri + dynamic_swing))
                tasks.append(
                    (
                        lid,
                        {
                            "on": True,
                            "xy": xy,
                            "bri": bri,
                            "transitiontime": transition,
                        },
                    )
                )

            list(pool.map(put_light, tasks))

            tick += 1
            if once:
                return
            if duration_s > 0 and (time.time() - start_ts) >= duration_s:
                return
            time.sleep(max(0.38, min(0.75, beat_period * 0.90)))


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        st = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "stat="], text=True, timeout=1.5).strip()
        if st.startswith("Z"):
            return False
    except Exception:
        pass
    return True


def _pid_lstart(pid: int) -> str | None:
    """Return kernel process start timestamp string for PID-reuse disambiguation."""
    try:
        st = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "lstart="], text=True, timeout=1.5).strip()
        return st if st else None
    except Exception:
        return None


def _is_hue_daemon_pid(pid: int, expected_lstart: str | None = None, require_lstart: bool = True) -> bool:
    """Verify strictly (fail-closed) that pid belongs to a Hue DSP/REST daemon and matches its start timestamp."""
    if not _pid_alive(pid):
        return False
    try:
        if require_lstart and not expected_lstart:
            return False
        if expected_lstart:
            live_lstart = _pid_lstart(pid)
            if not live_lstart or live_lstart != expected_lstart:
                return False

        cmd = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "command="], text=True, timeout=1.5).strip()
        if not cmd:
            return False
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return False
        if not tokens:
            return False

        exe_path = tokens[0]
        exe_name = os.path.basename(exe_path)
        allowed_bins = {"hue-dsp-stream", "hue-dsp", "hue-query", "hue_query.py"}
        override = os.environ.get("HUE_DSP_BIN")
        override_resolved: str | None = None
        if override:
            try:
                override_resolved = str(Path(override).resolve())
            except OSError:
                override_resolved = None

        try:
            exe_resolved = str(Path(exe_path).resolve())
        except OSError:
            exe_resolved = exe_path

        if exe_name in allowed_bins or (override_resolved is not None and exe_resolved == override_resolved):
            return True

        this_cli_resolved = str(Path(__file__).resolve())
        if exe_name.startswith(("python", "Python", "uv", "bash", "sh")):
            i = 1
            if exe_name == "uv" and i < len(tokens) and tokens[i] == "run":
                i += 1
                if i < len(tokens) and os.path.basename(tokens[i]).startswith(("python", "Python")):
                    i += 1
            if i >= len(tokens):
                return False
            if tokens[i] == "-m":
                return (i + 1 < len(tokens)) and tokens[i + 1] in ("hue_dsp.cli", "hue_dsp")
            if tokens[i].startswith("-"):
                return False
            target_tok = tokens[i]
            target_bn = os.path.basename(target_tok)
            if target_bn in allowed_bins:
                return True
            try:
                target_resolved = str(Path(target_tok).resolve())
                if target_resolved == this_cli_resolved or (
                    override_resolved is not None and target_resolved == override_resolved
                ):
                    return True
            except OSError:
                pass
        return False
    except Exception:
        # Fail closed on any ps inspection error or timeout so unrelated processes are never signaled
        return False


def _read_pid_record() -> tuple[int | None, str | None, str | None]:
    if not PID_FILE.exists():
        return None, None, None
    try:
        lines = [ln.strip() for ln in PID_FILE.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return None, None, None
    if not lines:
        return None, None, None
    try:
        pid = int(lines[0])
    except ValueError:
        pid = None
    ec_id: str | None = None
    lstart: str | None = None
    for extra in lines[1:]:
        if ec_id is None and len(extra) == 36 and extra.count("-") == 4:
            ec_id = extra
        elif extra.startswith("lstart="):
            lstart = extra[len("lstart=") :].strip() or None
    return pid, ec_id, lstart


def _write_pid_record(pid: int, ec_id: str | None = None) -> bool:
    lstart = _pid_lstart(pid)
    if not lstart:
        return False
    rec_lines = [str(pid)]
    if ec_id and len(ec_id) == 36:
        rec_lines.append(ec_id)
    rec_lines.append(f"lstart={lstart}")
    PID_FILE.write_text("\n".join(rec_lines) + "\n")
    return True


def _extract_recorded_ec_id(hint_ec_id: str | None = None) -> str | None:
    if hint_ec_id and len(hint_ec_id) == 36:
        return hint_ec_id
    _, rec_ec, _ = _read_pid_record()
    if rec_ec:
        return rec_ec
    if LOG_FILE.exists():
        try:
            tail_lines = LOG_FILE.read_text(errors="replace").splitlines()[-40:]
            for ln in reversed(tail_lines):
                m = EC_UUID_RE.search(ln)
                if m:
                    return m.group(1)
        except OSError:
            pass
    return None


def _clip_v2_request(ip: str, key: str, method: str, path: str, payload: dict | None = None) -> dict:
    url = f"https://{ip}{path}"
    body_str = json.dumps(payload) if payload is not None else None

    try:
        data = body_str.encode("utf-8") if body_str is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("hue-application-key", key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=3.0) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", errors="replace")
        if not (200 <= status < 300):
            raise RuntimeError(f"CLIP v2 {method} {path} returned HTTP {status}")
        parsed = json.loads(raw)
    except Exception as primary_err:
        curl_cmd = [
            "/usr/bin/curl",
            "-sk",
            "-m",
            "3",
            "-w",
            "\n%{http_code}",
            "-X",
            method,
            "-H",
            f"hue-application-key: {key}",
        ]
        if body_str is not None:
            curl_cmd.extend(["-H", "Content-Type: application/json", "-d", body_str])
        curl_cmd.append(url)
        proc = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=4.0)
        if proc.returncode != 0:
            raise RuntimeError(
                f"CLIP v2 {method} {path} failed ({primary_err}; curl rc={proc.returncode})"
            ) from primary_err
        out_lines = proc.stdout.strip().splitlines()
        if not out_lines:
            raise RuntimeError(f"CLIP v2 {method} {path} returned empty response") from primary_err
        try:
            http_code = int(out_lines[-1].strip())
        except ValueError as ve:
            raise RuntimeError(f"CLIP v2 {method} {path} malformed status footer") from ve
        if not (200 <= http_code < 300):
            raise RuntimeError(f"CLIP v2 {method} {path} returned HTTP {http_code}")
        parsed = json.loads("\n".join(out_lines[:-1]))

    if not isinstance(parsed, dict):
        raise RuntimeError(f"CLIP v2 {method} {path} returned non-object payload")
    errs = parsed.get("errors")
    if isinstance(errs, list) and len(errs) > 0:
        raise RuntimeError(f"CLIP v2 {method} {path} errors: {errs}")
    return parsed


def _force_stop_entertainment_rest(hint_ec_id: str | None = None) -> None:
    """Dynamically resolve Entertainment Configuration(s), send stop, and verify inactive status."""
    load_secrets()
    ip = os.environ.get("HUE_BRIDGE_IP", "").strip()
    key = os.environ.get("HUE_APP_KEY", "").strip()
    if not ip or not key:
        raise RuntimeError("HUE_BRIDGE_IP or HUE_APP_KEY not set; cannot verify CLIP v2 Entertainment teardown")

    recorded_ec = _extract_recorded_ec_id(hint_ec_id)
    listing = _clip_v2_request(ip, key, "GET", "/clip/v2/resource/entertainment_configuration")
    items = listing.get("data")
    if not isinstance(items, list) or not items:
        raise RuntimeError("CLIP v2 returned zero entertainment_configuration resources")

    target_ids: list[str] = []
    for item in items:
        cid = item.get("id")
        if isinstance(cid, str) and len(cid) == 36 and item.get("status") == "active":
            target_ids.append(cid)

    if recorded_ec and recorded_ec not in target_ids:
        if any(item.get("id") == recorded_ec for item in items):
            target_ids.append(recorded_ec)

    if not target_ids:
        music = next(
            (
                it.get("id")
                for it in items
                if it.get("configuration_type") == "music" and isinstance(it.get("id"), str) and len(it.get("id")) == 36
            ),
            None,
        )
        chosen = music or (items[0].get("id") if isinstance(items[0].get("id"), str) else None)
        if not chosen or len(chosen) != 36:
            raise RuntimeError("Could not resolve valid 36-char entertainment_configuration UUID")
        target_ids.append(chosen)

    for ec_id in target_ids:
        _clip_v2_request(
            ip,
            key,
            "PUT",
            f"/clip/v2/resource/entertainment_configuration/{ec_id}",
            {"action": "stop"},
        )

    for ec_id in target_ids:
        inactive = False
        for _ in range(8):
            st_resp = _clip_v2_request(
                ip,
                key,
                "GET",
                f"/clip/v2/resource/entertainment_configuration/{ec_id}",
            )
            st_data = st_resp.get("data")
            if isinstance(st_data, list) and any(
                isinstance(d, dict) and d.get("id") == ec_id and d.get("status") == "inactive" for d in st_data
            ):
                inactive = True
                break
            time.sleep(0.08)
        if not inactive:
            raise RuntimeError(
                f"Entertainment configuration {ec_id} did not report status == 'inactive' after stop command"
            )


def stop_existing_daemon(timeout_s: float = 8.0) -> tuple[bool, int | None]:
    """Send SIGTERM and block until confirmed process exit + acknowledged CLIP v2 Entertainment teardown."""
    if not PID_FILE.exists():
        return False, None

    pid, recorded_ec, recorded_lstart = _read_pid_record()
    if pid is None:
        try:
            _force_stop_entertainment_rest()
        except Exception as exc:
            sys.exit(f"Error: stale/corrupt PID file cleanup failed CLIP v2 teardown verification ({exc})")
        PID_FILE.unlink(missing_ok=True)
        return False, None

    if not _is_hue_daemon_pid(pid, expected_lstart=recorded_lstart, require_lstart=True):
        try:
            _force_stop_entertainment_rest()
        except Exception as exc:
            sys.exit(f"Error: stale PID {pid} cleanup failed CLIP v2 teardown verification ({exc})")
        PID_FILE.unlink(missing_ok=True)
        return False, pid

    stop_log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            _force_stop_entertainment_rest()
        except Exception as exc:
            sys.exit(f"Error: exited PID {pid} failed CLIP v2 teardown verification ({exc})")
        PID_FILE.unlink(missing_ok=True)
        return False, pid
    except OSError as sig_err:
        sys.exit(f"Error: failed to send SIGTERM to live Hue daemon PID {pid} ({sig_err}); retaining PID file")

    deadline = time.monotonic() + timeout_s
    killed_hard = False
    while time.monotonic() < deadline:
        if not _is_hue_daemon_pid(pid, expected_lstart=recorded_lstart, require_lstart=True):
            break
        time.sleep(0.05)
    else:
        if _is_hue_daemon_pid(pid, expected_lstart=recorded_lstart, require_lstart=True):
            try:
                os.kill(pid, signal.SIGKILL)
                killed_hard = True
            except ProcessLookupError:
                pass
            except OSError as kill_err:
                sys.exit(f"Error: could not SIGKILL unresponsive Hue daemon PID {pid} ({kill_err}); retaining PID file")
            for _ in range(20):
                if not _is_hue_daemon_pid(pid, expected_lstart=recorded_lstart, require_lstart=True):
                    break
                time.sleep(0.05)

    if _is_hue_daemon_pid(pid, expected_lstart=recorded_lstart, require_lstart=True):
        sys.exit(f"Error: Hue daemon PID {pid} remained alive after SIGKILL; refusing to clear PID file or proceed")

    shutdown_tail = ""
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, "rb") as rf:
                rf.seek(stop_log_offset)
                shutdown_tail = rf.read().decode("utf-8", errors="replace")
        except OSError:
            shutdown_tail = ""

    ack_marker = (
        f"[DSP 50Hz -> Hue] Entertainment teardown acknowledged (ec={recorded_ec})."
        if recorded_ec
        else "[DSP 50Hz -> Hue] Entertainment teardown acknowledged"
    )
    daemon_verified_teardown = (
        (not killed_hard)
        and (ack_marker in shutdown_tail)
        and ("CLIP v2 Entertainment stop unacknowledged" not in shutdown_tail)
    )
    if not daemon_verified_teardown:
        try:
            _force_stop_entertainment_rest()
        except Exception as exc:
            sys.exit(f"Error: CLIP v2 Entertainment teardown failed after stopping PID {pid} ({exc})")

    PID_FILE.unlink(missing_ok=True)
    time.sleep(0.10)
    return True, pid


def ensure_dsp_binary() -> Path | None:
    override = os.environ.get("HUE_DSP_BIN")
    if override:
        cand = Path(override)
        return cand if (cand.exists() and os.access(cand, os.X_OK)) else None

    dsp_bin = Path.home() / ".local/bin/hue-dsp-stream"
    src_dir = Path(__file__).resolve().parent / "native"
    if not (src_dir / "build.sh").exists():
        src_dir = Path(__file__).resolve().parent / "hue_dsp"
    build_sh = src_dir / "build.sh"

    needs_build = not (dsp_bin.exists() and os.access(dsp_bin, os.X_OK))
    if not needs_build and build_sh.exists():
        try:
            bin_mtime = dsp_bin.stat().st_mtime
            for src_name in (
                "dtls_hue.h",
                "dtls_hue.c",
                "hue_dsp_stream.swift",
                "build.sh",
            ):
                sp = src_dir / src_name
                if sp.exists() and sp.stat().st_mtime > bin_mtime:
                    needs_build = True
                    break
        except OSError:
            pass

    if needs_build and build_sh.exists():
        try:
            subprocess.run(["/bin/bash", str(build_sh), str(dsp_bin)], check=True, timeout=30)
        except Exception:
            pass

    return dsp_bin if (dsp_bin.exists() and os.access(dsp_bin, os.X_OK)) else None


def spawn_verified_dsp_daemon(dsp_bin: Path, velvet: bool, ready_timeout_s: float = 6.5) -> int:
    """Spawn hue-dsp-stream and wait for explicit readiness acknowledgement before returning."""
    cmd = [str(dsp_bin)] + (["--velvet"] if velvet else [])
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0

    with open(LOG_FILE, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )
    if not _write_pid_record(proc.pid):
        PID_FILE.write_text(f"{proc.pid}\n")

    deadline = time.monotonic() + ready_timeout_s
    ready = False
    tail = ""
    while time.monotonic() < deadline:
        rc = proc.poll()
        if LOG_FILE.exists():
            try:
                with open(LOG_FILE, "rb") as rf:
                    rf.seek(log_offset)
                    tail = rf.read().decode("utf-8", errors="replace")
            except OSError:
                tail = ""
        else:
            tail = ""

        if rc is not None:
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
            cleanup_note = ""
            cleanup_ok = False
            try:
                _force_stop_entertainment_rest()
                cleanup_ok = True
            except Exception as cleanup_exc:
                cleanup_note = f"; CLIP cleanup error: {cleanup_exc}"
            if cleanup_ok:
                PID_FILE.unlink(missing_ok=True)
            detail = tail.strip() or f"exit code {rc}"
            sys.exit(f"Error: hue-dsp-stream failed during startup ({detail}{cleanup_note})")

        if DSP_READY_BANNER in tail and proc.poll() is None:
            ready = True
            break

        time.sleep(0.05)

    if not ready or proc.poll() is not None:
        m_ec_partial = EC_UUID_RE.search(tail)
        ec_val = m_ec_partial.group(1) if m_ec_partial else None
        if not _write_pid_record(proc.pid, ec_val):
            PID_FILE.write_text(f"{proc.pid}\n" + (f"{ec_val}\n" if ec_val else ""))

        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                pass
        if proc.poll() is None or _pid_alive(proc.pid):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except OSError as kill_err:
                sys.exit(
                    f"Error: could not SIGKILL timed-out startup child PID {proc.pid} ({kill_err}); retaining PID file"
                )
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            for _ in range(20):
                if proc.poll() is not None and not _pid_alive(proc.pid):
                    break
                time.sleep(0.05)

        if proc.poll() is None or _pid_alive(proc.pid):
            sys.exit(
                f"Error: timed-out startup child PID {proc.pid} remained alive after SIGKILL; retaining PID file {PID_FILE}"
            )

        cleanup_note = ""
        cleanup_ok = False
        try:
            _force_stop_entertainment_rest()
            cleanup_ok = True
        except Exception as cleanup_exc:
            cleanup_note = f"; CLIP cleanup error: {cleanup_exc}"
        if cleanup_ok:
            PID_FILE.unlink(missing_ok=True)
        sys.exit(
            f"Error: hue-dsp-stream did not report readiness within {ready_timeout_s:.1f}s (see {LOG_FILE}{cleanup_note})"
        )

    m_ec = EC_UUID_RE.search(tail)
    if not _write_pid_record(proc.pid, m_ec.group(1) if m_ec else None):
        proc.terminate()
        PID_FILE.unlink(missing_ok=True)
        sys.exit("Error: failed to record kernel lstart identity for hue-dsp-stream")
    return proc.pid


def main() -> None:
    p = argparse.ArgumentParser(
        description="hue-dsp — Real-Time 48 kHz CoreAudio vDSP + 50 Hz DTLS 1.2 Philips Hue Entertainment Engine"
    )
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        help="discover | pair | start | velvet | stop | spotify | status | scenes | on | off | bri | color | scene | flash",
    )
    p.add_argument(
        "value",
        nargs="?",
        help="Brightness (1-100%%), color name/hex/Kelvin, scene name, or spotify subcommand (start|velvet|stop|once|status)",
    )
    p.add_argument("--ip", help="Hue Bridge IPv4 address (for pair)")
    p.add_argument("--light", "-l", help="Target specific light ID instead of room group")
    p.add_argument("--group", "-g", help="Target room group ID (auto-detected if omitted)")
    p.add_argument("--bri", "-b", type=int, help="Optional brightness percentage (1-100)")
    p.add_argument("--once", action="store_true", help="Sync Spotify palette once and exit")
    p.add_argument(
        "--rest",
        action="store_true",
        help="Force HTTP REST polling instead of 50Hz DTLS CoreAudio DSP stream",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Internal flag for background Spotify sync daemon",
    )
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    args = p.parse_args()

    # Support top-level shortcuts: hue-dsp start | velvet | stop
    action = args.action.lower()
    if action in ("start", "velvet", "stop"):
        args.value = action
        action = "spotify"

    if action == "discover":
        bridges = discover_bridges()
        if args.json:
            print(json.dumps(bridges, indent=2))
            return
        if not bridges:
            print("No Philips Hue Bridges discovered on the local network.")
            return
        for b in bridges:
            print(f"Hue Bridge: {b['ip']} (id={b.get('id') or 'unknown'})")
        return

    if action == "pair":
        res = pair_bridge(ip=args.ip or args.value)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"Paired with Hue Bridge {res['ip']} — credentials saved to {res['config_path']}")
        return

    if action == "spotify":
        sub = (args.value or ("once" if args.once else "start")).lower()
        if args.daemon:
            run_spotify_sync(once=False)
            return
        if sub == "stop":
            had_pid_file = PID_FILE.exists()
            stopped, pid = stop_existing_daemon()
            if stopped:
                print(f"Stopped Hue Spotify sync daemon (PID {pid}).")
            elif had_pid_file:
                print(f"Daemon already stopped (cleaned stale PID {pid}).")
            else:
                print("No Hue Spotify sync daemon is running.")
            return
        if sub == "status":
            pid, rec_ec, rec_lstart = _read_pid_record()
            running = bool(pid and _is_hue_daemon_pid(pid, expected_lstart=rec_lstart))
            suffix = f" (PID {pid}" + (f", ec={rec_ec}" if rec_ec else "") + ")" if running else ""
            print(f"Daemon running: {running}{suffix}")
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(errors="ignore").strip().splitlines()
                for ln in lines[-3:]:
                    print(ln)
            return
        if sub == "once" or args.once:
            stop_existing_daemon()
            run_spotify_sync(once=True)
            return

        stop_existing_daemon()

        load_secrets()
        velvet = sub in ("velvet", "sexy", "noir", "intimate")
        dsp_bin = None if args.rest else ensure_dsp_binary()
        if dsp_bin and os.environ.get("HUE_CLIENT_KEY"):
            pid = spawn_verified_dsp_daemon(dsp_bin, velvet=velvet)
            mode_label = "Velvet Noir" if velvet else "Auto Artwork"
            print(
                f"Live 48kHz CoreAudio DSP + 50Hz DTLS HueStream v2.0 active (PID {pid}, mode={mode_label}, log: {LOG_FILE})."
            )
            print("Stop anytime with: hue-dsp spotify stop")
            return

        run_spotify_sync(once=True)
        with open(LOG_FILE, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "spotify", "--daemon"],
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
        time.sleep(0.25)
        if proc.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            sys.exit(f"Error: REST Spotify sync daemon exited prematurely (rc={proc.returncode})")
        _write_pid_record(proc.pid)
        print(f"Live Spotify -> Philips Hue reactive daemon active (PID {proc.pid}, log: {LOG_FILE}).")
        print("Stop anytime with: hue-dsp spotify stop")
        return

    if action in ("on", "off", "bri", "brightness", "color", "scene") and PID_FILE.exists():
        stop_existing_daemon()

    if action == "status":
        data = api("GET", "")
        if args.json:
            print(json.dumps(data, indent=2))
            return
        ip = os.environ.get("HUE_BRIDGE_IP", "")
        bid = os.environ.get("HUE_BRIDGE_ID", "") or ((data.get("config") or {}).get("bridgeid", ""))
        header_id = f" ({bid})" if bid else ""
        print(f"Philips Hue Bridge — {ip}{header_id}")
        pid, _, rec_lstart = _read_pid_record()
        if pid and _is_hue_daemon_pid(pid, expected_lstart=rec_lstart):
            print(f"  [Spotify Sync Daemon: ACTIVE (PID {pid})]")
        print("\nRooms / Groups:")
        for gid, g in data.get("groups", {}).items():
            st = g.get("state", {})
            print(
                f"  [{gid}] {g.get('name')} ({g.get('type')}) — all_on={st.get('all_on')} any_on={st.get('any_on')} lights={','.join(g.get('lights', []))}"
            )
        print("\nLights:")
        for lid, light in data.get("lights", {}).items():
            st = light.get("state", {})
            pct = round((st.get("bri", 0) / 254) * 100) if st.get("on") else 0
            if st.get("colormode") == "ct" and st.get("ct"):
                mode_str = f"{round(1_000_000 / st['ct'])}K"
            elif st.get("colormode") == "xy" and st.get("xy"):
                mode_str = f"xy={st['xy']}"
            else:
                mode_str = st.get("colormode", "")
            print(
                f"  [{lid}] {light.get('name', ''):<18} | {'ON ' if st.get('on') else 'OFF'} | bri={pct:>3}% ({st.get('bri', 0):>3}/254) | mode={mode_str} | reachable={st.get('reachable')}"
            )
        return

    if action == "scenes":
        scenes = api("GET", "/scenes")
        for sid, s in sorted(scenes.items(), key=lambda x: x[1].get("name", "")):
            print(
                f"  {sid:<18} | {s.get('name', ''):<22} | group={s.get('group', '')} | lights={','.join(s.get('lights', []))}"
            )
        return

    group_id = _resolve_default_group(args.group)
    target_path = f"/lights/{args.light}/state" if args.light else f"/groups/{group_id}/action"
    payload: dict = {}

    if action == "on":
        payload["on"] = True
        if args.value and args.value.rstrip("%").isdigit():
            pct = max(1, min(100, int(args.value.rstrip("%"))))
            payload["bri"] = max(1, min(254, round(pct * 254 / 100)))
    elif action == "off":
        payload["on"] = False
    elif action in ("bri", "brightness"):
        if not args.value:
            sys.exit("Usage: hue-dsp bri <1-100>")
        pct = max(1, min(100, int(args.value.rstrip("%"))))
        payload = {"on": True, "bri": max(1, min(254, round(pct * 254 / 100)))}
    elif action == "color":
        if not args.value:
            sys.exit("Usage: hue-dsp color <name|#hex|2700K|warm|cool>")
        payload = {"on": True, **parse_color(args.value)}
    elif action == "scene":
        if not args.value:
            sys.exit("Usage: hue-dsp scene <scene_name_or_id>")
        scenes = api("GET", "/scenes")
        q = args.value.strip().lower()
        match_id = None
        for sid, s in scenes.items():
            if sid.lower() == q or s.get("name", "").lower() == q:
                match_id = sid
                break
        if not match_id:
            for sid, s in scenes.items():
                if q in s.get("name", "").lower():
                    match_id = sid
                    break
        if not match_id:
            sys.exit(f"Scene '{args.value}' not found. Run 'hue-dsp scenes' to list scenes.")
        payload = {"scene": match_id}
        target_path = f"/groups/{group_id}/action"
    elif action == "flash":
        payload = {"alert": "select"}
    else:
        sys.exit(
            f"Unknown action '{args.action}'. Use: discover, pair, start, velvet, stop, status, scenes, on, off, bri, color, scene, flash, spotify"
        )

    if args.bri is not None:
        pct = max(1, min(100, args.bri))
        payload["bri"] = max(1, min(254, round(pct * 254 / 100)))

    res = api("PUT", target_path, payload)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
