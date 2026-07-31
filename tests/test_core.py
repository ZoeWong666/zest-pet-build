"""Tests for the Zest core. Run: python -m pytest tests/ -q

No GUI toolkit is needed here; qt_backend is covered by tests/test_qt_backend.py
which is skipped when PyQt6 is unavailable.
"""
from __future__ import annotations

import itertools
import json
import statistics
from pathlib import Path

import pytest
from PIL import Image

from zestpet import core
from zestpet.core import COMMON, Clip, ClipLibrary, Compositor, PetState, look_frame


@pytest.fixture(scope="module")
def lib() -> ClipLibrary:
    library = ClipLibrary().load()
    assert library.warnings == [], f"asset warnings: {library.warnings}"
    return library


# ── throwaway asset trees ───────────────────────────────
# Several tests need a synthetic assets/ dir rather than the shipped art. They
# used to build it inline, four near-identical copies of the same mkdir / save /
# write-manifest dance, so a change to the layout meant editing all four.
def asset_tree(tmp_path: Path, cell=(192, 208)) -> Path:
    """An empty assets/ with a minimal manifest. Add clips with write_clip."""
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"cell": list(cell)}))
    return root


def write_clip(root: Path, persona: str, name: str, frames: int = 2,
               size=(192, 208), options=None) -> Path:
    """Write a clip folder of flat-colour PNGs. ``options`` becomes anim.json —
    pass a dict for valid JSON, or a raw string to test malformed input."""
    folder = root / "anim" / persona / name
    folder.mkdir(parents=True)
    for i in range(frames):
        Image.new("RGBA", size, ((i * 40) % 256, 0, 0, 255)).save(folder / f"{i:02d}.png")
    if options is not None:
        (folder / "anim.json").write_text(
            options if isinstance(options, str) else json.dumps(options))
    return folder


# ── silhouette rulers ───────────────────────────────────
# Shared by the dog-size checks below. Each takes a clip and returns one linear
# measurement, so they are interchangeable in the parametrised test. They all
# read the alpha channel, so the scan itself lives in one place.
def _opaque_columns(frame: Image.Image, y: int, x_window=(0.0, 1.0)):
    px = frame.load()
    width = frame.size[0]
    x0, x1 = int(width * x_window[0]), int(width * x_window[1])
    return [x for x in range(x0, x1) if px[x, y][3] > 128]


def ear_tip_to_paw(clip, x_window=(0.0, 1.0)) -> float:
    """Artwork height on the first frame. The top is read inside x_window so a
    hand or a pant leg reaching above the dog does not count; the paw line is
    read full-width."""
    frame = clip.frames[0]
    height = frame.size[1]
    top = next(y for y in range(height) if _opaque_columns(frame, y, x_window))
    bottom = max(y for y in range(height) if _opaque_columns(frame, y))
    return bottom - top + 1


def ear_tip_to_ear_tip(clip, x_window=(0.0, 1.0), band_fraction: float = 0.42) -> float:
    """Head width on the first frame: the widest row in the top slice.

    Only meaningful for a dog facing the camera. Later frames are not usable on
    head-pat — the forearm swings out sideways, where it would be mistaken for
    the widest part.
    """
    frame = clip.frames[0]
    _, top, _, bottom = frame.getbbox()
    band = top + max(1, int((bottom - top) * band_fraction))
    spans = [_opaque_columns(frame, y, x_window) for y in range(top, band)]
    return max((xs[-1] - xs[0] + 1 for xs in spans if xs), default=0)


def silhouette_scale(clip, x_window=(0.0, 1.0)) -> float:
    """Linear size taken from the drawn area, median over the clip.

    Square-rooted because area grows with the square of the size. Unlike the
    other two rulers this does not care which way the dog faces, so it is the
    one that works on the running clips, where there is no ear-to-ear line to
    measure. It counts every opaque pixel, so it is only valid where nothing but
    the dog is drawn — a hand or a pant leg would be measured as dog.
    """
    areas = []
    for frame in clip.frames:
        areas.append(sum(len(_opaque_columns(frame, y, x_window))
                         for y in range(frame.size[1])))
    return statistics.median(areas) ** 0.5


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
    """No source may skip the residue cleanup.

    The art is rendered on magenta and every loader path has to run the frames
    through drop_key_residue. The risk this guards is a missing call — a new
    source, or a step inserted after the cleanup that resamples the key colour
    back in — so it asks for zero, not for a small number.
    """
    offenders = {}
    for clip in lib._clips.values():
        count = 0
        for frame in clip.frames:
            px = frame.load()
            w, h = frame.size
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    r, g, b, a = px[x, y]
                    if (a > 64 and r - g > core.MAGENTA_OVER_GREEN
                            and b - g > core.MAGENTA_OVER_GREEN):
                        count += 1
        if count:
            offenders[f"{clip.persona}/{clip.name}"] = count
    assert not offenders, f"chroma-key residue left: {offenders}"


