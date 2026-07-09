"""Core engine: region selection, capture, encoding and delivery.

Everything here is compositor-agnostic. It shells out to the standard
wlroots-ecosystem tools (slurp, wf-recorder), ffmpeg and wl-clipboard, so
the same code runs on Hyprland, Sway, river, Wayfire, etc.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

# --- paths -------------------------------------------------------------------


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/rectangle-{os.getuid()}"
    d = Path(base) / "rectangle"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _videos_dir() -> Path:
    # honour XDG user dirs, fall back to ~/Videos
    xdg = os.environ.get("XDG_VIDEOS_DIR")
    if xdg:
        return Path(xdg)
    try:
        out = subprocess.run(
            ["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=2
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return Path.home() / "Videos"


STATE_FILE = _runtime_dir() / "session.json"


# --- dependency checks -------------------------------------------------------

REQUIRED = ("slurp", "wf-recorder", "ffmpeg")
OPTIONAL = ("wl-copy", "notify-send")


def missing_deps(include_optional: bool = False) -> list[str]:
    tools = list(REQUIRED) + (list(OPTIONAL) if include_optional else [])
    return [t for t in tools if shutil.which(t) is None]


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def notify(summary: str, body: str = "", *, urgency: str = "normal") -> None:
    if not have("notify-send"):
        return
    args = ["notify-send", "-a", "rectangle", "-u", urgency, summary]
    if body:
        args.append(body)
    try:
        subprocess.run(args, timeout=3)
    except subprocess.SubprocessError:
        pass


# --- settings ----------------------------------------------------------------


@dataclass
class Settings:
    fmt: str = "gif"           # "gif" or "mp4"
    fps: int = 20
    scale_width: int = 0       # 0 = keep source width (gif only)
    to_clipboard: bool = True
    to_file: bool = True
    audio: bool = False        # mp4 only; gif drops audio
    codec: str = "auto"        # mp4 encoder: "auto" | "vaapi" | "x264"

    @staticmethod
    def load() -> "Settings":
        cfg = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "rectangle"
            / "config.json"
        )
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text())
                known = {k: data[k] for k in asdict(Settings()) if k in data}
                return Settings(**known)
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return Settings()

    def save(self) -> None:
        cfg_dir = (
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "rectangle"
        )
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(json.dumps(asdict(self), indent=2))


# --- session state (for the toggle model) -----------------------------------


@dataclass
class Session:
    pid: int                    # wf-recorder pid
    geometry: str               # slurp-style "x,y wxh"
    raw_path: str               # intermediate capture file
    started: float
    settings: dict = field(default_factory=dict)


def read_session() -> Session | None:
    if not STATE_FILE.is_file():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        sess = Session(**data)
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not _pid_alive(sess.pid):
        STATE_FILE.unlink(missing_ok=True)
        return None
    return sess


def _write_session(sess: Session) -> None:
    STATE_FILE.write_text(json.dumps(asdict(sess)))


def _clear_session() -> None:
    STATE_FILE.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- pipeline steps ----------------------------------------------------------


# slurp styling: a gentle dark dim outside the selection (not the default
# white "flashbang"), a fully transparent interior so you see what you're
# framing, and a thin accent border. Overridable via RECTANGLE_SLURP_ARGS.
_SLURP_STYLE = [
    "-b", "1c1c1c99",   # background dim outside selection (dark, ~60%)
    "-s", "00000000",   # selection interior fully transparent
    "-c", "4a90d9ff",   # border accent
    "-w", "2",          # border weight
]


def select_region() -> str | None:
    """Run slurp; return slurp-format geometry, or None if cancelled."""
    extra = os.environ.get("RECTANGLE_SLURP_ARGS")
    args = ["slurp"] + (extra.split() if extra else _SLURP_STYLE)
    try:
        out = subprocess.run(args, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("slurp is not installed")
    geom = out.stdout.strip()
    if out.returncode != 0 or not geom:
        return None
    return geom


def _timestamp_name() -> str:
    # local time, filename-safe
    return time.strftime("rectangle-%Y%m%d-%H%M%S")


def start_recording(geometry: str, settings: Settings) -> Session:
    """Launch wf-recorder against `geometry`. Returns a live Session."""
    raw = _runtime_dir() / f"{_timestamp_name()}.mkv"
    cmd = ["wf-recorder", "-y", "-g", geometry, "-f", str(raw)]
    if settings.fmt == "mp4":
        # constant framerate for a clean mp4; gif is re-timed in ffmpeg later
        cmd += ["-r", str(settings.fps)]
    if settings.audio and settings.fmt == "mp4":
        cmd.append("--audio")
    # keep stderr for diagnostics
    log = open(_runtime_dir() / "wf-recorder.log", "wb")
    proc = subprocess.Popen(cmd, stdout=log, stderr=log)
    # wf-recorder needs a beat to attach to the output before it's really rolling
    time.sleep(0.35)
    if proc.poll() is not None:
        err = (_runtime_dir() / "wf-recorder.log").read_text(errors="replace")
        raise RuntimeError(f"wf-recorder failed to start:\n{err.strip()}")
    sess = Session(
        pid=proc.pid,
        geometry=geometry,
        raw_path=str(raw),
        started=time.time(),
        settings=asdict(settings),
    )
    _write_session(sess)
    return sess


def stop_recording(sess: Session) -> str:
    """Signal wf-recorder to finalise, then encode + deliver. Returns out path."""
    # wf-recorder writes a valid file when interrupted with SIGINT
    try:
        os.kill(sess.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    _wait_for_exit(sess.pid, timeout=10)
    _clear_session()

    settings = Settings(**{k: sess.settings[k] for k in asdict(Settings()) if k in sess.settings})
    raw = Path(sess.raw_path)
    if not raw.is_file() or raw.stat().st_size == 0:
        raise RuntimeError("capture produced no data (was the recording too short?)")

    out = _encode(raw, settings)
    _deliver(out, settings)
    raw.unlink(missing_ok=True)
    return str(out)


def _wait_for_exit(pid: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    # last resort
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _encode(raw: Path, settings: Settings) -> Path:
    dest_dir = _videos_dir() if settings.to_file else _runtime_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = raw.stem

    if settings.fmt == "mp4":
        return _encode_mp4(raw, dest_dir / f"{stem}.mp4", settings)

    # GIF with a proper generated palette (two-pass in one graph)
    out = dest_dir / f"{stem}.gif"
    scale = f"scale={settings.scale_width}:-1:flags=lanczos," if settings.scale_width else ""
    vf = (
        f"fps={settings.fps},{scale}"
        "split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    cmd = ["ffmpeg", "-y", "-i", str(raw), "-vf", vf, "-loop", "0", str(out)]
    _run_ffmpeg(cmd)
    return out


def _vaapi_device() -> str | None:
    """Pick a VAAPI render node, honouring RECTANGLE_VAAPI_DEVICE."""
    env = os.environ.get("RECTANGLE_VAAPI_DEVICE")
    if env:
        return env if Path(env).exists() else None
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    return str(nodes[0]) if nodes else None


def _mp4_x264(raw: Path, out: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-i", str(raw),
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        str(out),
    ]


def _mp4_vaapi(raw: Path, out: Path, device: str) -> list[str]:
    # software-decode the raw capture, upload to the GPU, encode with h264_vaapi
    return [
        "ffmpeg", "-y", "-vaapi_device", device, "-i", str(raw),
        "-vf", "format=nv12,hwupload",
        "-c:v", "h264_vaapi", "-qp", "24",
        "-movflags", "+faststart",
        str(out),
    ]


def _encode_mp4(raw: Path, out: Path, settings: Settings) -> Path:
    """Encode mp4, using VAAPI when requested/available, falling back to x264."""
    codec = settings.codec
    device = _vaapi_device()
    use_vaapi = codec == "vaapi" or (codec == "auto" and device is not None
                                     and _encoder_available("h264_vaapi"))
    if use_vaapi and device:
        try:
            _run_ffmpeg(_mp4_vaapi(raw, out, device))
            return out
        except RuntimeError:
            if codec == "vaapi":
                raise  # user explicitly asked for it; don't hide the failure
            # auto: hardware path failed, fall back to software silently
    _run_ffmpeg(_mp4_x264(raw, out))
    return out


_ENCODER_CACHE: dict[str, bool] = {}


def _encoder_available(name: str) -> bool:
    if name not in _ENCODER_CACHE:
        try:
            res = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            _ENCODER_CACHE[name] = name in res.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            _ENCODER_CACHE[name] = False
    return _ENCODER_CACHE[name]


def _run_ffmpeg(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg failed:\n{tail}")


def _deliver(out: Path, settings: Settings) -> None:
    if settings.to_clipboard:
        _copy_to_clipboard(out, settings.fmt)


def _copy_to_clipboard(path: Path, fmt: str) -> None:
    if not have("wl-copy"):
        raise RuntimeError("wl-copy (wl-clipboard) not installed; cannot copy")
    mime = "image/gif" if fmt == "gif" else "video/mp4"
    with open(path, "rb") as fh:
        # wl-copy detaches and holds the selection in the background
        subprocess.run(["wl-copy", "--type", mime], stdin=fh, check=True)
