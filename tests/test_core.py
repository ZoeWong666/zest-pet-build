"""Tests for the Zest core. Run: python -m pytest tests/ -q

No GUI toolkit is needed here; qt_backend is covered by tests/test_qt_backend.py
which is skipped when PyQt6 is unavailable.
"""
from __future__ import annotations

import itertools
import json

import pytest

from zestpet import core
from zestpet.core import COMMON, Clip, ClipLibrary, Compositor, PetState, look_frame


@pytest.fixture(scope="module")
def lib() -> ClipLibrary:
    library = ClipLibrary().load()
    assert library.warnings == [], f"asset warnings: {library.warnings}"
    return library


# ── assets ──────────────────────────────────────────────
def test_assets_load(lib):
    assert len(lib) > 0
    assert lib.cell == (192, 208)


def test_every_clip_has_frames_of_one_size(lib):
    """Mixed frame sizes inside a clip would make the pet jitter."""
    for (persona, name) in [(c.persona, c.name) for c in lib._clips.values()]:
        clip = lib.resolve(name, persona)
        assert clip.frame_count > 0, f"{persona}/{name} has no frames"
        sizes = {f.size for f in clip.frames}
        assert len(sizes) == 1, f"{persona}/{name} has mixed frame sizes: {sizes}"


def test_every_frame_is_rgba_and_not_blank(lib):
    for clip in lib._clips.values():
        for i, frame in enumerate(clip.frames):
            assert frame.mode == "RGBA", f"{clip.persona}/{clip.name}[{i}] is {frame.mode}"
            assert frame.getbbox() is not None, f"{clip.persona}/{clip.name}[{i}] is fully blank"


def test_no_chroma_key_residue(lib):
    """The art is rendered on magenta. Any strongly magenta pixel left behind is
    key residue showing as a rim around the silhouette — the palette is black,
    tan, yellow, grey-blue and cream, so nothing here is legitimately magenta."""
    offenders = {}
    for clip in lib._clips.values():
        count = 0
        for frame in clip.frames:
            px = frame.load()
            w, h = frame.size
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    r, g, b, a = px[x, y]
                    if a > 128 and r > 110 and b > 110 and g < 70:
                        count += 1
        if count > 20:  # a handful of sampled pixels is dark fur, not a rim
            offenders[f"{clip.persona}/{clip.name}"] = count
    assert not offenders, f"chroma-key residue left: {offenders}"


def test_manifest_rows_all_loaded(lib):
    manifest = json.loads((core.asset_root() / "manifest.json").read_text())
    for row in manifest["rows"]:
        clip = lib.resolve(row["name"], COMMON)
        assert clip is not None, f"manifest row {row['name']} did not load"


def test_props_have_transparency(lib):
    """The prop art ships on a white background; it must be keyed out."""
    assert lib.props, "no props loaded"
    for name, prop in lib.props.items():
        assert prop.getpixel((0, 0))[3] == 0, f"prop {name} corner is opaque"


# ── persona resolution ──────────────────────────────────
def test_persona_override_and_fallback(lib):
    assert lib.resolve("idle", "evil").persona == "evil"
    # waiting has no evil variant, so it falls back to the shared clip
    assert lib.resolve("waiting", "evil").persona == COMMON


def test_persona_only_clips_are_not_reachable_from_normal(lib):
    for name in lib.persona_only("evil"):
        assert lib.resolve(name, COMMON) is None
        assert lib.resolve(name, "evil") is not None


def test_directory_clip_inherits_atlas_timing(lib):
    """An override folder should only need frames, not a repeat of the timing."""
    waiting = lib.resolve("waiting", COMMON)
    assert waiting.source == "dir"
    assert waiting.fps == 4  # from the manifest row, not from an anim.json


# ── frame pacing ────────────────────────────────────────
@pytest.mark.parametrize("tick_hz", [30, 60, 120])
def test_frame_pacing_is_even(lib, tick_hz):
    """Every frame must occupy the same number of ticks (+-1 for non-integer ratios).

    Repeated interval subtraction used to leave a float residue that held one
    frame for an extra tick and then advanced two frames at once.
    """
    state = PetState(library=lib)
    state.play("running-right")  # 10 fps
    state._now = 0.0
    now, seen = 0.0, []
    for _ in range(tick_hz * 2):
        now += 1.0 / tick_hz
        state.advance(now)
        seen.append(state.frame)
    runs = [len(list(group)) for _, group in itertools.groupby(seen)][1:-1]
    assert max(runs) - min(runs) <= 1, f"uneven frame pacing at {tick_hz}Hz: {sorted(set(runs))}"
    expected = tick_hz / 10
    assert abs(sum(runs) / len(runs) - expected) < 0.5


