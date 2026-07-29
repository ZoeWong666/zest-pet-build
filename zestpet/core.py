#!/usr/bin/env python3
"""Zest core: asset discovery, animation clips and the state machine.

Pure PIL, no GUI toolkit imports. Everything here is offline and local.

Asset layout (convention over configuration)::

    assets/
      manifest.json          atlas rows
      atlas.webp             sprite sheet
      anim/<persona>/<name>/ 00.png 01.png ...  [anim.json]
      props/<name>.png

To add an animation: drop a folder of numbered PNGs into
``anim/common/<name>/`` (or ``anim/evil/<name>/``). It is discovered at
startup and appears in the menu. No code changes required.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageChops

COMMON = "common"
DEFAULT_FPS = 8
DEFAULT_TEMP_DUR = 2.0


def asset_root() -> Path:
    """Locate assets/ both when running from source and from a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets"


# ── clip ────────────────────────────────────────────────
@dataclass
class Clip:
    """One animation: a list of frames plus playback metadata."""

    name: str
    frames: List[Image.Image]
    fps: int = DEFAULT_FPS
    persona: str = COMMON
    loop: bool = True
    duration: Optional[float] = None  # seconds to hold when used as a temp action
    dynamic: bool = False  # window resizes to fit this clip's own frame size
    source: str = "dir"

    def __post_init__(self) -> None:
        if self.dynamic and self.frames:
            self._pad_to_widest()
        self.size: Tuple[int, int] = self.frames[0].size if self.frames else (0, 0)

    def _pad_to_widest(self) -> None:
        """Centre narrow frames on the widest frame so the pet doesn't jitter.

        Done once at load time; the old code redid this on every rendered frame.
        """
        widest = max(f.size[0] for f in self.frames)
        padded = []
        for f in self.frames:
            if f.size[0] < widest:
                canvas = Image.new("RGBA", (widest, f.size[1]), (0, 0, 0, 0))
                canvas.paste(f, ((widest - f.size[0]) // 2, 0), f)
                f = canvas
            padded.append(f)
        self.frames = padded

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def frame_interval(self) -> float:
        return 1.0 / max(1, self.fps)

    def frame(self, index: int) -> Image.Image:
        return self.frames[index % len(self.frames)]


def pad_to_cell(img: Image.Image, cell: Tuple[int, int]) -> Image.Image:
    cw, ch = cell
    scale = min(cw / img.size[0], ch / img.size[1])
    resized = img.resize((max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.paste(resized, ((cw - resized.size[0]) // 2, (ch - resized.size[1]) // 2), resized)
    return canvas


WHITE_KEY_THRESHOLD = 230


def drop_key_residue(img: Image.Image) -> Image.Image:
    """Make leftover chroma-key pixels transparent.

    The art is rendered on magenta and keyed by whoever produced it, which
    leaves a one-pixel magenta rim on silhouettes — present in the atlas as
    well as in the frame folders. Zest's palette is black, tan, yellow,
    grey-blue and cream, so a strongly magenta pixel (red and blue high, green
    low) is always residue. Tan and yellow carry too much green to match, and
    black is too dark.

    Done at load time so it covers every source, including art added later.
    Vectorised through channel ops; a per-pixel loop would cost far more.
    """
    r, g, b, a = img.split()
    high = lambda v: 255 if v > 110 else 0  # noqa: E731
    low = lambda v: 255 if v < 70 else 0  # noqa: E731
    mask = ImageChops.darker(ImageChops.darker(r.point(high), b.point(high)), g.point(low))
    # Clearing alpha alone is not enough: the magenta stays in the RGB channels,
    # and any later resampling (a scale factor, a size change) blends it back in
    # around the edges. Zero the colour too.
    cleared = Image.merge("RGBA", (
        ImageChops.subtract(r, mask),
        ImageChops.subtract(g, mask),
        ImageChops.subtract(b, mask),
        ImageChops.subtract(a, mask),
    ))
    return cleared


def drop_white_background(img: Image.Image) -> Image.Image:
    """Make near-white pixels transparent so props drop in without an alpha channel.

    Vectorised through PIL channel ops; the original per-pixel Python loop cost
    about 0.2s for a 1.1 megapixel prop.
    """
    r, g, b, a = img.split()
    def over(channel):
        return channel.point(lambda v: 255 if v > WHITE_KEY_THRESHOLD else 0)
    white = ImageChops.darker(ImageChops.darker(over(r), over(g)), over(b))
    img.putalpha(ImageChops.subtract(a, white))
    return img


# ── directional look ────────────────────────────────────
LOOK_CLIPS = ("look-A", "look-B")
LOOK_STEPS = 16


def look_frame(dx: float, dy_up: float) -> Tuple[str, int]:
    """Map a direction vector to (clip name, frame index).

    The two look rows are 8 head orientations each, sweeping clockwise from
    facing-front, so 16 steps of 22.5 deg cover the full circle. ``dy_up`` is
    positive upwards (screen coordinates must be flipped before calling).
    """
    clock = (90 - math.degrees(math.atan2(dy_up, dx))) % 360
    index = int(round(clock / (360 / LOOK_STEPS))) % LOOK_STEPS
    per_clip = LOOK_STEPS // len(LOOK_CLIPS)
    return LOOK_CLIPS[index // per_clip], index % per_clip


# ── library ─────────────────────────────────────────────
class ClipLibrary:
    """Loads the atlas rows and every anim/<persona>/<name>/ directory."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else asset_root()
        self.cell: Tuple[int, int] = (192, 208)
        self.display_name = "Zest"
        self._clips: Dict[Tuple[str, str], Clip] = {}
        self.props: Dict[str, Image.Image] = {}
        self.warnings: List[str] = []

    # -- loading --
    def load(self) -> "ClipLibrary":
        manifest_path = self.root / "manifest.json"
        manifest: dict = {}
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text())
        else:
            self.warnings.append(f"no manifest at {manifest_path}")
        self.cell = tuple(manifest.get("cell", self.cell))  # type: ignore[assignment]
        self.display_name = manifest.get("display_name", self.display_name)

        self._load_atlas(manifest)
        self._load_dirs()
        self._load_props()
        return self

    def _load_atlas(self, manifest: dict) -> None:
        rel = manifest.get("atlas")
        if not rel:
            return
        path = self.root / rel
        if not path.is_file():
            self.warnings.append(f"atlas missing: {path}")
            return
        sheet = Image.open(path)
        if sheet.mode != "RGBA":
            sheet = sheet.convert("RGBA")
        cw, ch = self.cell
        for row in manifest.get("rows", []):
            name, r = row["name"], row["row"]
            frames = []
            for col in range(row.get("frames", 0)):
                left, top = col * cw, r * ch
                if left + cw > sheet.size[0] or top + ch > sheet.size[1]:
                    self.warnings.append(f"atlas row '{name}' frame {col} out of bounds")
                    break
                frames.append(drop_key_residue(sheet.crop((left, top, left + cw, top + ch)).copy()))
            if frames:
                self._put(Clip(
                    name=name, frames=frames, fps=row.get("fps", DEFAULT_FPS),
                    persona=COMMON, duration=row.get("duration"), source="atlas",
                ))

    def _load_dirs(self) -> None:
        anim_root = self.root / "anim"
        if not anim_root.is_dir():
            return
        for persona_dir in sorted(p for p in anim_root.iterdir() if p.is_dir()):
            for clip_dir in sorted(p for p in persona_dir.iterdir() if p.is_dir()):
                clip = self._load_clip_dir(clip_dir, persona_dir.name)
                if clip:
                    self._put(clip)

    def _load_clip_dir(self, clip_dir: Path, persona: str) -> Optional[Clip]:
        paths = sorted(p for p in clip_dir.iterdir() if p.suffix.lower() == ".png")
        if not paths:
            return None
        options: dict = {}
        opt_file = clip_dir / "anim.json"
        if opt_file.is_file():
            try:
                options = json.loads(opt_file.read_text())
            except (OSError, ValueError) as exc:
                self.warnings.append(f"bad anim.json in {clip_dir.name}: {exc}")

        # Inherit playback settings from the atlas clip of the same name so an
        # override directory only has to supply the frames.
        inherited = self._clips.get((COMMON, clip_dir.name))
        fps = options.get("fps", inherited.fps if inherited else DEFAULT_FPS)
        duration = options.get("duration", inherited.duration if inherited else None)

        frames = []
        for p in paths:
            img = Image.open(p)
            img = img.convert("RGBA") if img.mode != "RGBA" else img.copy()
            # Art from a different render can come in at the wrong scale. Rather
            # than re-cutting the frames, declare a factor in anim.json and
            # resize here, so the source files stay untouched and the number is
            # easy to tweak.
            factor = options.get("scale")
            if factor and factor != 1:
                img = img.resize((max(1, round(img.size[0] * factor)),
                                  max(1, round(img.size[1] * factor))), Image.LANCZOS)
            if options.get("pad"):
                img = pad_to_cell(img, self.cell)
            # Last, so nothing downstream can resample the key colour back in.
            frames.append(drop_key_residue(img))
        return Clip(
            name=clip_dir.name, frames=frames, fps=fps, persona=persona,
            loop=options.get("loop", True), duration=duration,
            dynamic=options.get("dynamic", False), source="dir",
        )

    def _load_props(self) -> None:
        prop_dir = self.root / "props"
        if not prop_dir.is_dir():
            return
        cw, ch = self.cell
        for path in sorted(prop_dir.glob("*.png")):
            img = drop_white_background(Image.open(path).convert("RGBA"))
            pw, ph = img.size
            scale = ch / ph if path.stem == "home" else min(cw * 0.5 / pw, ch * 0.5 / ph)
            scale = min(scale, 3.0)
            self.props[path.stem] = img.resize((max(1, int(pw * scale)), max(1, int(ph * scale))), Image.LANCZOS)

    def _put(self, clip: Clip) -> None:
        self._clips[(clip.persona, clip.name)] = clip

    # -- queries --
    def resolve(self, name: str, persona: str) -> Optional[Clip]:
        """Persona-specific clip if present, otherwise the common one."""
        return self._clips.get((persona, name)) or self._clips.get((COMMON, name))

    def has(self, name: str, persona: str) -> bool:
        return self.resolve(name, persona) is not None

    def personas(self) -> List[str]:
        extra = sorted({p for p, _ in self._clips if p != COMMON})
        return [COMMON] + extra

    def names_for(self, persona: str) -> List[str]:
        """Every clip playable under persona (common + persona-only), ordered.

        Atlas rows keep their manifest order; directory-only clips follow,
        alphabetically. That way a newly dropped folder lands predictably.
        """
        atlas_order = [n for (p, n), c in self._clips.items() if c.source == "atlas" and p == COMMON]
        playable = {n for (p, n) in self._clips if p in (COMMON, persona)}
        rest = sorted(playable - set(atlas_order))
        return [n for n in atlas_order if n in playable] + rest

    def persona_only(self, persona: str) -> List[str]:
        """Clips that exist only for this persona (no common fallback)."""
        if persona == COMMON:
            return []
        return sorted(n for (p, n) in self._clips if p == persona and (COMMON, n) not in self._clips)

    def __len__(self) -> int:
        return len(self._clips)


# ── state machine ───────────────────────────────────────
@dataclass
class PetState:
    """Tracks which clip is playing and when it should end.

    ``held`` clips were picked from the menu and never time out.
    """

    library: ClipLibrary
    persona: str = COMMON
    clip: Optional[Clip] = None
    frame: int = 0
    held: bool = False
    pinned: bool = False
    _elapsed: float = 0.0
    _temp_until: Optional[float] = None
    _pin_until: Optional[float] = None
    _now: float = field(default_factory=time.perf_counter)

    def play(self, name: str, *, temp: bool = False, held: bool = False) -> bool:
        clip = self.library.resolve(name, self.persona)
        if clip is None:
            return False
        if clip is self.clip and self._temp_until is None and not temp and not self.pinned:
            return True
        self.clip, self.frame, self._elapsed = clip, 0, 0.0
        self.held, self.pinned = held, False
        self._pin_until = None
        if held or not temp:
            self._temp_until = None
        else:
            self._temp_until = time.perf_counter() + (clip.duration or DEFAULT_TEMP_DUR)
        return True

    def pin(self, name: str, frame_index: int, *, hold: Optional[float] = None) -> bool:
        """Hold one specific frame — used for the directional look frames.

        The atlas look rows are 16 head orientations, not a loop, so the
        correct behaviour is to stop on the frame matching the mouse angle.

        Pinning is *ambient*: it does not mark the pet busy, so autonomous
        behaviour (wandering) can still take over. Only deliberate actions
        (clicks, menu picks) claim the pet.
        """
        clip = self.library.resolve(name, self.persona)
        if clip is None or not clip.frames:
            return False
        self.clip = clip
        self.frame = frame_index % clip.frame_count
        self._elapsed, self.pinned, self.held = 0.0, True, False
        self._temp_until = None
        self._pin_until = time.perf_counter() + (hold if hold is not None else (clip.duration or DEFAULT_TEMP_DUR))
        return True

    def set_persona(self, persona: str) -> None:
        self.persona = persona
        name = self.clip.name if self.clip else "idle"
        self.clip = None  # force re-resolve against the new persona
        if not self.play(name):
            self.play("idle")

    @property
    def busy(self) -> bool:
        """True while a deliberate action (click, menu pick) owns the pet.

        Ambient pinning is deliberately excluded so idle behaviour can run.
        """
        return self.held or self._temp_until is not None

    def advance(self, now: Optional[float] = None) -> bool:
        """Step the clock. Returns True if the rendered frame changed.

        The frame index comes from dividing total elapsed time by the frame
        interval, not from repeatedly subtracting it. Subtraction leaves a
        floating-point residue that makes one frame linger for two ticks and
        the next two frames fire in a single tick — a visible stutter.
        """
        now = time.perf_counter() if now is None else now
        dt = max(0.0, min(now - self._now, 0.25))  # clamp: sleep/wake shouldn't fast-forward
        self._now = now
        if self.clip is None:
            return False

        changed = False
        if not self.pinned:
            self._elapsed += dt
            # Epsilon so a tick landing exactly on a frame boundary rounds up
            # instead of flooring back to the previous frame.
            step = int(self._elapsed / self.clip.frame_interval + 1e-9)
            if self.clip.loop:
                index = step % self.clip.frame_count
            else:
                index = min(step, self.clip.frame_count - 1)
            if index != self.frame:
                self.frame, changed = index, True

        if self._temp_until is not None and now >= self._temp_until:
            self._temp_until = None
            self.play("idle")
            changed = True
        elif self.pinned and self._pin_until is not None and now >= self._pin_until:
            self._pin_until = None
            self.play("idle")
            changed = True
        return changed

    def current_frame(self) -> Optional[Image.Image]:
        return self.clip.frame(self.frame) if self.clip else None

    @property
    def next_wake(self) -> float:
        """Seconds until the next frame is due — lets the UI idle instead of spinning."""
        if self.clip is None or self.pinned:
            return 0.05
        interval = self.clip.frame_interval
        return max(0.005, interval - (self._elapsed % interval))


# ── prop compositing (cached) ───────────────────────────
class Compositor:
    """Composites props behind the pet, memoised per (clip, frame, props, size).

    The old code redid this every timer tick even though props are static.
    """

    def __init__(self, library: ClipLibrary, max_entries: int = 256) -> None:
        self.library = library
        self._cache: Dict[tuple, Image.Image] = {}
        self._max = max_entries

    def clear(self) -> None:
        self._cache.clear()

    def render(self, clip: Clip, frame_index: int, props: frozenset, size: Tuple[int, int]) -> Image.Image:
        base = clip.frame(frame_index)
        if not props:
            return base
        key = (clip.persona, clip.name, frame_index % clip.frame_count, props, size)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        w, h = size
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for name in sorted(props):
            prop = self.library.props.get(name)
            if prop is None:
                continue
            pw, ph = prop.size
            if pw > w:
                prop = prop.crop(((pw - w) // 2, 0, (pw - w) // 2 + w, ph))
                pw = w
            canvas.paste(prop, ((w - pw) // 2, h - ph + 10), prop)
        pet = base if base.size == size else base.resize(size, Image.LANCZOS)
        canvas.paste(pet, (0, 0), pet)
        if len(self._cache) >= self._max:
            self._cache.clear()
        self._cache[key] = canvas
        return canvas


# ── settings ────────────────────────────────────────────
CONFIG_PATH = Path(os.path.expanduser("~")) / ".zestpet" / "config.json"
DEFAULT_CONFIG = {"scale": 1.0, "persona": COMMON, "props": [], "pos": None, "wander": True, "click_through": False}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    """Atomic write so a crash mid-save can't leave a truncated config."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass  # a toy pet must never die over a settings file
