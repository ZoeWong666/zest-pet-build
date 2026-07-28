# Zest 🐾

A local, offline desktop pet. No network, no accounts, no external services —
it reads PNGs off disk and draws a dog on your screen.

```bash
pip install -r requirements.txt
python main.py
```

| Input | Result |
| --- | --- |
| Click | waves |
| Double-click | random surprise action |
| Drag | runs in the direction you pull, falls when dropped |
| Right-click | full menu (mode, size, props, animations, behaviour) |
| Move the cursor nearby | turns its head to watch you |
| Leave it alone | strolls along the screen on its own |
| Tray icon | show/hide, recentre, click-through, quit |

Settings (size, mode, props, position, behaviour toggles) are saved to
`~/.zestpet/config.json` and restored on the next launch.

## Adding a new animation

Make a folder. That's the whole procedure.

```
assets/anim/common/backflip/
    00.png
    01.png
    02.png
```

Restart the pet and `backflip` is in the right-click menu under *Animations*.
No code to edit, no manifest to update, no list to register it in.

Rules the loader follows:

- **Folder name = animation name.** `anim/common/backflip/` becomes `backflip`.
- **Files are played in sorted order.** Any names work (`00.png`, `01.png`, …
  is just the convention). Gaps in numbering don't matter.
- **`common` vs a persona.** `anim/common/…` is always available.
  `anim/evil/…` only plays in Evil mode. Any new folder under `anim/`
  becomes a new selectable mode automatically.
- **Same name overrides.** `anim/common/waiting/` replaces the `waiting` row
  from the sprite atlas. `anim/evil/idle/` replaces `idle` only in Evil mode.
- **Frames in one folder must be the same size**, or the pet will jitter.
  There's a test that enforces this.

### Optional tuning

Drop an `anim.json` beside the frames. Every field is optional; an override
folder inherits timing from the atlas row it replaces.

```json
{
  "fps": 12,
  "duration": 3.5,
  "loop": false,
  "pad": true,
  "dynamic": false
}
```

| Field | Meaning | Default |
| --- | --- | --- |
| `fps` | playback speed | `8`, or inherited |
| `duration` | seconds to hold when triggered as a one-off action | forever |
| `loop` | `false` freezes on the last frame | `true` |
| `pad` | letterbox art smaller than the cell into the full 192×208 cell | `false` |
| `dynamic` | let the window resize to this art's own dimensions | `false` |

### A new mode (persona)

```
assets/anim/sleepy/idle/     # replaces idle in "Sleepy" mode
assets/anim/sleepy/yawn/     # only exists in "Sleepy" mode
```

`sleepy` shows up in the *Mode* menu on its own. Anything it doesn't define
falls back to `common`.

## Asset layout

```
assets/
  manifest.json      sprite-atlas row definitions
  atlas.webp         the sprite sheet
  anim/<mode>/<name>/  frame folders (+ optional anim.json)
  props/<name>.png   scenery drawn behind the pet
```

Props are drawn behind the pet, anchored to the bottom. Near-white pixels are
keyed to transparent on load, so art on a white background works as-is.

`manifest.json` only exists because the atlas is one packed image and its rows
can't be discovered from the filesystem. Loose animation folders need no entry.

## Layout guides

`references/layout-guides/` holds blank grids (6-frame, 8-frame, 8-frame
two-row) at the right cell size for drawing new frames, plus
`canonical-base.png` as the character reference.

## Project layout

```
zestpet/core.py        assets, animation clips, state machine — no GUI imports
zestpet/qt_backend.py  window, rendering, input, menu, tray
main.py                entry point
tests/                 77 tests, no display required
tools/migrate_assets.py  one-shot migration from the old decoded/ layout
```

`core.py` has no Qt dependency, so animation and timing logic is testable
without a display.

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q
```

## Builds

CI produces both on every push to `main` (Actions → the run → Artifacts):

- `Zest-Windows` → `Zest.exe`
- `Zest-macOS` → `Zest.app` zipped, with `LSUIElement` set so it stays out of
  the Dock

Binaries are deliberately not committed. Build locally with:

```bash
pyinstaller --onefile --windowed --name Zest --add-data "assets:assets" main.py
```

Use `assets;assets` instead of `assets:assets` on Windows.
