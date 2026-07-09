"""Command-line entry point.

Design: a single bound key runs `rectangle`. The *first* press selects a
region and starts recording; the *next* press stops, encodes and delivers.
This "toggle" model means one keybinding drives the whole capture, which is
the fastest possible path for the "record a clip, get a gif on the clipboard"
workflow.
"""

from __future__ import annotations

import argparse
import sys

from . import backend
from .backend import Settings


def _add_output_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gif", dest="fmt", action="store_const", const="gif",
                   help="output an animated GIF (default)")
    p.add_argument("--mp4", dest="fmt", action="store_const", const="mp4",
                   help="output an H.264 mp4 instead of a GIF")
    p.add_argument("--fps", type=int, help="frames per second (default from config)")
    p.add_argument("--width", type=int, dest="scale_width",
                   help="scale output to this width in px (0 = source width)")
    p.add_argument("--clipboard", dest="to_clipboard", action="store_true",
                   default=None, help="copy result to the clipboard")
    p.add_argument("--no-clipboard", dest="to_clipboard", action="store_false",
                   help="do not touch the clipboard")
    p.add_argument("--file", dest="to_file", action="store_true", default=None,
                   help="save result to your Videos directory")
    p.add_argument("--no-file", dest="to_file", action="store_false",
                   help="do not save a file (clipboard only)")
    p.add_argument("--audio", action="store_true", default=None,
                   help="capture audio (mp4 only)")
    p.add_argument("--vaapi", dest="codec", action="store_const", const="vaapi",
                   help="encode mp4 with VAAPI hardware acceleration")
    p.add_argument("--x264", dest="codec", action="store_const", const="x264",
                   help="encode mp4 with software libx264")


def _merge_settings(args: argparse.Namespace) -> Settings:
    s = Settings.load()
    for attr in ("fmt", "fps", "scale_width", "to_clipboard", "to_file", "audio", "codec"):
        val = getattr(args, attr, None)
        if val is not None:
            setattr(s, attr, val)
    return s


def cmd_toggle(args: argparse.Namespace) -> int:
    sess = backend.read_session()
    if sess is not None:
        return _do_stop(sess)
    return _do_start(_merge_settings(args))


def cmd_start(args: argparse.Namespace) -> int:
    if backend.read_session() is not None:
        print("rectangle: already recording", file=sys.stderr)
        return 1
    return _do_start(_merge_settings(args))


def cmd_stop(_args: argparse.Namespace) -> int:
    sess = backend.read_session()
    if sess is None:
        print("rectangle: not recording", file=sys.stderr)
        return 1
    return _do_stop(sess)


def _do_start(settings: Settings) -> int:
    missing = backend.missing_deps()
    if missing:
        msg = "missing required tools: " + ", ".join(missing)
        print(f"rectangle: {msg}", file=sys.stderr)
        backend.notify("rectangle", msg, urgency="critical")
        return 1
    try:
        geom = backend.select_region()
    except RuntimeError as e:
        print(f"rectangle: {e}", file=sys.stderr)
        return 1
    if geom is None:
        print("rectangle: selection cancelled", file=sys.stderr)
        return 130
    try:
        backend.start_recording(geom, settings)
    except RuntimeError as e:
        print(f"rectangle: {e}", file=sys.stderr)
        backend.notify("rectangle: failed to start", str(e), urgency="critical")
        return 1
    backend.notify("● Recording", f"{settings.fmt.upper()} · press the key again to stop")
    print(f"rectangle: recording {geom} -> {settings.fmt}")
    return 0


def _do_stop(sess: backend.Session) -> int:
    backend.notify("Encoding…", "rectangle is finalising your clip")
    try:
        out = backend.stop_recording(sess)
    except RuntimeError as e:
        print(f"rectangle: {e}", file=sys.stderr)
        backend.notify("rectangle: failed", str(e), urgency="critical")
        return 1
    dest = []
    if sess.settings.get("to_clipboard"):
        dest.append("clipboard")
    if sess.settings.get("to_file"):
        dest.append(out)
    backend.notify("✓ Saved", " · ".join(dest) if dest else out)
    print(f"rectangle: done -> {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rectangle",
        description="Draw a rectangle, record it, get a GIF or mp4 on your clipboard.",
    )
    p.set_defaults(func=None)
    sub = p.add_subparsers(dest="command")

    # bare `rectangle` == toggle
    tog = sub.add_parser("toggle", help="start if idle, stop if recording (default)")
    _add_output_flags(tog)
    tog.set_defaults(func=cmd_toggle)

    start = sub.add_parser("start", help="select a region and start recording")
    _add_output_flags(start)
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop recording and deliver the clip")
    stop.set_defaults(func=cmd_stop)

    gui = sub.add_parser("gui", help="open the graphical control panel")
    gui.set_defaults(func=cmd_gui)

    # allow output flags on the bare invocation too
    _add_output_flags(p)
    return p


def cmd_gui(_args: argparse.Namespace) -> int:
    try:
        from .panel import run as run_gui
    except ImportError as e:
        print(f"rectangle: GUI unavailable ({e}); install PyGObject/GTK4",
              file=sys.stderr)
        return 1
    return run_gui()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        # bare `rectangle` (with or without output flags) -> toggle
        return cmd_toggle(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