def test_key_residue_mask_is_calibrated():
    """The cut between residue and art, pinned against real pixel values.

    Residue arrives at every brightness because the rim is magenta diluted by
    whatever it borders. Testing against an absolute level missed the darker end
    of that range: the samples here are measured from imported frames, and the
    (105, 5, 107) one survived the earlier threshold of 110 and showed as a
    visible purple fringe.
    """
    palette = {
        "black fur": (30, 25, 20),
        "tan": (200, 140, 60),
        "yellow bandana": (255, 210, 60),
        "cream muzzle": (245, 235, 210),
        "grey-blue": (100, 130, 160),
        "neutral grey": (100, 95, 100),
        "cool shadow on black": (60, 60, 70),
    }
    residue = {
        "bright rim": (216, 3, 202),
        "mid rim": (115, 34, 110),
        "dark rim": (105, 5, 107),
        "darker rim": (77, 5, 67),
    }

    def surviving_alpha(rgb):
        img = Image.new("RGBA", (4, 4), rgb + (255,))
        return core.drop_key_residue(img).getpixel((2, 2))[3]

    for name, rgb in palette.items():
        assert surviving_alpha(rgb) == 255, f"{name} {rgb} was treated as residue"
    for name, rgb in residue.items():
        assert surviving_alpha(rgb) == 0, f"{name} {rgb} survived the cleanup"


def test_residue_cleanup_only_takes_a_rim(lib):
    """Cleanup must shave the edge, not bite into the dog.

    Compared against the frames as they sit on disk, since those are pre-cleanup.
    """
    for name, persona in (("walking-right", "evil"), ("running-right", "evil"),
                          ("idle", "evil")):
        clip = lib.resolve(name, persona)
        if clip is None or clip.source != "dir":
            continue
        folder = core.asset_root() / "anim" / persona / name
        raw = [Image.open(p).convert("RGBA") for p in sorted(folder.glob("*.png"))]
        if len(raw) != clip.frame_count:
            continue

        def opaque(img):
            return sum(1 for p in img.getdata() if p[3] > 128)

        before = sum(opaque(f) for f in raw)
        after = sum(opaque(f) for f in clip.frames)
        lost = (before - after) / before
        assert 0 <= lost < 0.08, f"{persona}/{name} lost {lost:.1%} of its artwork"


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


def test_only_one_platform_difference_exists():
    """Windows and macOS must behave identically apart from the window type.

    macOS needs Qt.Window because a Qt.Tool window is an NSPanel and the system
    hides panels when the app deactivates. Everything else — animations, input,
    menu, behaviour — runs the same code on both. Any new platform branch should
    be a deliberate decision, so this test makes one show up as a failure.
    """
    import re
    from pathlib import Path
    source_root = Path(core.__file__).parent
    branches = []
    for path in sorted(source_root.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"sys\.platform|platform\.(system|machine)|os\.name", line):
                if line.lstrip().startswith("#"):
                    continue
                branches.append(f"{path.name}:{number}: {line.strip()}")
    assert len(branches) == 1, "unexpected platform-specific code:\n" + "\n".join(branches)
    assert "PET_WINDOW_KIND" in branches[0]


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
# mode. That art is now the separate "head-tilt" clip and look-A falls back to the
# atlas. See test_look_clips_are_never_overridden.
LEGACY_EVIL_OVERRIDES = {"idle", "running-right", "running-left"}
LEGACY_EVIL_SPECIALS = {"angry", "grin", "smirk", "poop"}
# The "failed" override was dropped: it had 7 of 8 frames, a 28px jump
# between frames 0 and 1, and a magenta artefact. The atlas row is clean.
LEGACY_DIR_OVERRIDES = {"waiting"}
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
    """The art that used to shadow look-A is still reachable, just renamed.

    Named for what it shows — a head tilt with a smirk — rather than something
    interpretive, so nobody has to open the folder to find out.
    """
    tilt = lib.resolve("head-tilt", "evil")
    assert tilt is not None and tilt.frame_count == 8
    assert lib.resolve("head-tilt", COMMON) is None


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


