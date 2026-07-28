"""Qt front-end tests. Run headless via QT_QPA_PLATFORM=offscreen.

Skipped entirely when PyQt6 is not installed.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from zestpet import core  # noqa: E402
from zestpet.core import COMMON, ClipLibrary  # noqa: E402
from zestpet.qt_backend import SCALES, PetWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def library():
    return ClipLibrary().load()


@pytest.fixture
def window(app, library, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    win = PetWindow(library, core.load_config())
    win.show()
    win.render()
    yield win
    win.close()


class FakeMouseEvent:
    """Minimal stand-in for QMouseEvent (constructing real ones is awkward)."""

    def __init__(self, button=Qt.MouseButton.LeftButton, x=0, y=0, buttons=None):
        self._button = button
        self._pos = QPointF(x, y)
        self._buttons = buttons if buttons is not None else button

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def globalPosition(self):
        return self._pos


def pixmap_size(win):
    pm = win.label.pixmap()
    return pm.width(), pm.height()


# ── window ──────────────────────────────────────────────
def test_window_is_frameless_translucent_and_on_top(window):
    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


def test_window_has_no_drop_shadow(window):
    """A drop shadow on a frameless window reads as a ghost box behind the pet."""
    assert window.windowFlags() & Qt.WindowType.NoDropShadowWindowHint
    assert window.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
    assert not window.autoFillBackground()


def test_window_kind_avoids_the_macos_panel_auto_hide(window):
    """On macOS a Qt.Tool window is an NSPanel, which the system hides when the
    app deactivates — that made the pet vanish when clicking the desktop."""
    import sys as _sys
    from zestpet.qt_backend import PET_WINDOW_KIND
    # Qt window types are not independent bits (Tool contains the Window bit),
    # so the type has to be compared through WindowType_Mask.
    kind = window.windowFlags() & Qt.WindowType.WindowType_Mask
    if _sys.platform == "darwin":
        assert PET_WINDOW_KIND == Qt.WindowType.Window
        assert kind == Qt.WindowType.Window
        assert kind != Qt.WindowType.Tool
    else:
        assert PET_WINDOW_KIND == Qt.WindowType.Tool
        assert kind == Qt.WindowType.Tool


def test_first_run_stands_on_the_desktop_floor(library, tmp_path, monkeypatch, app):
    from PyQt6.QtGui import QGuiApplication
    from zestpet.qt_backend import FLOOR_MARGIN
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "fresh.json")
    win = PetWindow(library, core.load_config())
    area = QGuiApplication.primaryScreen().availableGeometry()
    assert win.y() == area.bottom() - win.height() - FLOOR_MARGIN
    win.close()


def test_window_matches_cell_size_at_100_percent(window, library):
    assert (window.width(), window.height()) == library.cell
    assert pixmap_size(window) == library.cell


@pytest.mark.parametrize("scale", SCALES)
def test_every_scale_step_resizes_window_and_pixmap(window, library, scale):
    window.set_scale(scale)
    window.render()
    expected = (int(library.cell[0] * scale), int(library.cell[1] * scale))
    assert (window.width(), window.height()) == expected
    assert pixmap_size(window) == expected


def test_dynamic_clip_resizes_window_and_normal_clip_restores(window, library):
    window.set_persona("evil")
    window.pick_animation("angry")
    window.render()
    angry = library.resolve("angry", "evil")
    assert (window.width(), window.height()) == angry.size
    window.pick_animation("idle")
    window.render()
    assert (window.width(), window.height()) == library.cell


def test_dynamic_resize_keeps_feet_planted(window, library):
    window.move(400, 400)
    bottom_before = window.y() + window.height()
    window.set_persona("evil")
    window.pick_animation("angry")
    window.render()
    assert window.y() + window.height() == bottom_before


# ── rendering ───────────────────────────────────────────
def test_pixmap_cache_reuses_entries(window):
    window._invalidate()
    for _ in range(300):
        window.tick()
    clip = window.state.clip
    assert len(window._pixmaps) <= clip.frame_count, "cache should not grow per tick"


def test_prop_toggle_changes_render_and_persists(window):
    window.toggle_prop("home")
    window.render()
    assert "home" in window.active_props
    assert core.load_config()["props"] == ["home"]
    window.toggle_prop("home")
    assert core.load_config()["props"] == []


def test_persona_toggle_switches_clip_source(window):
    window.set_persona("evil")
    assert window.state.clip.persona == "evil"
    window.set_persona(COMMON)
    assert window.state.clip.persona == COMMON


def test_settings_persist_across_restart(window, library):
    window.set_scale(1.5)
    window.set_persona("evil")
    window.toggle_prop("home")
    reopened = PetWindow(library, core.load_config())
    assert reopened.scale == 1.5
    assert reopened.state.persona == "evil"
    assert reopened.active_props == {"home"}
    reopened.close()


def test_unknown_persona_in_config_falls_back(library, tmp_path, monkeypatch, app):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "config.json")
    core.save_config({"persona": "does-not-exist"})
    win = PetWindow(library, core.load_config())
    assert win.state.persona == COMMON
    win.close()


# ── interaction ─────────────────────────────────────────
def test_single_click_waves(window):
    window.on_press(FakeMouseEvent())
    window.on_release(FakeMouseEvent())
    assert window.state.clip.name == "waving"


def test_double_click_survives_the_trailing_release(window):
    """Qt delivers press, release, doubleClick, release. The trailing release
    used to be treated as a single click and overwrote the double-click action
    with a wave, which made the feature look broken."""
    window.on_press(FakeMouseEvent())
    window.on_release(FakeMouseEvent())
    window.on_press(FakeMouseEvent())
    window.on_release(FakeMouseEvent())
    window.on_double_click(FakeMouseEvent())
    picked = window.state.clip.name
    window.on_release(FakeMouseEvent())  # the trailing release
    assert window.state.clip.name == picked, "trailing release clobbered the double-click"
    assert window.state.clip.name != "waving" or picked == "waving"


def test_single_click_after_double_click_still_waves(window):
    """The release-swallowing flag must not leak into the next interaction."""
    window.on_double_click(FakeMouseEvent())          # sets the swallow flag
    window.on_press(FakeMouseEvent())                 # new interaction clears it
    window.on_release(FakeMouseEvent())
    assert window.state.clip.name == "waving"


def test_double_click_cycles_under_the_real_event_sequence(window, library):
    """Replays what macOS actually delivered (captured from a real session):

        press / release(-> waving) / doubleClick / release(swallowed)

    The first release plays the wave, so anchoring the cycle on the live clip
    made every double-click land on the animation after "waving" — six real
    double-clicks produced "jumping" six times.
    """
    order = library.names_for(window.state.persona)
    window.state.play("idle")
    window._cycle_index = None
    seen = []
    for _ in range(len(order)):
        window.on_press(FakeMouseEvent())
        window.on_release(FakeMouseEvent())        # plays waving
        window.on_double_click(FakeMouseEvent())
        window.on_release(FakeMouseEvent())        # trailing, swallowed
        seen.append(window.state.clip.name)
    assert len(set(seen)) == len(order), f"double-click got stuck: {seen}"
    assert seen == order[1:] + order[:1], f"expected menu order, got {seen}"


def test_menu_pick_restarts_the_double_click_cycle(window, library):
    order = library.names_for(window.state.persona)
    window.pick_animation("review")
    assert window._cycle_index is None
    window.on_press(FakeMouseEvent())
    window.on_double_click(FakeMouseEvent())
    expected = order[(order.index("review") + 1) % len(order)]
    assert window.state.clip.name == expected


def test_double_click_cycle_includes_persona_only_clips(window, library):
    window.set_persona("evil")
    order = library.names_for("evil")
    seen = set()
    for _ in range(len(order) * 2):
        window.on_double_click(FakeMouseEvent())
        seen.add(window.state.clip.name)
    assert set(library.persona_only("evil")) <= seen


def test_double_click_holds_the_animation(window):
    window.on_double_click(FakeMouseEvent())
    name = window.state.clip.name
    for _ in range(400):
        window.tick()
    assert window.state.clip.name == name, "double-clicked animation should stay put"


def test_drag_moves_window_and_plays_direction(window):
    start = window.pos()
    window.on_press(FakeMouseEvent(x=0, y=0))
    window.on_move(FakeMouseEvent(x=60, y=0))
    assert window.state.clip.name == "running-right"
    assert window.pos().x() == start.x() + 60
    window.on_move(FakeMouseEvent(x=10, y=0))
    assert window.state.clip.name == "running-left"


def test_tiny_movement_is_a_click_not_a_drag(window):
    window.on_press(FakeMouseEvent(x=0, y=0))
    window.on_move(FakeMouseEvent(x=2, y=2))
    assert window._dragging is False
    window.on_release(FakeMouseEvent())
    assert window.state.clip.name == "waving"


def test_drag_release_returns_to_idle_when_on_the_floor(window):
    area = window._current_screen_geometry()
    window.move(500, area.bottom() - window.height() - 8)
    window.on_press(FakeMouseEvent(x=0, y=0))
    window.on_move(FakeMouseEvent(x=40, y=0))
    window.on_release(FakeMouseEvent())
    assert window._fall_velocity is None
    assert window.state.clip.name == "idle"


def test_drop_from_height_falls_to_the_floor(window):
    window.config["gravity"] = True
    window.move(500, 60)
    window.on_press(FakeMouseEvent(x=0, y=0))
    window.on_move(FakeMouseEvent(x=40, y=0))
    window.on_release(FakeMouseEvent())
    assert window._fall_velocity is not None
    assert window.state.clip.name == "jumping"
    for _ in range(600):
        window.tick()
        if window._fall_velocity is None:
            break
    floor = window._current_screen_geometry().bottom() - window.height() - 8
    assert window.y() == floor
    assert window.state.clip.name == "idle"


def test_gravity_can_be_disabled(window):
    window.config["gravity"] = False
    window.move(500, 60)
    window.on_press(FakeMouseEvent(x=0, y=0))
    window.on_move(FakeMouseEvent(x=40, y=0))
    window.on_release(FakeMouseEvent())
    assert window._fall_velocity is None
    assert window.y() == 60


# ── autonomous behaviour ────────────────────────────────
def test_wander_starts_only_after_a_quiet_spell(window):
    from zestpet.qt_backend import IDLE_BEFORE_WANDER
    window.config["wander"] = True
    window._last_interaction = __import__("time").perf_counter()
    assert window._step_wander() is False, "should not wander right after interaction"
    window._last_interaction -= IDLE_BEFORE_WANDER + 1
    start_x = window.x()
    moved = False
    for _ in range(400):
        window.tick()
        if window.x() != start_x:
            moved = True
            break
    assert moved, "pet never wandered"
    assert window.state.clip.name in ("running-left", "running-right")


def test_wander_respects_the_disable_flag(window):
    from zestpet.qt_backend import IDLE_BEFORE_WANDER
    window.config["wander"] = False
    window._last_interaction -= IDLE_BEFORE_WANDER + 1
    start_x = window.x()
    for _ in range(200):
        window.tick()
    assert window.x() == start_x


def test_wander_does_not_interrupt_a_held_animation(window):
    from zestpet.qt_backend import IDLE_BEFORE_WANDER
    window.config["wander"] = True
    window.pick_animation("review")
    window._last_interaction -= IDLE_BEFORE_WANDER + 1
    start_x = window.x()
    for _ in range(200):
        window.tick()
    assert window.state.clip.name == "review"
    assert window.x() == start_x


def test_wander_stays_on_screen(window):
    from zestpet.qt_backend import IDLE_BEFORE_WANDER
    area = window._current_screen_geometry()
    window.config["wander"] = True
    for _ in range(6):
        window._last_interaction -= IDLE_BEFORE_WANDER + 1
        for _ in range(400):
            window.tick()
        assert area.x() <= window.x() <= area.right() - window.width()


def test_look_pins_a_direction_without_blocking_idle_behaviour(window):
    window.config["look"] = True
    window.poll_look()
    assert window.state.clip.name in ("look-A", "look-B")
    assert window.state.pinned
    assert not window.state.busy, "looking must stay ambient"


def test_look_can_be_disabled(window):
    window.config["look"] = False
    window.state.play("idle")
    window.poll_look()
    assert window.state.clip.name == "idle"


def test_look_does_not_interrupt_a_deliberate_action(window):
    window.config["look"] = True
    window.state.play("waving", temp=True)
    window.poll_look()
    assert window.state.clip.name == "waving"


# ── menu ────────────────────────────────────────────────
def test_menu_row_order_is_mode_props_size_then_actions(window, library, monkeypatch):
    """Fixed layout: 1st row mode, 2nd props, 3rd size, 4th row onwards actions."""
    captured = []
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu.exec", lambda self, *a: captured.append(self))
    window.set_persona(COMMON)
    window.show_menu(QPoint(0, 0))
    rows = [a for a in captured[0].actions() if not a.isSeparator()]
    assert "Mode" in rows[0].text()
    assert "Prop:" in rows[1].text()
    assert rows[2].text() == "Size" and rows[2].menu() is not None
    action_names = library.names_for(COMMON)
    for offset, name in enumerate(action_names):
        assert name in rows[3 + offset].text(), f"row {4 + offset} should be action {name}"


def test_animations_are_top_level_not_nested(window, library, monkeypatch):
    """Switching animation is the most used action, so it must not be buried."""
    captured = []
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu.exec", lambda self, *a: captured.append(self))
    window.show_menu(QPoint(0, 0))
    top_level = [a.text() for a in captured[0].actions() if a.menu() is None]
    for name in library.names_for(window.state.persona):
        assert any(name in text for text in top_level), f"{name} is not in the top-level menu"
    submenus = [a.text() for a in captured[0].actions() if a.menu() is not None]
    assert len(submenus) <= 2, f"too many nested submenus: {submenus}"


def test_menu_lists_every_discovered_animation(window, library, monkeypatch):
    captured = []
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu.exec", lambda self, *a: captured.append(self))
    window.show_menu(QPoint(0, 0))
    assert captured, "menu was never shown"
    labels = _all_action_texts(captured[0])
    for name in library.names_for(window.state.persona):
        assert any(name in text for text in labels), f"{name} missing from menu"
    assert any("Quit" in text for text in labels)


def test_menu_exposes_evil_specials_only_in_evil_mode(window, library, monkeypatch):
    captured = []
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu.exec", lambda self, *a: captured.append(self))
    window.set_persona(COMMON)
    window.show_menu(QPoint(0, 0))
    normal_labels = " ".join(_all_action_texts(captured[-1]))
    window.set_persona("evil")
    window.show_menu(QPoint(0, 0))
    evil_labels = " ".join(_all_action_texts(captured[-1]))
    for special in library.persona_only("evil"):
        assert special not in normal_labels
        assert special in evil_labels


def test_menu_has_all_scale_steps(window, monkeypatch):
    captured = []
    monkeypatch.setattr("PyQt6.QtWidgets.QMenu.exec", lambda self, *a: captured.append(self))
    window.show_menu(QPoint(0, 0))
    labels = " ".join(_all_action_texts(captured[0]))
    for scale in SCALES:
        assert f"{int(scale * 100)}%" in labels


def _all_action_texts(menu):
    texts = []
    for action in menu.actions():
        texts.append(action.text())
        if action.menu() is not None:
            texts.extend(_all_action_texts(action.menu()))
    return texts


# ── click-through ───────────────────────────────────────
def test_click_through_toggles_mouse_transparency(window):
    window.set_click_through(True)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert core.load_config()["click_through"] is True
    window.set_click_through(False)
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


# ── adaptive timer ──────────────────────────────────────
def test_timer_slows_down_when_nothing_moves(window):
    window.config["wander"] = False
    window.state.play("idle")  # 6 fps
    for _ in range(10):
        window.tick()
    assert window._timer.interval() > 16, "timer should back off when idle"


def test_timer_runs_fast_while_falling(window):
    window.config["gravity"] = True
    window.move(500, 60)
    window._fall_velocity = 0.0
    window.tick()
    assert window._timer.interval() == 16
