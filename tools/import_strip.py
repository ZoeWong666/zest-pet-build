#!/usr/bin/env python3
"""Turn a chroma-keyed sprite strip into an animation folder.

    python tools/import_strip.py <image> <mode>/<name> [options]

Example:
    python tools/import_strip.py ~/Desktop/strip.png evil/rub-leg \
        --frames 8 --fps 8 --duration 4 --dynamic

What it does:
  * samples the corners to find the chroma-key colour (the art is nominally
    #FF00FF but renders come out slightly off, e.g. (229, 4, 219))
  * finds frame boundaries by looking for columns that are entirely
    background, instead of dividing the width evenly — the strips are not
    evenly spaced, and small detached bits like motion lines are merged back
    into the frame they belong to
  * keys the background out with a soft edge and removes magenta fringing
  * writes every frame at one common size, anchored bottom-left, so the fixed
    part of the scene (a pant leg, a prop) does not jitter between frames

Pure Pillow, no network, no extra dependencies.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Per-channel distance from the key colour. Below LOW is background, above
# HIGH is subject, in between gets partial alpha so edges stay smooth.
KEY_TOLERANCE_LOW = 40
KEY_TOLERANCE_HIGH = 90
# Detached pieces (motion lines) sit ~2px from their frame, while the gap
# between frames is ~39px, so anything under this belongs to one frame.
GAP_MERGE = 15


def detect_key_colour(img: Image.Image) -> Tuple[int, int, int]:
    """Most common colour in the image.

    Corners are unreliable: these renders have a slight background gradient,
    so a corner reads (213, 11, 206) while the bulk is (229, 4, 219). The
    tolerance ramp absorbs the remaining variation.
    """
    from collections import Counter
    w, h = img.size
    counts = Counter()
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            counts[img.getpixel((x, y))[:3]] += 1
    return counts.most_common(1)[0][0]


def alpha_from_key(img: Image.Image, key: Tuple[int, int, int]) -> Image.Image:
    """Soft alpha: per-channel max distance from the key colour, ramped."""
    r, g, b = img.split()[:3]
    flat = [Image.new("L", img.size, c) for c in key]
    dist = None
    for channel, plane in zip((r, g, b), flat):
        d = ImageChops.difference(channel, plane)
        dist = d if dist is None else ImageChops.lighter(dist, d)
    span = max(1, KEY_TOLERANCE_HIGH - KEY_TOLERANCE_LOW)
    return dist.point(lambda v: 0 if v <= KEY_TOLERANCE_LOW
                      else 255 if v >= KEY_TOLERANCE_HIGH
                      else int((v - KEY_TOLERANCE_LOW) * 255 / span))


def despill(img: Image.Image, alpha: Image.Image) -> Image.Image:
    """Pull magenta tint out of edge pixels.

    Green is the clean channel for a magenta key, so the excess of (r+b)/2
    over g is spill. Applied only where alpha is partial, which is where the
    key colour actually bled into the subject.
    """
    r, g, b = img.split()[:3]
    half = lambda v: v // 2  # noqa: E731
    excess = ImageChops.subtract(ImageChops.add(r.point(half), b.point(half)), g)
    fixed = Image.merge("RGB", (ImageChops.subtract(r, excess), g, ImageChops.subtract(b, excess)))
    edge = alpha.point(lambda v: 255 if 0 < v < 250 else 0)
    return Image.composite(fixed, img.convert("RGB"), edge)


def find_frame_spans(alpha: Image.Image) -> List[Tuple[int, int]]:
    """Column ranges that hold subject pixels, with near neighbours merged."""
    w, h = alpha.size
    px = alpha.load()
    occupied = []
    for x in range(w):
        hit = False
        for y in range(0, h, 2):
            if px[x, y] > 8:
                hit = True
                break
        occupied.append(hit)

    spans: List[List[int]] = []
    start = None
    for x, hit in enumerate(occupied):
        if hit and start is None:
            start = x
        elif not hit and start is not None:
            spans.append([start, x - 1])
            start = None
    if start is not None:
        spans.append([start, w - 1])

    merged: List[List[int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= GAP_MERGE:
            merged[-1][1] = span[1]
        else:
            merged.append(span)
    return [(a, b) for a, b in merged]


def vertical_extent(alpha: Image.Image) -> Tuple[int, int]:
    box = alpha.getbbox()
    if box is None:
        raise SystemExit("the image is entirely background — wrong key colour?")
    return box[1], box[3]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("target", help="mode/name, e.g. evil/rub-leg")
    ap.add_argument("--frames", type=int, default=None, help="expected count; refuses to write on mismatch")
    ap.add_argument("--fps", type=int, default=8)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--dynamic", action="store_true", help="let the window take this art's own size")
    ap.add_argument("--pad", action="store_true", help="letterbox into the 192x208 cell instead")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if "/" not in args.target:
        raise SystemExit("target must look like <mode>/<name>, e.g. evil/rub-leg")
    mode, name = args.target.split("/", 1)

    src = Image.open(Path(args.image).expanduser()).convert("RGB")
    key = detect_key_colour(src)
    alpha = alpha_from_key(src, key)
    cleaned = despill(src, alpha)
    rgba = cleaned.convert("RGBA")
    rgba.putalpha(alpha)

    spans = find_frame_spans(alpha)
    top, bottom = vertical_extent(alpha)
    print(f"key colour      : {key}")
    print(f"frames detected : {len(spans)}")
    print(f"vertical extent : y={top}..{bottom - 1} ({bottom - top}px)")
    for i, (a, b) in enumerate(spans):
        print(f"   frame {i}: x={a}..{b}  width {b - a + 1}")

    if args.frames and len(spans) != args.frames:
        raise SystemExit(f"expected {args.frames} frames but found {len(spans)} — "
                         f"adjust GAP_MERGE or check the strip")

    # One canvas for every frame, anchored bottom-left so whatever is fixed in
    # the scene stays fixed.
    cell_w = max(b - a + 1 for a, b in spans)
    cell_h = bottom - top
    print(f"output cell     : {cell_w}x{cell_h}")

    out_dir = ASSETS / "anim" / mode / name
    if args.dry_run:
        print(f"(dry run) would write {len(spans)} frames to {out_dir}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()

    for i, (a, b) in enumerate(spans):
        frame = rgba.crop((a, top, b + 1, bottom))
        canvas = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
        canvas.paste(frame, (0, cell_h - frame.size[1]), frame)
        if args.pad:
            from zestpet.core import pad_to_cell
            canvas = pad_to_cell(canvas, (192, 208))
        canvas.save(out_dir / f"{i:02d}.png")

    options = {"fps": args.fps}
    if args.duration is not None:
        options["duration"] = args.duration
    if args.dynamic:
        options["dynamic"] = True
    (out_dir / "anim.json").write_text(json.dumps(options, indent=2) + "\n")

    print(f"\nwrote {len(spans)} frames to {out_dir.relative_to(ROOT)}")
    print(f"anim.json: {json.dumps(options)}")
    print("Restart the pet — it appears in the right-click menu automatically.")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