def test_parity_evil_idle_padded_to_cell(lib):
    """Legacy loaded evil idle with pad_to_cell; narrow art must still fill the cell."""
    assert lib.resolve("idle", "evil").size == lib.cell


# ── extensibility: the whole point of the refactor ──────
def test_new_animation_folder_is_discovered(tmp_path):
    """Dropping a folder of PNGs in must be enough — no code, no manifest edit."""
    root = asset_tree(tmp_path)
    write_clip(root, COMMON, "backflip", frames=3)

    library = ClipLibrary(root).load()
    clip = library.resolve("backflip", COMMON)
    assert clip is not None and clip.frame_count == 3
    assert "backflip" in library.names_for(COMMON)


def test_new_animation_options_are_honoured(tmp_path):
    root = asset_tree(tmp_path)
    write_clip(root, "evil", "cackle", frames=4, size=(100, 208), options={
        "fps": 12, "duration": 3.5, "loop": False, "pad": True, "dynamic": False})

    clip = ClipLibrary(root).load().resolve("cackle", "evil")
    assert (clip.fps, clip.duration, clip.loop) == (12, 3.5, False)
    assert clip.size == (192, 208), "pad option should letterbox into the cell"


def test_scale_option_shrinks_the_dog_inside_the_cell(tmp_path):
    """Art from a different render can arrive at the wrong scale; a factor in
    anim.json fixes it without re-cutting the source frames.

    Without ``dynamic`` the frame has to stay cell-shaped, so the canvas is kept
    and only the drawing inside it shrinks. See the dynamic case below.
    """
    root = asset_tree(tmp_path)
    folder = write_clip(root, COMMON, "big", frames=2, size=(200, 300),
                        options={"scale": 0.5})
    # a solid block does not reach the canvas edges, so the shrink is visible
    for path in sorted(folder.glob("*.png")):
        canvas = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
        canvas.paste(Image.new("RGBA", (100, 100), (9, 9, 9, 255)), (50, 150))
        canvas.save(path)

    clip = ClipLibrary(root).load().resolve("big", COMMON)
    assert clip.size == (200, 300), "canvas must survive"
    box = clip.frames[0].getchannel("A").getbbox()
    # Loose: resampling a hard-edged test block rings, which spreads the alpha a
    # few pixels wider than the arithmetic answer of 50.
    assert (box[2] - box[0]) == pytest.approx(50, rel=0.2), "drawing should be halved"


@pytest.mark.parametrize("name, persona, ruler, x_window", [
    ("head-pat", COMMON, ear_tip_to_paw, (0.62, 1.0)),
    ("rub-leg", "evil", ear_tip_to_paw, (0.45, 1.0)),
    ("head-pat", COMMON, ear_tip_to_ear_tip, (0.0, 1.0)),
    ("running-right", COMMON, silhouette_scale, (0.0, 1.0)),
    ("running-left", COMMON, silhouette_scale, (0.0, 1.0)),
    ("running-right", "evil", silhouette_scale, (0.0, 1.0)),
    ("running-left", "evil", silhouette_scale, (0.0, 1.0)),
    ("walking-right", "evil", silhouette_scale, (0.0, 1.0)),
    ("walking-left", "evil", silhouette_scale, (0.0, 1.0)),
], ids=["head-pat height", "rub-leg height", "head-pat head width",
        "run-right common area", "run-left common area",
        "run-right evil area", "run-left evil area",
        "walk-right evil area", "walk-left evil area"])