def test_exact_boundary_ticks_do_not_stutter(lib):
    state = PetState(library=lib)
    state.play("idle")  # 6 fps
    state._now = 0.0
    now, seen = 0.0, []
    for _ in range(18):
        now += 1 / 6
        state.advance(now)
        seen.append(state.frame)
    assert seen == [1, 2, 3, 4, 5, 0] * 3


def test_long_gap_does_not_fast_forward(lib):
    """Waking from sleep should not blast through hundreds of frames."""
    state = PetState(library=lib)
    state.play("idle")
    state.advance(state._now + 3600)
    assert state.frame <= 2


def test_non_looping_clip_holds_last_frame(lib):
    clip = Clip(name="once", frames=list(lib.resolve("idle", COMMON).frames), fps=10, loop=False)
    library = ClipLibrary()
    library._put(clip)
    state = PetState(library=library)
    state.play("once")
    state._now = 0.0
    now = 0.0
    for _ in range(200):  # advance() clamps each step, so step the clock properly
        now += 0.05
        state.advance(now)
    assert state.frame == clip.frame_count - 1


# ── state machine ───────────────────────────────────────
def test_temp_action_expires(lib):
    state = PetState(library=lib)
    state.play("waving", temp=True)  # duration 2.0
    assert state.busy and state.clip.name == "waving"
    state.advance(state._now + 1.9)
    assert state.clip.name == "waving"
    state.advance(state._now + 0.2)
    assert state.clip.name == "idle" and not state.busy


def test_held_action_never_expires(lib):
    state = PetState(library=lib)
    state.play("review", held=True)
    state.advance(state._now + 3600)
    assert state.clip.name == "review" and state.held


def test_pin_holds_one_frame_and_is_ambient(lib):
    """Looking must not mark the pet busy, or idle behaviour never runs."""
    state = PetState(library=lib)
    assert state.pin("look-A", 4)
    assert state.frame == 4 and state.pinned
    assert not state.busy, "pinned look should be ambient"
    state.advance(state._now + 1.0)
    assert state.frame == 4, "pinned frame must not advance"
    state.advance(state._now + 3.0)
    assert state.clip.name == "idle" and not state.pinned


def test_persona_switch_keeps_current_action_when_possible(lib):
    state = PetState(library=lib, persona="evil")
    state.play("idle")
    assert state.clip.persona == "evil"
    state.set_persona(COMMON)
    assert state.clip.persona == COMMON and state.clip.name == "idle"


def test_persona_switch_falls_back_when_action_is_persona_only(lib):
    state = PetState(library=lib, persona="evil")
    state.play("poop", temp=True)
    state.set_persona(COMMON)
    assert state.clip.name == "idle"


def test_unknown_clip_is_refused_not_crashed(lib):
    state = PetState(library=lib)
    state.play("idle")
    assert state.play("no-such-animation") is False
    assert state.clip.name == "idle"


def test_next_wake_stays_within_frame_interval(lib):
    state = PetState(library=lib)
    state.play("idle")
    for i in range(1, 40):
        state.advance(state._now + 0.017)
        assert 0 < state.next_wake <= state.clip.frame_interval + 1e-9


# ── look direction ──────────────────────────────────────
def test_look_clips_are_never_overridden(lib):
    """The look rows are 16 head orientations, not decoration.

    An evil/look-A folder used to shadow them with eight forward-facing
    expression frames, which silently broke cursor-watching in Evil mode: the
    pet changed expression instead of turning its head. Any persona override of
    these clips is a bug, so they must always resolve to the atlas.
    """
    for persona in lib.personas():
        for name in core.LOOK_CLIPS:
            clip = lib.resolve(name, persona)
            assert clip is not None, f"{persona} lost {name}"
            assert clip.source == "atlas", (
                f"{persona}/{name} is overridden by a folder; directional look "
                f"needs the atlas frames")
            assert clip.frame_count == core.LOOK_STEPS // len(core.LOOK_CLIPS)


def test_look_covers_sixteen_distinct_orientations():
    import math
    seen = set()
    for step in range(16):
        angle = math.radians(90 - step * 22.5)
        seen.add(look_frame(math.cos(angle) * 100, math.sin(angle) * 100))
    assert len(seen) == 16


def test_look_maps_cardinals_to_stable_frames():
    assert look_frame(0, 100) == ("look-A", 0)     # cursor above
    assert look_frame(100, 0) == ("look-A", 4)     # cursor right
    assert look_frame(0, -100) == ("look-B", 0)    # cursor below
    assert look_frame(-100, 0) == ("look-B", 4)    # cursor left


# ── compositing ─────────────────────────────────────────
def test_compositor_returns_base_frame_without_props(lib):
    clip = lib.resolve("idle", COMMON)
    assert Compositor(lib).render(clip, 0, frozenset(), lib.cell) is clip.frame(0)


