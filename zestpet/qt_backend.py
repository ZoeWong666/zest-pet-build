#!/usr/bin/env python3
"""PyQt6 front end for Zest: window, rendering, interaction, menu, tray.

Runs on macOS and Windows from one code path. Fully offline.
"""
from __future__ import annotations

import random
import sys
import time
from typing import Dict, Optional, Tuple

from PIL import ImageQt
from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QAction, QCursor, QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from . import core
from .core import COMMON, ClipLibrary, Compositor, PetState

SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
DRAG_THRESHOLD = 3
CLICK_MAX_SECONDS = 0.3
LOOK_POLL_MS = 100
IDLE_BEFORE_WANDER = 12.0
# Ground speed is matched to each cycle's own stride so the paws grip instead of
# skating. Measured from the art by tracking how far the ground-contact pixels
# travel: the walk keeps all eight frames planted and drags 121px through its
# 1.0s cycle; the run's three contact frames drag 77px in 0.30s, and the
# airborne half covers the same again. A single 55px/sec for both left the pet
# treadmilling — a full gallop advanced it 33px, under a third of a body length.
GAIT_SPEED = {"walking": 121.0, "running": 257.0}  # px/sec
RUN_DISTANCE = 320  # far enough that strolling there would take too long
MIN_WANDER_DISTANCE = 40  # don't bother strolling less than this
GRAVITY = 1400.0  # px/sec^2
FLOOR_MARGIN = 8
# Used when the platform does not report a taskbar or Dock reservation, which
# happens on macOS whenever the Dock is set to auto-hide.
DEFAULT_BOTTOM_RESERVE = 60
PERSONA_LABEL = {COMMON: "🐶 Normal", "evil": "😈 Evil"}

# On macOS a Qt.Tool window becomes an NSPanel, and the system hides panels as
# soon as the owning application is deactivated — clicking the desktop made the
# pet vanish. A plain Qt.Window stays put, which is what the original Cocoa
# build used (a borderless NSWindow at floating level). Elsewhere Qt.Tool is
# what keeps the pet out of the taskbar, so pick per platform.
PET_WINDOW_KIND = Qt.WindowType.Window if sys.platform == "darwin" else Qt.WindowType.Tool


def to_pixmap(pil_img) -> QPixmap:
    return QPixmap.fromImage(ImageQt.ImageQt(pil_img))


def _gait_of(name: str):
    """Split a locomotion clip name into (gait, direction), or None."""
    gait, _, direction = name.rpartition("-")
    return (gait, direction) if gait in GAIT_SPEED and direction in ("left", "right") else None


