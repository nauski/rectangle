"""GTK4 control panel — the button-driven front-end.

The engine (backend.py) does all the real work; this is just a small floating
window with Record/Stop plus format, fps and destination controls. It stays
compositor-agnostic: give the window a stable class ("rectangle") so the user
can float/pin it with a window rule, and keep it tiny so it stays out of frame.

Because wf-recorder captures a fixed rectangle, the only thing that can leak
into the video is this window itself. So we hide it while slurp is drawing and
shrink to a compact bar while recording.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import backend  # noqa: E402
from .backend import Settings  # noqa: E402

APP_ID = "dev.rectangle.Rectangle"

CSS = b"""
.rec-time { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 1.3rem; }
.record-btn { font-weight: 700; }
.hint { opacity: 0.6; font-size: 0.85rem; }
"""


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="rectangle")
        self.set_default_size(300, -1)
        self.set_resizable(False)

        self.settings = Settings.load()
        self.geometry: str | None = None
        self.session: backend.Session | None = None
        self._timer_id: int | None = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        self.set_child(outer)

        # --- region row -----------------------------------------------------
        self.select_btn = Gtk.Button(label="⬚  Select region")
        self.select_btn.connect("clicked", self.on_select)
        outer.append(self.select_btn)

        self.region_lbl = Gtk.Label(label="no region — will ask when you record")
        self.region_lbl.add_css_class("hint")
        self.region_lbl.set_xalign(0)
        outer.append(self.region_lbl)

        # --- format segmented ----------------------------------------------
        fmt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        fmt_box.add_css_class("linked")
        self.gif_btn = Gtk.ToggleButton(label="GIF")
        self.mp4_btn = Gtk.ToggleButton(label="MP4")
        self.mp4_btn.set_group(self.gif_btn)
        (self.gif_btn if self.settings.fmt == "gif" else self.mp4_btn).set_active(True)
        self.gif_btn.connect("toggled", self.on_fmt)
        fmt_box.append(self.gif_btn)
        fmt_box.append(self.mp4_btn)
        outer.append(self._labeled("Format", fmt_box))

        # --- fps ------------------------------------------------------------
        self.fps = Gtk.SpinButton.new_with_range(1, 60, 1)
        self.fps.set_value(self.settings.fps)
        self.fps.connect("value-changed", self.on_fps)
        outer.append(self._labeled("FPS", self.fps))

        # --- destinations ---------------------------------------------------
        self.clip_chk = Gtk.CheckButton(label="Copy to clipboard")
        self.clip_chk.set_active(self.settings.to_clipboard)
        self.clip_chk.connect("toggled", self.on_dest)
        self.file_chk = Gtk.CheckButton(label="Save to Videos")
        self.file_chk.set_active(self.settings.to_file)
        self.file_chk.connect("toggled", self.on_dest)
        outer.append(self.clip_chk)
        outer.append(self.file_chk)

        outer.append(Gtk.Separator())

        # --- record / stop --------------------------------------------------
        self.record_btn = Gtk.Button(label="●  Record")
        self.record_btn.add_css_class("record-btn")
        self.record_btn.add_css_class("suggested-action")
        self.record_btn.connect("clicked", self.on_record_clicked)
        outer.append(self.record_btn)

        self.status = Gtk.Label(label="")
        self.status.add_css_class("hint")
        self.status.set_wrap(True)
        outer.append(self.status)

        self._apply_css()

    # -- helpers -------------------------------------------------------------
    def _labeled(self, text: str, widget: Gtk.Widget) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        lbl.set_hexpand(True)
        row.append(lbl)
        row.append(widget)
        return row

    def _apply_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _set_controls_sensitive(self, on: bool) -> None:
        for w in (self.select_btn, self.gif_btn, self.mp4_btn, self.fps,
                  self.clip_chk, self.file_chk):
            w.set_sensitive(on)

    # -- settings callbacks --------------------------------------------------
    def on_fmt(self, _btn: Gtk.ToggleButton) -> None:
        self.settings.fmt = "gif" if self.gif_btn.get_active() else "mp4"
        self.settings.save()

    def on_fps(self, _spin: Gtk.SpinButton) -> None:
        self.settings.fps = int(self.fps.get_value())
        self.settings.save()

    def on_dest(self, _chk: Gtk.CheckButton) -> None:
        self.settings.to_clipboard = self.clip_chk.get_active()
        self.settings.to_file = self.file_chk.get_active()
        self.settings.save()

    # -- region selection ----------------------------------------------------
    def on_select(self, _btn: Gtk.Button) -> None:
        # Note: we deliberately do NOT hide the window here. Unmapping it makes
        # the compositor re-place (usually re-center) it on the next map, since
        # Wayland clients can't set their own position — that would lose wherever
        # the user parked the panel. Keeping it mapped preserves the position.
        def worker():
            try:
                geom = backend.select_region()
            except RuntimeError as e:
                GLib.idle_add(self._after_select, None, str(e))
                return
            GLib.idle_add(self._after_select, geom, None)

        threading.Thread(target=worker, daemon=True).start()

    def _after_select(self, geom: str | None, err: str | None) -> bool:
        if err:
            self.status.set_text(err)
        elif geom:
            self.geometry = geom
            self.region_lbl.set_text(f"region: {geom}")
        return False  # one-shot

    # -- record / stop -------------------------------------------------------
    def on_record_clicked(self, _btn: Gtk.Button) -> None:
        if self.session is not None:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        missing = backend.missing_deps()
        if missing:
            self.status.set_text("missing: " + ", ".join(missing))
            return
        self.record_btn.set_sensitive(False)
        self.status.set_text("")

        def worker():
            geom = self.geometry
            if geom is None:
                try:
                    geom = backend.select_region()
                except RuntimeError as e:
                    GLib.idle_add(self._start_failed, str(e))
                    return
                if geom is None:
                    GLib.idle_add(self._start_cancelled)
                    return
            try:
                sess = backend.start_recording(geom, self.settings)
            except RuntimeError as e:
                GLib.idle_add(self._start_failed, str(e))
                return
            GLib.idle_add(self._recording_started, sess, geom)

        threading.Thread(target=worker, daemon=True).start()

    def _start_failed(self, msg: str) -> bool:
        self.record_btn.set_sensitive(True)
        self.status.set_text(msg)
        return False

    def _start_cancelled(self) -> bool:
        self.record_btn.set_sensitive(True)
        self.status.set_text("selection cancelled")
        return False

    def _recording_started(self, sess: backend.Session, geom: str) -> bool:
        self.session = sess
        self.geometry = geom
        self.region_lbl.set_text(f"region: {geom}")
        self.record_btn.set_sensitive(True)
        self.record_btn.set_label("■  Stop  ·  00:00")
        self.record_btn.remove_css_class("suggested-action")
        self.record_btn.add_css_class("destructive-action")
        self._set_controls_sensitive(False)
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        return False

    def _tick(self) -> bool:
        if self.session is None:
            return False
        import time
        elapsed = int(time.time() - self.session.started)
        self.record_btn.set_label(f"■  Stop  ·  {elapsed // 60:02d}:{elapsed % 60:02d}")
        return True

    def _stop(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        sess = self.session
        self.session = None
        self.record_btn.set_sensitive(False)
        self.record_btn.set_label("Encoding…")

        def worker():
            try:
                out = backend.stop_recording(sess)
            except RuntimeError as e:
                GLib.idle_add(self._stopped, None, str(e))
                return
            GLib.idle_add(self._stopped, out, None)

        threading.Thread(target=worker, daemon=True).start()

    def _stopped(self, out: str | None, err: str | None) -> bool:
        self.record_btn.set_sensitive(True)
        self.record_btn.set_label("●  Record")
        self.record_btn.remove_css_class("destructive-action")
        self.record_btn.add_css_class("suggested-action")
        self._set_controls_sensitive(True)
        if err:
            self.status.set_text(err)
        else:
            dest = []
            if self.settings.to_clipboard:
                dest.append("clipboard")
            if self.settings.to_file:
                dest.append(out or "")
            self.status.set_text("✓ " + " · ".join(d for d in dest if d))
        return False


class RectangleApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self._win: Panel | None = None

    def do_activate(self):
        if self._win is None:
            self._win = Panel(self)
        self._win.present()


def run() -> int:
    return RectangleApp().run(None)