def test_compositor_caches_per_frame(lib):
    comp = Compositor(lib)
    clip = lib.resolve("idle", COMMON)
    props = frozenset({"home"})
    first = comp.render(clip, 0, props, lib.cell)
    assert comp.render(clip, 0, props, lib.cell) is first, "identical request should hit cache"
    assert len(comp._cache) == 1
    for i in range(clip.frame_count):
        comp.render(clip, i, props, lib.cell)
    assert len(comp._cache) == clip.frame_count


def test_composite_keeps_pet_in_front(lib):
    """The prop is scenery: the pet must be drawn over it."""
    comp = Compositor(lib)
    clip = lib.resolve("idle", COMMON)
    composed = comp.render(clip, 0, frozenset({"home"}), lib.cell)
    base = clip.frame(0)
    w, h = lib.cell
    checked = 0
    for x in range(0, w, 7):
        for y in range(0, h, 7):
            if base.getpixel((x, y))[3] == 255:  # partial alpha legitimately blends
                assert composed.getpixel((x, y))[:3] == base.getpixel((x, y))[:3]
                checked += 1
    assert checked > 50


# ── config ──────────────────────────────────────────────
def test_config_roundtrip_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    monkeypatch.setattr(core, "CONFIG_PATH", target)
    core.save_config({"scale": 1.5, "persona": "evil"})
    assert core.load_config()["scale"] == 1.5
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"