class PetWidget(QLabel):
    """The pet's visible surface. Forwards input to the window."""

    def __init__(self, owner: "PetWindow") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def mousePressEvent(self, event) -> None:
        self._owner.on_press(event)

    def mouseMoveEvent(self, event) -> None:
        self._owner.on_move(event)

    def mouseReleaseEvent(self, event) -> None:
        self._owner.on_release(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self._owner.on_double_click(event)


class PetWindow(QWidget):
    def __init__(self, library: ClipLibrary, config: dict) -> None:
        super().__init__()
        self.library = library
        self.config = config
        self.state = PetState(library=library, persona=config.get("persona", COMMON))
        if self.state.persona not in library.personas():
            self.state.persona = COMMON
        self.compositor = Compositor(library)
        self.scale: float = float(config.get("scale", 1.0))
        self.active_props = {p for p in config.get("props", []) if p in library.props}
        self._pixmaps: Dict[tuple, QPixmap] = {}
        self._drag_origin: Optional[QPoint] = None
        self._drag_window_origin: Optional[QPoint] = None
        self._dragging = False
        self._press_time = 0.0
        self._swallow_next_release = False
        self._cycle_index: Optional[int] = None
        self._clip_at_press: Optional[str] = None
        self._last_drag_x = 0
        self._last_interaction = time.perf_counter()
        self._wander_target: Optional[int] = None
        self._wander_gait: str = "running"
        self._wander_carry: float = 0.0
        self._fall_velocity: Optional[float] = None
        self._tray: Optional[QSystemTrayIcon] = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            # Without this the compositor draws a drop shadow around the
            # frameless window, which shows up as a translucent rectangle
            # behind the pet. The old Cocoa build called setHasShadow_(False).
            | Qt.WindowType.NoDropShadowWindowHint
            | PET_WINDOW_KIND
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.label = PetWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

        self.state.play("idle")
        self._apply_window_size()
        self._restore_position()
        self.set_click_through(bool(config.get("click_through", False)))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(16)
        self._look_timer = QTimer(self)
        self._look_timer.timeout.connect(self.poll_look)
        self._look_timer.start(LOOK_POLL_MS)

    # ── geometry ─────────────────────────────────────
    def _clip_size(self) -> Tuple[int, int]:
        """Target widget size: dynamic clips keep their own aspect, others use the cell."""
        clip = self.state.clip
        base = clip.size if (clip and clip.dynamic and clip.size[0]) else self.library.cell
        return max(1, int(base[0] * self.scale)), max(1, int(base[1] * self.scale))

    def _apply_window_size(self) -> None:
        """Resize around the bottom edge and horizontal centre so feet stay planted."""
        w, h = self._clip_size()
        if (self.width(), self.height()) == (w, h):
            return
        old = self.geometry()
        center_x = old.x() + old.width() // 2
        bottom = old.y() + old.height() if old.height() else old.y() + h
        self.setFixedSize(w, h)
        self.label.setFixedSize(w, h)
        if old.height():
            self.move(center_x - w // 2, bottom - h)

    def _current_screen_geometry(self):
        center = self.geometry().center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _floor_y(self) -> int:
        """Window top position that rests the pet's visible paws on the floor.

        Two corrections over "bottom of the available area":

        Qt does not always report the taskbar or Dock. On this machine
        availableGeometry().bottom() equals the full screen bottom whenever the
        Dock is set to auto-hide, and the value changes as that setting changes,
        so the pet dropped to the very bottom and its feet went behind the Dock.
        When nothing is reported, hold back a default reserve instead.

        The frames also carry different amounts of transparent padding under the
        paws, so the position is measured from the artwork, not the canvas edge.
        """
        center = self.geometry().center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        full, available = screen.geometry(), screen.availableGeometry()
        reserved = full.bottom() - available.bottom()
        if reserved <= 0:
            reserved = DEFAULT_BOTTOM_RESERVE
        baseline = full.bottom() - reserved - FLOOR_MARGIN
        clip = self.state.clip
        if clip is None or not clip.size[1]:
            return baseline - self.height()
        visible_bottom = round(clip.content_bottom * self.height() / clip.size[1])
        return baseline - visible_bottom

    def _restore_position(self) -> None:
        pos = self.config.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            candidate = QPoint(int(pos[0]), int(pos[1]))
            for screen in QGuiApplication.screens():
                if screen.availableGeometry().contains(candidate):
                    self.move(candidate)
                    return
        # First run: stand on the desktop near the right edge rather than
        # floating in mid-air, so gravity and wandering start from the floor.
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 120, self._floor_y())

    # ── rendering ────────────────────────────────────
    def render(self) -> None:
        clip = self.state.clip
        if clip is None:
            return
        self._apply_window_size()
        size = (self.label.width(), self.label.height())
        props = frozenset(self.active_props)
        key = (clip.persona, clip.name, self.state.frame % clip.frame_count, props, size)
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            composed = self.compositor.render(clip, self.state.frame, props, self.library.cell)
            pixmap = to_pixmap(composed)
            if (pixmap.width(), pixmap.height()) != size:
                pixmap = pixmap.scaled(
                    size[0], size[1],
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            if len(self._pixmaps) > 512:
                self._pixmaps.clear()
            self._pixmaps[key] = pixmap
        self.label.setPixmap(pixmap)

    def _invalidate(self) -> None:
        self._pixmaps.clear()
        self.compositor.clear()

    # ── main loop ────────────────────────────────────
    def tick(self) -> None:
        changed = self.state.advance()
        moved = self._step_fall() or self._step_wander()
        if changed or moved:
            self.render()
        # Idle instead of spinning at 60Hz when nothing is moving.
        interval = 16 if (self._fall_velocity is not None or self._wander_target is not None) \
            else int(max(16, min(120, self.state.next_wake * 1000)))
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def _step_fall(self) -> bool:
        if self._fall_velocity is None:
            return False
        floor = self._floor_y()
        self._fall_velocity += GRAVITY * (self._timer.interval() / 1000.0)
        new_y = int(self.y() + self._fall_velocity * (self._timer.interval() / 1000.0))
        if new_y >= floor:
            self.move(self.x(), floor)
            self._fall_velocity = None
            if not self.state.play("land", temp=True):
                self.state.play("idle")
            self._remember_position()
        else:
            self.move(self.x(), new_y)
        return True

    def _pick_wander_target(self, area, span: int) -> Optional[int]:
        """A random x at least MIN_WANDER_DISTANCE away, or None if there is none.

        The reachable stretch is the screen minus the pet's own width; the part
        worth walking to is that stretch with a MIN_WANDER_DISTANCE hole punched
        around the current position. Draw uniformly across what is left of it, so
        a target is always found when one exists and every reachable spot stays
        equally likely.
        """
        low, high = area.x(), area.x() + span
        ranges = [(lo, hi) for lo, hi in ((low, self.x() - MIN_WANDER_DISTANCE),
                                         (self.x() + MIN_WANDER_DISTANCE, high))
                  if lo <= hi]
        if not ranges:
            return None
        pick = random.randint(0, sum(hi - lo + 1 for lo, hi in ranges) - 1)
        for lo, hi in ranges:
            width = hi - lo + 1
            if pick < width:
                return lo + pick
            pick -= width
        return None  # unreachable: pick is always inside one of the ranges

    def _gait_for(self, distance: int) -> str:
        """Stroll a short hop, run a long one.

        Walking art is evil-only, so any persona without it always runs.
        """
        if distance < RUN_DISTANCE and self.library.has("walking-right", self.state.persona):
            return "walking"
        return "running"

    def _step_wander(self) -> bool:
        """Stroll to a random spot along the current screen after a quiet spell."""
        if self.state.busy or self._dragging or self._fall_velocity is not None:
            self._wander_target = None
            return False
        now = time.perf_counter()
        area = self._current_screen_geometry()
        if self._wander_target is None:
            if not self.config.get("wander", True):
                return False
            if now - self._last_interaction < IDLE_BEFORE_WANDER:
                return False
            span = max(0, area.width() - self.width())
            if span < MIN_WANDER_DISTANCE:
                return False  # screen too narrow to walk anywhere worth going
            # Pick straight out of the range that is actually far enough away,
            # rather than guessing and rejecting. Rejection sampling stalled
            # wandering on a narrow screen: with only a third of the screen far
            # enough to be worth walking to, a run of close picks was likely
            # enough to happen, and giving up also reset the idle timer, which
            # cost another IDLE_BEFORE_WANDER before the next attempt.
            target = self._pick_wander_target(area, span)
            if target is None:
                return False
            self._wander_target = target
            self._wander_gait = self._gait_for(abs(target - self.x()))
            self._wander_carry = 0.0
        gait = self._wander_gait
        direction = 1 if self._wander_target > self.x() else -1
        self.state.play(f"{gait}-right" if direction > 0 else f"{gait}-left")
        # Carry the fraction of a pixel over to the next tick. Truncating it
        # away made the real speed a function of the timer interval rather than
        # of the setting: at 16ms a stroll advanced a whole pixel per tick,
        # 62 px/sec, whatever the constant said.
        advance = GAIT_SPEED[gait] * self._timer.interval() / 1000.0 + self._wander_carry
        step = int(advance)
        self._wander_carry = advance - step
        new_x = self.x() + direction * step
        arrived = (new_x >= self._wander_target) if direction > 0 else (new_x <= self._wander_target)
        if arrived:
            new_x = self._wander_target
            self._wander_target = None
            self.state.play("idle")
            self._last_interaction = now
            self._remember_position()
        self.move(max(area.x(), min(new_x, area.right() - self.width())), self.y())
        return True

    def poll_look(self) -> None:
        """Turn the head towards the cursor when the pet is otherwise unoccupied."""
        if not self.config.get("look", True) or self.state.busy or self._dragging:
            return
        if self._wander_target is not None or self._fall_velocity is not None:
            return
        cursor = QCursor.pos()
        center = self.geometry().center()
        dx = cursor.x() - center.x()
        dy_up = center.y() - cursor.y()  # screen y grows downwards
        if abs(dx) < 4 and abs(dy_up) < 4:
            return
        name, frame_index = core.look_frame(dx, dy_up)
        if not self.library.has(name, self.state.persona):
            return
        if self.state.clip and self.state.clip.name == name and self.state.frame == frame_index:
            return
        if self.state.pin(name, frame_index):
            self.render()

    # ── input ────────────────────────────────────────
    def _log_event(self, what: str) -> None:
        """Append an interaction trace when config['debug'] is on.

        Real double-clicks can only be produced by a person, so this is the
        only way to see what the OS actually delivers.
        """
        if not self.config.get("debug"):
            return
        try:
            core.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(core.CONFIG_PATH.parent / "events.log", "a") as fh:
                fh.write("%.3f %-12s clip=%-14s held=%s swallow=%s\n" % (
                    time.perf_counter(), what,
                    self.state.clip.name if self.state.clip else None,
                    self.state.held, self._swallow_next_release))
        except OSError:
            pass

    def on_press(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            # The menu is opened on release, not here. QMenu.exec() takes a
            # mouse grab, so the right-button release that follows a press is
            # delivered to the open menu and dismissed it immediately. macOS
            # native menu tracking swallows that release; Windows does not, so
            # the menu only flashed and looked like nothing happened.
            self._log_event("right-press")
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_origin = event.globalPosition().toPoint()
        self._drag_window_origin = self.pos()
        self._last_drag_x = self._drag_origin.x()
        self._dragging = False
        self._press_time = time.perf_counter()
        # Remember what was showing before the click, so a following
        # double-click can anchor its cycle on it rather than on the wave.
        self._clip_at_press = self.state.clip.name if self.state.clip else None
        # A fresh press starts a new interaction. Clearing here matters because
        # the trailing release after a double-click does not always arrive, and
        # a stale flag would swallow the next genuine click.
        self._swallow_next_release = False
        self._wander_target = None
        self._fall_velocity = None
        self._log_event("press")

    def on_move(self, event) -> None:
        if self._drag_origin is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        current = event.globalPosition().toPoint()
        delta = current - self._drag_origin
        if abs(delta.x()) <= DRAG_THRESHOLD and abs(delta.y()) <= DRAG_THRESHOLD:
            return
        self._dragging = True
        self.move(self._drag_window_origin + delta)
        if current.x() > self._last_drag_x + 2:
            self.state.play("running-right")
        elif current.x() < self._last_drag_x - 2:
            self.state.play("running-left")
        self._last_drag_x = current.x()
        self.render()

    def on_release(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._log_event("right-release/menu")
            self.show_menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._last_interaction = time.perf_counter()
        # Qt delivers press, release, doubleClick, release. Without this the
        # trailing release counts as a single click and overwrites whatever the
        # double-click just picked.
        if self._swallow_next_release:
            self._swallow_next_release = False
            self._drag_origin = None
            self._log_event("release/swallowed")
            return
        if self._dragging:
            self._dragging = False
            self.state.play("idle")
            self._start_fall_if_airborne()
            self._remember_position()
        elif time.perf_counter() - self._press_time < CLICK_MAX_SECONDS:
            self.state.play("waving", temp=True)
        self._drag_origin = None
        self._log_event("release")
        self.render()

    def on_double_click(self, event) -> None:
        """Step to the next animation, so double-clicking browses through them."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._dragging = False
        self._drag_origin = None
        self._swallow_next_release = True
        order = self.library.names_for(self.state.persona)
        if not order:
            return
        # The cycle position is tracked separately instead of being read off the
        # current clip: the first click of a double-click already played the
        # wave, so using the live clip as the anchor made every double-click
        # land on the animation after "waving".
        if self._cycle_index is None:
            anchor = self._clip_at_press
            self._cycle_index = (order.index(anchor) + 1) % len(order) if anchor in order else 0
        else:
            self._cycle_index = (self._cycle_index + 1) % len(order)
        self._play_held(order[self._cycle_index])
        self._log_event("DOUBLE-CLICK")

    def _play_held(self, name: str) -> None:
        self._last_interaction = time.perf_counter()
        self._wander_target = None
        gait = _gait_of(name)
        if gait and self._start_traverse(*gait):
            return
        self.state.play(name, held=True)
        self.render()

    def _start_traverse(self, gait: str, direction: str) -> bool:
        """Walk or run to the edge of the screen instead of on the spot.

        Held playback keeps the pet still, so picking a gait from the menu ran
        the legs and went nowhere — a treadmill. Returns False when there is no
        room to go anywhere, in which case the caller holds the clip as before.
        """
        area = self._current_screen_geometry()
        edge = area.x() if direction == "left" else area.right() - self.width()
        if abs(edge - self.x()) < MIN_WANDER_DISTANCE:
            return False
        self.state.play(f"{gait}-{direction}")  # not held, so _step_wander drives it
        self._wander_gait = gait
        self._wander_carry = 0.0
        self._wander_target = edge
        self.render()
        return True

    def _start_fall_if_airborne(self) -> None:
        if not self.config.get("gravity", True):
            return
        floor = self._floor_y()
        if self.y() < floor - 4:
            self._fall_velocity = 0.0
            self.state.play("jumping")

    # ── menu ─────────────────────────────────────────
    def show_menu(self, position: QPoint) -> QMenu:
        menu = QMenu(self)

        # Order is deliberate: mode, props, size, then the actions.
        # 1st row — mode toggle, cycling to the next persona.
        personas = self.library.personas()
        if len(personas) > 1:
            nxt = personas[(personas.index(self.state.persona) + 1) % len(personas)]
            action = menu.addAction(PERSONA_LABEL.get(nxt, nxt.title()) + " Mode")
            action.triggered.connect(lambda _=False, p=nxt: self.set_persona(p))

        # 2nd row — props.
        for name in sorted(self.library.props):
            action = menu.addAction(("✓ " if name in self.active_props else "   ") + f"Prop: {name}")
            action.triggered.connect(lambda _=False, n=name: self.toggle_prop(n))

        # 3rd row — size.
        size_menu = menu.addMenu("Size")
        for value in SCALES:
            label = f"{int(value * 100)}%" + (" (default)" if value == 1.0 else "")
            item = size_menu.addAction(("● " if value == self.scale else "   ") + label)
            item.triggered.connect(lambda _=False, s=value: self.set_scale(s))

        menu.addSeparator()

        # 4th row onwards — the actions. Built straight from the clip library,
        # so a newly dropped animation folder appears here with no code change.
        for name in self.library.names_for(self.state.persona):
            clip = self.library.resolve(name, self.state.persona)
            if clip is None:
                continue
            mark = "● " if (self.state.clip and self.state.clip.name == name) else "   "
            tag = " *" if clip.persona != COMMON else ""
            action = menu.addAction(f"{mark}{name}{tag}")
            action.triggered.connect(lambda _=False, n=name: self.pick_animation(n))

        menu.addSeparator()
        release = menu.addAction("↩ Back to idle")
        release.triggered.connect(self.release_hold)

        options = menu.addMenu("Options")
        for key, label in (("wander", "Wander when idle"), ("look", "Watch the cursor"),
                           ("gravity", "Fall when dropped")):
            item = options.addAction(("✓ " if self.config.get(key, True) else "   ") + label)
            item.triggered.connect(lambda _=False, k=key: self.toggle_flag(key=k))
        item = options.addAction(("✓ " if self.config.get("click_through") else "   ") + "Click-through")
        item.triggered.connect(lambda _=False: self.set_click_through(not self.config.get("click_through", False)))

        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.exec(position)
        return menu

    # ── commands ─────────────────────────────────────
    def pick_animation(self, name: str) -> None:
        self._cycle_index = None  # an explicit pick restarts the double-click cycle
        self._play_held(name)

    def release_hold(self) -> None:
        self.state.play("idle")
        self._last_interaction = time.perf_counter()
        self.render()

    def set_persona(self, persona: str) -> None:
        self._cycle_index = None  # the animation list differs per persona
        self.state.set_persona(persona)
        self.config["persona"] = persona
        self._invalidate()
        self.render()
        self._save()

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self.config["scale"] = scale
        self._invalidate()
        self._apply_window_size()
        self.render()
        self._save()

    def toggle_prop(self, name: str) -> None:
        self.active_props.symmetric_difference_update({name})
        self.config["props"] = sorted(self.active_props)
        self._pixmaps.clear()
        self.render()
        self._save()

    def toggle_flag(self, key: str) -> None:
        self.config[key] = not self.config.get(key, True)
        if key == "wander":
            self._wander_target = None
        self._save()

    def set_click_through(self, enabled: bool) -> None:
        self.config["click_through"] = enabled
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)
        self._save()

    def _remember_position(self) -> None:
        self.config["pos"] = [self.x(), self.y()]
        self._save()

    def _save(self) -> None:
        core.save_config(self.config)

    # ── tray ─────────────────────────────────────────
    def install_tray(self) -> None:
        """Menu-bar / notification-area icon, so quitting never needs a right-click
        on the pet (which is impossible in click-through mode)."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        idle = self.library.resolve("idle", self.state.persona)
        icon = QIcon(to_pixmap(idle.frame(0))) if idle else QIcon()
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip(self.library.display_name)
        menu = QMenu()
        show = QAction("Show / Hide", menu)
        show.triggered.connect(lambda: self.setVisible(not self.isVisible()))
        menu.addAction(show)
        centre = QAction("Bring to centre", menu)
        centre.triggered.connect(self.centre_on_screen)
        menu.addAction(centre)
        through = QAction("Toggle click-through", menu)
        through.triggered.connect(lambda: self.set_click_through(not self.config.get("click_through", False)))
        menu.addAction(through)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def centre_on_screen(self) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.x() + (area.width() - self.width()) // 2,
                  area.y() + (area.height() - self.height()) // 2)
        self.setVisible(True)
        self._remember_position()


def run(argv: Optional[list] = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setQuitOnLastWindowClosed(False)
    library = ClipLibrary().load()
    for warning in library.warnings:
        print(f"  ! {warning}", file=sys.stderr)
    if not len(library):
        print("No animations found under assets/. Nothing to show.", file=sys.stderr)
        return 1

    window = PetWindow(library, core.load_config())
    window.show()
    window.render()
    window.install_tray()
    app.aboutToQuit.connect(window._remember_position)  # keep where you left it
    print(f"{library.display_name} 🐾  {len(library)} clips | "
          f"personas: {', '.join(library.personas())} | "
          f"click=wave  double-click=surprise  drag=move  right-click=menu")
    return app.exec()