def test_clips_match_the_reference_dog_size(lib, name, persona, ruler, x_window):
    """The dog must read as the same size in every clip.

    Three rulers, because no single one covers every pose:

    * Imported strips arrive from renders at other scales — head-pat was 1.18x
      and rub-leg 1.07x before correction — and are measured ear-tip to paw.
    * Height alone is not enough. The head-pat render has puppy proportions, a
      bigger head on shorter legs, and the head is lowered under the hand: its
      height matched while the dog still read as visibly larger, which is what
      shipped in v1.2. Head width is what the eye actually judges.
    * The running rows face sideways, so they have no ear-to-ear line and their
      stretched pose makes height meaningless. Drawn area covers them; they were
      1.17-1.19x before correction, in both personas.

    x_window skips foreign objects: the hand above head-pat, the pant leg beside
    rub-leg.
    """
    clip = lib.resolve(name, persona)
    if clip is None:
        pytest.skip(f"{persona}/{name} not present")
    ratio = ruler(clip, x_window) / ruler(lib.resolve("idle", COMMON))
    assert 0.9 <= ratio <= 1.1, f"{persona}/{name} dog is {ratio:.2f}x the reference dog"


def test_reference_clips_agree_on_the_dog_size(lib):
    """The rulers above are only worth anything if the clips they are measured
    against agree with each other. These four all show an upright dog from the
    front, so the area ruler should read the same for all of them."""
    measured = {
        f"{persona}/{name}": silhouette_scale(lib.resolve(name, persona))
        for name, persona in (("idle", COMMON), ("idle", "evil"),
                              ("head-tilt", "evil"), ("waiting", COMMON))
    }
    spread = max(measured.values()) / min(measured.values())
    assert spread < 1.05, f"reference clips disagree on the dog size: {measured}"


def test_shrink_artwork_keeps_the_canvas_and_the_paw_line(lib):
    """A clip that does not own its window must stay one cell in size.

    The floor calculation reads the paw line as a fraction of the canvas height,
    so shrinking the canvas along with the art would lift the pet off the floor.
    """
    frame = lib.resolve("idle", COMMON).frames[0]
    shrunk = core.shrink_artwork(frame, 0.85)
    assert shrunk.size == frame.size, "canvas must not change"
    before = frame.getchannel("A").getbbox()
    after = shrunk.getchannel("A").getbbox()
    assert after[3] == before[3], "paw line must not move"
    assert after[2] - after[0] < before[2] - before[0], "artwork should be narrower"
    centre = lambda box: (box[0] + box[2]) / 2  # noqa: E731
    assert abs(centre(after) - centre(before)) <= 1, "artwork should stay centred"


def test_atlas_row_can_declare_a_scale(tmp_path):
    """Atlas rows had no way to correct their size, so the oversized running
    rows could not be fixed without re-cutting the sheet."""
    root = tmp_path / "assets"
    root.mkdir()
    sheet = Image.new("RGBA", (192, 208), (0, 0, 0, 0))
    sheet.paste(Image.new("RGBA", (100, 100), (9, 9, 9, 255)), (40, 100))
    sheet.save(root / "atlas.png")
    (root / "manifest.json").write_text(json.dumps({
        "cell": [192, 208], "atlas": "atlas.png",
        "rows": [{"name": "full", "row": 0, "frames": 1},
                 {"name": "small", "row": 0, "frames": 1, "scale": 0.5}]}))

    library = ClipLibrary(root).load()
    full, small = library.resolve("full", COMMON), library.resolve("small", COMMON)
    assert full.size == small.size == (192, 208), "the cell size must be untouched"
    full_box = full.frames[0].getchannel("A").getbbox()
    small_box = small.frames[0].getchannel("A").getbbox()
    assert (small_box[2] - small_box[0]) == pytest.approx(
        (full_box[2] - full_box[0]) / 2, rel=0.2)


def test_dynamic_clip_scale_shrinks_the_whole_frame(tmp_path):
    """The opposite case: a dynamic clip owns its window, so the canvas goes
    with the art. head-pat and rub-leg rely on this."""
    root = asset_tree(tmp_path)
    write_clip(root, COMMON, "swoop", frames=2, size=(200, 300),
               options={"scale": 0.5, "dynamic": True})
    assert ClipLibrary(root).load().resolve("swoop", COMMON).size == (100, 150)


def test_bad_anim_json_is_reported_not_fatal(tmp_path):
    root = asset_tree(tmp_path)
    write_clip(root, COMMON, "broken", frames=1, options="{ nope")

    library = ClipLibrary(root).load()
    assert library.resolve("broken", COMMON) is not None
    assert any("broken" in w for w in library.warnings)


def test_missing_assets_reported_not_crashed(tmp_path):
    library = ClipLibrary(tmp_path / "does-not-exist").load()
    assert len(library) == 0
    assert library.warnings