def test_config_survives_corruption(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    target.write_text("{ this is not json")
    monkeypatch.setattr(core, "CONFIG_PATH", target)
    assert core.load_config() == core.DEFAULT_CONFIG


def test_config_survives_unwritable_location(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "nope" / "x" / "config.json")
    monkeypatch.setattr(core.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    core.save_config({"scale": 1.0})  # must not raise


# ── parity with the pre-refactor build ──────────────────
LEGACY_ATLAS_STATES = [
    "idle", "running-right", "running-left", "waving", "jumping",
    "failed", "waiting", "running", "review", "look-A", "look-B",
]
# "look-A" was in this set, but the evil folder held forward-facing expression
# frames rather than head orientations, which broke cursor-watching in Evil
# mode. That art is now the separate "taunt" clip and look-A falls back to the
# atlas. See test_look_clips_are_never_overridden.
LEGACY_EVIL_OVERRIDES = {"idle", "running-right", "running-left"}
LEGACY_EVIL_SPECIALS = {"angry", "grin", "smirk", "poop"}
# The "failed" override was dropped: it had 7 of 8 frames, a 28px jump
# between frames 0 and 1, and a magenta artefact. The atlas row is clean.
LEGACY_DIR_OVERRIDES = {"waiting"}
LEGACY_SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
LEGACY_TEMP_DURATIONS = {
    "waving": 2.0, "jumping": 1.5, "failed": 2.5,
    "running-right": 0.8, "running-left": 0.8,
    "look-A": 3.0, "look-B": 3.0,
    "angry": 4.0, "grin": 4.0, "smirk": 4.0, "poop": 5.0,
}
LEGACY_DYNAMIC_CLIPS = {"angry", "grin", "smirk"}


def test_parity_all_legacy_states_present(lib):
    for name in LEGACY_ATLAS_STATES:
        assert lib.resolve(name, COMMON) is not None, f"lost legacy state {name}"


def test_parity_evil_overrides_present(lib):
    for name in LEGACY_EVIL_OVERRIDES:
        clip = lib.resolve(name, "evil")
        assert clip.persona == "evil", f"evil no longer overrides {name}"


def test_evil_expression_frames_kept_as_their_own_clip(lib):
    """The art that used to shadow look-A is still reachable, just renamed."""
    taunt = lib.resolve("taunt", "evil")
    assert taunt is not None and taunt.frame_count == 8
    assert lib.resolve("taunt", COMMON) is None


def test_parity_evil_specials_present(lib):
    """The old evil-only clips must all survive. New ones may be added, so this
    checks containment rather than equality."""
    assert LEGACY_EVIL_SPECIALS <= set(lib.persona_only("evil"))


def test_parity_directory_overrides_win_over_atlas(lib):
    for name in LEGACY_DIR_OVERRIDES:
        assert lib.resolve(name, COMMON).source == "dir", f"{name} no longer overridden by folder"


def test_parity_temp_durations_unchanged(lib):
    for name, expected in LEGACY_TEMP_DURATIONS.items():
        for persona in (COMMON, "evil"):
            clip = lib.resolve(name, persona)
            if clip is None:
                continue
            assert clip.duration == expected, f"{persona}/{name} duration {clip.duration} != {expected}"


def test_parity_dynamic_clips_keep_own_size(lib):
    for name in LEGACY_DYNAMIC_CLIPS:
        clip = lib.resolve(name, "evil")
        assert clip.dynamic, f"{name} lost its dynamic sizing"
        assert clip.size != lib.cell


def test_parity_scale_steps_unchanged():
    from zestpet import qt_backend
    assert qt_backend.SCALES == LEGACY_SCALES


def test_parity_evil_idle_padded_to_cell(lib):
    """Legacy loaded evil idle with pad_to_cell; narrow art must still fill the cell."""
    assert lib.resolve("idle", "evil").size == lib.cell


# ── extensibility: the whole point of the refactor ──────
def test_new_animation_folder_is_discovered(tmp_path):
    """Dropping a folder of PNGs in must be enough — no code, no manifest edit."""
    root = tmp_path / "assets"
    (root / "anim" / "common" / "backflip").mkdir(parents=True)
    from PIL import Image
    for i in range(3):
        Image.new("RGBA", (192, 208), (i * 40, 0, 0, 255)).save(
            root / "anim" / "common" / "backflip" / f"{i:02d}.png")
    (root / "manifest.json").write_text(json.dumps({"cell": [192, 208]}))

    library = ClipLibrary(root).load()
    clip = library.resolve("backflip", COMMON)
    assert clip is not None and clip.frame_count == 3
    assert "backflip" in library.names_for(COMMON)


def test_new_animation_options_are_honoured(tmp_path):
    root = tmp_path / "assets"
    folder = root / "anim" / "evil" / "cackle"
    folder.mkdir(parents=True)
    from PIL import Image
    for i in range(4):
        Image.new("RGBA", (100, 208), (0, 0, i * 50, 255)).save(folder / f"{i:02d}.png")
    (folder / "anim.json").write_text(json.dumps(
        {"fps": 12, "duration": 3.5, "loop": False, "pad": True, "dynamic": False}))
    (root / "manifest.json").write_text(json.dumps({"cell": [192, 208]}))

    clip = ClipLibrary(root).load().resolve("cackle", "evil")
    assert (clip.fps, clip.duration, clip.loop) == (12, 3.5, False)
    assert clip.size == (192, 208), "pad option should letterbox into the cell"


def test_scale_option_resizes_frames(tmp_path):
    """Art from a different render can arrive at the wrong scale; a factor in
    anim.json fixes it without re-cutting the source frames."""
    root = tmp_path / "assets"
    folder = root / "anim" / "common" / "big"
    folder.mkdir(parents=True)
    from PIL import Image
    for i in range(2):
        Image.new("RGBA", (200, 300), (10, 20, 30, 255)).save(folder / f"{i:02d}.png")
    (folder / "anim.json").write_text(json.dumps({"scale": 0.5}))
    (root / "manifest.json").write_text(json.dumps({"cell": [192, 208]}))

    clip = ClipLibrary(root).load().resolve("big", COMMON)
    assert clip.size == (100, 150)
    assert all(f.size == (100, 150) for f in clip.frames)


def test_imported_clips_match_the_atlas_dog_size(lib):
    """The dog must read as the same size in every clip.

    Measured ear-tip to paw, with the column range narrowed to skip the hand in
    head-pat and the pant leg in rub-leg. Both strips came from a render at a
    different scale: head-pat was 1.18x and rub-leg 1.07x before correction.
    """
    def dog_height(clip, frame_index, left_fraction):
        frame = clip.frames[frame_index]
        px = frame.load()
        w, h = frame.size
        x0 = int(w * left_fraction)
        top = next(y for y in range(h) if any(px[x, y][3] > 128 for x in range(x0, w)))
        bottom = max(y for y in range(h) if any(px[x, y][3] > 128 for x in range(w)))
        return bottom - top + 1

    reference = dog_height(lib.resolve("idle", COMMON), 0, 0.0)
    for name, persona, left in (("head-pat", COMMON, 0.62), ("rub-leg", "evil", 0.45)):
        clip = lib.resolve(name, persona)
        if clip is None:
            continue
        ratio = dog_height(clip, 0, left) / reference
        assert 0.9 <= ratio <= 1.1, f"{name} dog is {ratio:.2f}x the atlas dog"


def test_bad_anim_json_is_reported_not_fatal(tmp_path):
    root = tmp_path / "assets"
    folder = root / "anim" / "common" / "broken"
    folder.mkdir(parents=True)
    from PIL import Image
    Image.new("RGBA", (192, 208), (1, 2, 3, 255)).save(folder / "00.png")
    (folder / "anim.json").write_text("{ nope")
    (root / "manifest.json").write_text(json.dumps({"cell": [192, 208]}))

    library = ClipLibrary(root).load()
    assert library.resolve("broken", COMMON) is not None
    assert any("broken" in w for w in library.warnings)


def test_missing_assets_reported_not_crashed(tmp_path):
    library = ClipLibrary(tmp_path / "does-not-exist").load()
    assert len(library) == 0
    assert library.warnings
