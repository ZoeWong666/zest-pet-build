#!/usr/bin/env python3
"""
Zest Desktop Pet — standalone macOS companion.
"""

import os, sys, time, math, io, random
from PIL import Image
import objc
from AppKit import (
    NSApplication, NSWindow, NSView, NSImage,
    NSBackingStoreBuffered, NSBorderlessWindowMask, NSFloatingWindowLevel,
    NSTrackingArea, NSTrackingMouseEnteredAndExited, NSTrackingMouseMoved,
    NSTrackingActiveAlways, NSTrackingInVisibleRect,
    NSMenu, NSMenuItem, NSApp, NSApplicationActivationPolicyAccessory,
    NSColor, NSEvent, NSCompositingOperationSourceOver, NSScreen,
)
import Foundation
from Foundation import NSObject, NSTimer, NSMakeRect, NSMakeSize, NSMakePoint

# ── CONFIG ──────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
SPRITESHEET_PATH = os.path.join(BASE, "final", "spritesheet-extended.webp")
CELL_W, CELL_H = 192, 208
SCALE = 1.0
SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
PERSONA = "normal"  # "normal" | "evil"

ROWS = {
    0:  ("idle",          6,  6),  # fps 6 = 0.75x of original 8
    1:  ("running-right", 8,  10),
    2:  ("running-left",  8,  10),
    3:  ("waving",        4,  6),
    4:  ("jumping",       5,  8),
    5:  ("failed",        8,  5),
    6:  ("waiting",       6,  4),
    7:  ("running",       6,  10),
    8:  ("review",        6,  6),
    9:  ("look-A",        8,  4),
    10: ("look-B",        8,  4),
}
TEMP_DUR = {"waving": 2.0, "jumping": 1.5, "failed": 2.5,
            "running-right": 0.8, "running-left": 0.8, "looking": 3.0}

# Random pool for double-click
DOUBLE_CLICK_POOL = ["waving", "jumping", "failed", "running", "review"]

# ── HELPERS ─────────────────────────────────────────────

def pil_to_nsimage(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return NSImage.alloc().initWithData_(
        Foundation.NSData.dataWithBytes_length_(buf.getvalue(), buf.tell()))

def pad_to_cell(pil_img, cell_w=CELL_W, cell_h=CELL_H):
    """Center a PIL image in a transparent cell_w x cell_h canvas."""
    canvas = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
    iw, ih = pil_img.size
    scale = min(cell_w / iw, cell_h / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    resized = pil_img.resize((new_w, new_h), Image.LANCZOS)
    ox = (cell_w - new_w) // 2
    oy = (cell_h - new_h) // 2
    canvas.paste(resized, (ox, oy), resized)
    return canvas

def load_strip_frames(path, num_frames, row_y_start=None, row_y_end=None):
    """Load a horizontal strip, splitting into num_frames evenly."""
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    if row_y_start is None:
        row_y_start = 0
    if row_y_end is None:
        row_y_end = h
    row_h = row_y_end - row_y_start
    cell_w = w // num_frames
    frames = []
    for i in range(num_frames):
        left = i * cell_w
        # Find actual content bounds within this cell
        crop = img.crop((left, row_y_start, left + cell_w, row_y_end))
        frames.append(pil_to_nsimage(crop))
    return frames

def detect_content_rows(path):
    """Find top and bottom of non-magenta content in image."""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    # Scan at mid_x
    mx = w // 2
    top = None
    bottom = None
    for y in range(0, h, 2):
        r, g, b = img.getpixel((mx, y))
        is_m = r > 180 and g < 100 and b > 180
        if not is_m:
            if top is None:
                top = y
            bottom = y
    return top, bottom

# ── PET VIEW ────────────────────────────────────────────

class PetView(NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(PetView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._cell_img = None
        self._ctrl = controller
        return self

    def setImage_(self, img):
        self._cell_img = img
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self._cell_img:
            NSColor.clearColor().set()
            dest = self.bounds()
            src = NSMakeRect(0, 0, self._cell_img.size().width, self._cell_img.size().height)
            self._cell_img.drawInRect_fromRect_operation_fraction_(
                dest, src, NSCompositingOperationSourceOver, 1.0)

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event): self._ctrl.handleMouseDown_(event)
    def mouseDragged_(self, event): self._ctrl.handleMouseDragged_(event)
    def mouseUp_(self, event): self._ctrl.handleMouseUp_(event)
    def rightMouseDown_(self, event): self._ctrl.handleRightMouseDown_(event)
    def rightMouseUp_(self, event): pass
    def mouseMoved_(self, event): self._ctrl.handleMouseMoved_(event)
    def mouseEntered_(self, event): pass
    def mouseExited_(self, event): pass

# ── CONTROLLER ──────────────────────────────────────────

class PetController(NSObject):
    def init(self):
        self = objc.super(PetController, self).init()
        if self is None:
            return None
        self._cells = {}      # (row, col) -> NSImage
        self._cells_pil = {}  # (row, col) -> PIL Image (for fast compositing)
        self._evil_idle = []     # [NSImage * 6]
        self._evil_idle_pil = [] # [PIL * 6]
        self._evil_runr = []     # [NSImage * 8]
        self._evil_runr_pil = [] # [PIL * 8]
        self._evil_lock = []     # [NSImage * 8]
        self._evil_lock_pil = [] # [PIL * 8]
        self._evil_angry = []     # [NSImage * 8]
        self._evil_angry_pil = [] # [PIL * 8]
        self._evil_grin = []     # [NSImage * 8]
        self._evil_grin_pil = [] # [PIL * 8]
        self._evil_smirk = []     # [NSImage * 8]
        self._evil_smirk_pil = [] # [PIL * 8]
        self._evil_runl = []     # [NSImage * 8] mirrored
        self._evil_runl_pil = [] # [PIL * 8]
        self._waiting_ov = []     # [NSImage * 8] global override
        self._waiting_ov_pil = [] # [PIL * 8]
        self._failed_ov = []     # [NSImage * 8] global override
        self._failed_ov_pil = [] # [PIL * 8]
        self._row = 0
        self._frame = 0
        self._timer = None
        self._temp_state = None
        self._temp_start = 0.0
        self._dragging = False
        self._drag_start = None
        self._win_start = None
        self._last_mouse_x = 0.0
        self._window = None
        self._view = None
        self._props = {}        # name → PIL.Image
        self._active_props = set()  # currently visible prop names
        self._status_file = os.path.join(os.path.expanduser("~"), ".codex", "claude_status")
        self._last_status = "idle"
        self._status_timer = None
        return self

    def launch(self):
        screen = NSScreen.mainScreen()
        sf = screen.visibleFrame()
        w, h = int(CELL_W * SCALE), int(CELL_H * SCALE)
        x = sf.origin.x + sf.size.width - w - 120
        y = sf.origin.y + (sf.size.height - h) / 2

        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), NSBorderlessWindowMask,
            NSBackingStoreBuffered, False)
        self._window.setLevel_(NSFloatingWindowLevel + 1)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setOpaque_(False)
        self._window.setHasShadow_(False)
        self._window.setMovableByWindowBackground_(False)
        self._window.setIgnoresMouseEvents_(False)
        self._window.setCollectionBehavior_((1 << 3) | (1 << 0))
        self._window.setAcceptsMouseMovedEvents_(True)
        self._window.orderFrontRegardless()
        self._window.makeKeyAndOrderFront_(None)

        self._view = PetView.alloc().initWithFrame_controller_(
            NSMakeRect(0, 0, w, h), self)
        self._window.setContentView_(self._view)

        opts = (NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved |
                NSTrackingActiveAlways | NSTrackingInVisibleRect)
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self._view.bounds(), opts, self._view, None)
        self._view.addTrackingArea_(ta)

        self.load_cells()
        self.load_evil_strips()
        self.load_overrides()
        self.load_props()
        self.ensure_claude_hooks()
        self.switch_to("idle")
        self._status_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, "checkStatus:", None, True)
        print("  Zest is here! 🐾")
        print("     click → wave   |   right-click → menu   |   drag → run")

    def ensure_claude_hooks(self):
        """Auto-inject pet status hooks into ~/.claude/settings.json if missing."""
        settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        try:
            with open(settings_path) as f:
                settings = f.read()
        except:
            print("  (no ~/.claude/settings.json found, skipping hook setup)")
            return

        if '"zest_pet_status"' in settings:
            return  # already installed

        import json
        try:
            config = json.loads(settings)
        except:
            return

        hooks = config.get("hooks", {})
        changed = False

        pet_hooks = [
            ("Stop", "echo 'waiting' > ~/.codex/claude_status"),
            ("UserPromptSubmit", "echo 'busy' > ~/.codex/claude_status"),
            ("PermissionRequest", "echo 'confirm' > ~/.codex/claude_status"),
        ]
        for event, cmd in pet_hooks:
            if event not in hooks:
                hooks[event] = []
            already = any(
                h.get("command", "") == cmd
                for entry in hooks[event]
                for h in entry.get("hooks", [])
            )
            if not already:
                hooks[event].append({
                    "matcher": "",
                    "hooks": [{"type": "command", "command": cmd, "_note": "zest_pet_status"}]
                })
                changed = True

        if changed:
            config["hooks"] = hooks
            with open(settings_path, "w") as f:
                json.dump(config, f, indent=2)
            print("  ✓ Claude hooks auto-configured")
        else:
            print("  (Claude hooks already present)")

    def checkStatus_(self, timer):
        """Watch ~/.codex/claude_status and auto-switch state."""
        if self._temp_state:
            return
        try:
            with open(self._status_file) as f:
                status = f.read().strip()
        except:
            return
        if status == self._last_status:
            return
        self._last_status = status
        state_map = {"waiting": "waiting", "busy": "running", "done": "review", "idle": "idle", "confirm": "waving"}
        if status in state_map and not self._temp_state:
            self.switch_to(state_map[status])

    # ── LOADING ──────────────────────────────────────

    def load_cells(self):
        img = Image.open(SPRITESHEET_PATH)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        w, h = img.size
        print(f"Loaded spritesheet {w}x{h}")
        for row, (name, frames, _fps) in ROWS.items():
            for col in range(frames):
                left, top = col * CELL_W, row * CELL_H
                if left + CELL_W <= w and top + CELL_H <= h:
                    cell = img.crop((left, top, left + CELL_W, top + CELL_H))
                    self._cells_pil[(row, col)] = cell.copy()
                    self._cells[(row, col)] = pil_to_nsimage(cell)

    def load_evil_strips(self):
        """Load evil persona strips from decoded/"""
        decoded = os.path.join(BASE, "decoded")

        # Evil idle: prefer pre-extracted frames, fall back to 1.png
        idle_frames_dir = os.path.join(decoded, "evil-idle-normalized")
        if os.path.isdir(idle_frames_dir):
            self._evil_idle = []
            for i in range(6):
                fp = os.path.join(idle_frames_dir, f"{i:02d}.png")
                if os.path.exists(fp):
                    pil_img = pad_to_cell(Image.open(fp))
                    self._evil_idle_pil.append(pil_img.copy())
                    self._evil_idle.append(pil_to_nsimage(pil_img))
            if self._evil_idle:
                print(f"Loaded evil idle from frames: {len(self._evil_idle)} frames")
        elif os.path.exists(os.path.join(decoded, "1.png")):
            idle_path = os.path.join(decoded, "1.png")
            top, bottom = detect_content_rows(idle_path)
            if top is not None and bottom is not None:
                self._evil_idle = load_strip_frames(idle_path, 6, top, bottom)
                print(f"Loaded evil idle from 1.png: {len(self._evil_idle)} frames")

        # Evil running-right: from pre-extracted frames
        runr_frames_dir = os.path.join(decoded, "evil-runr-frames")
        if os.path.isdir(runr_frames_dir):
            self._evil_runr = []
            for i in range(8):
                fp = os.path.join(runr_frames_dir, f"{i:02d}.png")
                if os.path.exists(fp):
                    pil_img = Image.open(fp)
                    self._evil_runr_pil.append(pil_img.copy())
                    self._evil_runr.append(pil_to_nsimage(pil_img))
            if self._evil_runr:
                print(f"Loaded evil run-right from frames: {len(self._evil_runr)} frames")

        # Evil look: from pre-extracted frames
        look_frames_dir = os.path.join(decoded, "evil-look-frames")
        if os.path.isdir(look_frames_dir):
            self._evil_lock = []
            for i in range(8):
                fp = os.path.join(look_frames_dir, f"{i:02d}.png")
                if os.path.exists(fp):
                    pil_img = Image.open(fp)
                    self._evil_lock_pil.append(pil_img.copy())
                    self._evil_lock.append(pil_to_nsimage(pil_img))
            if self._evil_lock:
                print(f"Loaded evil look from frames: {len(self._evil_lock)} frames")

    def _load_frame_override(self, dir_name, ns_list, pil_list, count=8):
        """Load pre-extracted frames from decoded/<dir_name>/ into ns_list + pil_list."""
        dpath = os.path.join(BASE, "decoded", dir_name)
        if not os.path.isdir(dpath):
            return
        for i in range(count):
            fp = os.path.join(dpath, f"{i:02d}.png")
            if os.path.exists(fp):
                pil_img = Image.open(fp)
                pil_list.append(pil_img.copy())
                ns_list.append(pil_to_nsimage(pil_img))
        if ns_list:
            print(f"Loaded {dir_name}: {len(ns_list)} frames")

    def load_overrides(self):
        """Load global animation overrides (waiting, failed)."""
        self._load_frame_override("waiting-frames", self._waiting_ov, self._waiting_ov_pil)
        self._load_frame_override("failed-frames", self._failed_ov, self._failed_ov_pil)
        # Evil angry — pre-normalized
        self._load_frame_override("evil-angry-normalized", self._evil_angry, self._evil_angry_pil)
        # Evil grin
        self._load_frame_override("evil-grin-normalized", self._evil_grin, self._evil_grin_pil)
        # Evil smirk
        self._load_frame_override("evil-smirk-normalized", self._evil_smirk, self._evil_smirk_pil)
        # Evil run-left (mirrored from run-right)
        self._load_frame_override("evil-runl-frames", self._evil_runl, self._evil_runl_pil)

    def load_props(self):
        """Load and pre-process prop images for fast compositing."""
        props_dir = os.path.join(BASE, "decoded", "props")
        if os.path.isdir(props_dir):
            for fname in sorted(os.listdir(props_dir)):
                if fname.endswith(".png"):
                    name = fname[:-4]
                    fp = os.path.join(props_dir, fname)
                    img = Image.open(fp).convert("RGBA")
                    # Remove white background
                    pixels = img.load()
                    for px in range(img.size[0]):
                        for py in range(img.size[1]):
                            r, g, b, a = pixels[px, py]
                            if r > 230 and g > 230 and b > 230:
                                pixels[px, py] = (0, 0, 0, 0)
                    # Pre-scale prop: home = 2x pet height, width adaptive
                    pw, ph = img.size
                    if name == "home":
                        scale = (CELL_H * 1.0) / ph  # 1x pet height
                    else:
                        scale = min(CELL_W * 0.5 / pw, CELL_H * 0.5 / ph)
                    scale = min(scale, 3.0)  # cap at 3x
                    img = img.resize((int(pw * scale), int(ph * scale)), Image.LANCZOS)
                    self._props[name] = img
                    print(f"  Prop '{name}': {img.size}")
            if self._props:
                print(f"Loaded props: {list(self._props.keys())}")

    def composite_props(self, base_pil):
        """Composite props behind pet, then pet on top."""
        if not self._active_props or not self._props:
            return pil_to_nsimage(base_pil)

        w, h = int(CELL_W * SCALE), int(CELL_H * SCALE)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

        # Draw props behind pet
        for name in sorted(self._active_props):
            if name not in self._props:
                continue
            prop = self._props[name].copy()
            prop_scale = w / CELL_W
            pw = int(prop.size[0] * prop_scale)
            ph = int(prop.size[1] * prop_scale)
            prop = prop.resize((pw, ph), Image.LANCZOS)
            # If prop wider than window, crop center portion
            if pw > w:
                crop_left = (pw - w) // 2
                prop = prop.crop((crop_left, 0, crop_left + w, ph))
                pw = w
            ox = (w - pw) // 2
            oy = h - ph + 10
            canvas.paste(prop, (ox, oy), prop)

        # Pet on top
        pet = base_pil.resize((w, h), Image.LANCZOS)
        canvas.paste(pet, (0, 0), pet)

        return pil_to_nsimage(canvas)

    # ── ANIMATION ────────────────────────────────────

    def switch_to(self, name):
        if name == "angry":
            self._row = 20; self._frame = 0; self.show_cell(); self.restart_timer(10); return
        if name == "grin":
            self._row = 21; self._frame = 0; self.show_cell(); self.restart_timer(8); return
        if name == "smirk":
            self._row = 22; self._frame = 0; self.show_cell(); self.restart_timer(8); return
        # Restore normal window size for standard animations
        w, h = int(CELL_W * SCALE), int(CELL_H * SCALE)
        cur = self._window.frame()
        if int(cur.size.width) != w or int(cur.size.height) != h:
            bottom_y = cur.origin.y
            cx = cur.origin.x + cur.size.width / 2
            self._window.setFrame_display_animate_(NSMakeRect(cx - w/2, bottom_y, w, h), False, False)
            self._view.setFrame_(NSMakeRect(0, 0, w, h))
        for row, (n, frames, fps) in ROWS.items():
            if n == name:
                self._row = row; self._frame = 0; self.show_cell(); self.restart_timer(fps); return

    def restart_timer(self, fps):
        if self._timer:
            self._timer.invalidate()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / fps, self, "tick:", None, True)

    def show_cell(self):
        global PERSONA, SCALE
        # Dynamic size for angry/grin — keep bottom edge fixed
        if self._row in (20, 21, 22) and (self._evil_angry or self._evil_grin or self._evil_smirk):
            pil_list = (self._evil_angry_pil if self._row == 20
                   else self._evil_grin_pil if self._row == 21
                   else self._evil_smirk_pil)
            hh = max(im.size[1] for im in pil_list) if pil_list else CELL_H
            ww = max(im.size[0] for im in pil_list) if pil_list else CELL_W
            cur_w = int(ww * SCALE)
            cur_h = int(hh * SCALE)
            old = self._window.frame()
            bottom_y = old.origin.y  # keep bottom edge fixed
            cx = old.origin.x + old.size.width / 2
            new_frame = NSMakeRect(cx - cur_w/2, bottom_y, cur_w, cur_h)
            self._window.setFrame_display_animate_(new_frame, False, False)
            self._view.setFrame_(NSMakeRect(0, 0, cur_w, cur_h))
        elif self._row not in (20, 21, 22):
            # Restore normal height
            w, h = int(CELL_W * SCALE), int(CELL_H * SCALE)
            cur = self._window.frame()
            if int(cur.size.height) != h:
                cx = cur.origin.x + cur.size.width / 2
                cy = cur.origin.y + cur.size.height / 2
                new_frame = NSMakeRect(cx - w/2, cy - h/2, w, h)
                self._window.setFrame_display_animate_(new_frame, False, False)
                self._view.setFrame_(NSMakeRect(0, 0, w, h))

        # Get base cell — prefer PIL for compositing, fall back to NSImage
        base_pil = None
        base_ns = None

        if PERSONA == "evil":
            if self._row == 0 and self._evil_idle:
                idx = self._frame % len(self._evil_idle)
                base_pil = self._evil_idle_pil[idx] if self._evil_idle_pil else None
                base_ns = self._evil_idle[idx]
            elif self._row == 1 and self._evil_runr:
                idx = self._frame % len(self._evil_runr)
                base_pil = self._evil_runr_pil[idx] if self._evil_runr_pil else None
                base_ns = self._evil_runr[idx]
            elif self._row == 2 and self._evil_runl:
                idx = self._frame % len(self._evil_runl)
                base_pil = self._evil_runl_pil[idx] if self._evil_runl_pil else None
                base_ns = self._evil_runl[idx]
            elif self._row == 9 and self._evil_lock:
                idx = self._frame % len(self._evil_lock)
                base_pil = self._evil_lock_pil[idx] if self._evil_lock_pil else None
                base_ns = self._evil_lock[idx]

        if base_ns is None:
            # Evil smirk (row 22)
            if self._row == 22 and self._evil_smirk:
                idx = self._frame % len(self._evil_smirk)
                max_w = max(im.size[0] for im in self._evil_smirk_pil) if self._evil_smirk_pil else CELL_W
                if self._evil_smirk_pil:
                    pil = self._evil_smirk_pil[idx]
                    if pil.size[0] < max_w:
                        canvas = Image.new('RGBA', (max_w, pil.size[1]), (0,0,0,0))
                        canvas.paste(pil, ((max_w-pil.size[0])//2, 0), pil)
                        base_pil = canvas
                        base_ns = pil_to_nsimage(canvas)
                    else:
                        base_pil = pil
                        base_ns = self._evil_smirk[idx]
                else:
                    base_pil = None
                    base_ns = self._evil_smirk[idx]
            # Evil grin (row 21)
            elif self._row == 21 and self._evil_grin:
                idx = self._frame % len(self._evil_grin)
                max_w = max(im.size[0] for im in self._evil_grin_pil) if self._evil_grin_pil else CELL_W
                if self._evil_grin_pil:
                    pil = self._evil_grin_pil[idx]
                    if pil.size[0] < max_w:
                        canvas = Image.new('RGBA', (max_w, pil.size[1]), (0,0,0,0))
                        canvas.paste(pil, ((max_w-pil.size[0])//2, 0), pil)
                        base_pil = canvas
                        base_ns = pil_to_nsimage(canvas)
                    else:
                        base_pil = pil
                        base_ns = self._evil_grin[idx]
                else:
                    base_pil = None
                    base_ns = self._evil_grin[idx]
            # Angry (row 20, evil only) — pad narrow frames to max width
            elif self._row == 20 and self._evil_angry:
                idx = self._frame % len(self._evil_angry)
                max_w = max(im.size[0] for im in self._evil_angry_pil) if self._evil_angry_pil else CELL_W
                if self._evil_angry_pil:
                    pil = self._evil_angry_pil[idx]
                    if pil.size[0] < max_w:
                        canvas = Image.new('RGBA', (max_w, pil.size[1]), (0,0,0,0))
                        ox = (max_w - pil.size[0]) // 2
                        canvas.paste(pil, (ox, 0), pil)
                        base_pil = canvas
                        base_ns = pil_to_nsimage(canvas)
                    else:
                        base_pil = pil
                        base_ns = self._evil_angry[idx]
                else:
                    base_pil = None
                    base_ns = self._evil_angry[idx]
            # Global overrides: waiting (row 6), failed (row 5)
            elif self._row == 6 and self._waiting_ov:
                idx = self._frame % len(self._waiting_ov)
                base_pil = self._waiting_ov_pil[idx] if self._waiting_ov_pil else None
                base_ns = self._waiting_ov[idx]
            elif self._row == 5 and self._failed_ov:
                idx = self._frame % len(self._failed_ov)
                base_pil = self._failed_ov_pil[idx] if self._failed_ov_pil else None
                base_ns = self._failed_ov[idx]
            else:
                base_pil = self._cells_pil.get((self._row, self._frame))
                base_ns = self._cells.get((self._row, self._frame))

        if self._active_props and base_pil is not None:
            img = self.composite_props(base_pil)
            self._view.setImage_(img)
        elif base_ns is not None:
            self._view.setImage_(base_ns)

    def tick_(self, timer):
        global PERSONA
        if self._row == 20 and self._evil_angry:
            max_frames = len(self._evil_angry)
        elif self._row == 21 and self._evil_grin:
            max_frames = len(self._evil_grin)
        elif self._row == 22 and self._evil_smirk:
            max_frames = len(self._evil_smirk)
        else:
            max_frames = ROWS[self._row][1]
        if PERSONA == "evil":
            if self._row == 0 and self._evil_idle:
                max_frames = len(self._evil_idle)
            elif self._row == 1 and self._evil_runr:
                max_frames = len(self._evil_runr)
            elif self._row == 2 and self._evil_runl:
                max_frames = len(self._evil_runl)
            elif self._row == 9 and self._evil_lock:
                max_frames = len(self._evil_lock)
        self._frame = (self._frame + 1) % max_frames
        self.show_cell()  # always redraw for animated props
        if self._temp_state:
            dur = TEMP_DUR.get(self._temp_state, 2.0)
            if time.time() - self._temp_start >= dur:
                self._temp_state = None
                self.switch_to("idle")

    def set_temp(self, name):
        self._temp_state = name
        self._temp_start = time.time()
        self.switch_to(name)

    # ── MOUSE ────────────────────────────────────────

    def handleMouseDown_(self, event):
        if event.clickCount() >= 2:
            self._dragging = False
            self._drag_start = None
            pool = DOUBLE_CLICK_POOL.copy()
            if PERSONA == "evil":
                if self._evil_angry: pool.append("angry")
                if self._evil_grin: pool.append("grin")
                if self._evil_smirk: pool.append("smirk")
            choice = random.choice(pool)
            self.set_temp(choice)
        else:
            self._dragging = False
            self._drag_start = NSEvent.mouseLocation()
            self._win_start = self._window.frame().origin
            self._last_mouse_x = self._drag_start.x

    def handleMouseDragged_(self, event):
        if self._drag_start is None:
            return
        cur = NSEvent.mouseLocation()
        dx = cur.x - self._drag_start.x
        dy = cur.y - self._drag_start.y
        if abs(dx) > 2 or abs(dy) > 2:
            self._dragging = True
            nx = self._win_start.x + dx
            ny = self._win_start.y + dy
            self._window.setFrameOrigin_(NSMakePoint(nx, ny))
            if cur.x > self._last_mouse_x + 2:
                self.set_temp("running-right")
            elif cur.x < self._last_mouse_x - 2:
                self.set_temp("running-left")
            self._last_mouse_x = cur.x

    def handleMouseUp_(self, event):
        if self._dragging:
            self._dragging = False
            self._temp_state = None
            self.switch_to("idle")
        elif not self._dragging and self._drag_start is not None:
            self.set_temp("waving")
        self._drag_start = None

    def handleRightMouseDown_(self, event):
        self.show_menu(event)

    def handleMouseMoved_(self, event):
        if self._temp_state and self._temp_state != "looking":
            return
        wf = self._window.frame()
        cx = wf.origin.x + wf.size.width / 2
        cy = wf.origin.y + wf.size.height / 2
        mouse = NSEvent.mouseLocation()
        angle = math.degrees(math.atan2(mouse.y - cy, mouse.x - cx))
        clock = (90 - angle) % 360
        dirs = [
            (9, 0, 0), (9, 1, 22.5), (9, 2, 45), (9, 3, 67.5),
            (9, 4, 90), (9, 5, 112.5), (9, 6, 135), (9, 7, 157.5),
            (10, 0, 180), (10, 1, 202.5), (10, 2, 225), (10, 3, 247.5),
            (10, 4, 270), (10, 5, 292.5), (10, 6, 315), (10, 7, 337.5),
        ]
        best = min(dirs, key=lambda d: min(abs(d[2] - clock), 360 - abs(d[2] - clock)))
        r, c, deg = best
        name = "look-A" if r == 9 else "look-B"
        if self._row != r:
            self.switch_to(name)
            self._temp_state = "looking"
            self._temp_start = time.time()

    def resize_window(self, scale):
        global SCALE
        SCALE = scale
        w, h = int(CELL_W * SCALE), int(CELL_H * SCALE)
        old_frame = self._window.frame()
        cx = old_frame.origin.x + old_frame.size.width / 2
        cy = old_frame.origin.y + old_frame.size.height / 2
        new_frame = NSMakeRect(cx - w/2, cy - h/2, w, h)
        self._window.setFrame_display_animate_(new_frame, True, True)
        self._view.setFrame_(NSMakeRect(0, 0, w, h))
        self._view.setNeedsDisplay_(True)

    def toggle_persona(self):
        global PERSONA
        if PERSONA == "normal":
            PERSONA = "evil"
        else:
            PERSONA = "normal"
        print(f"Persona: {PERSONA}")
        self.switch_to("idle")

    # ── MENU ─────────────────────────────────────────

    def show_menu(self, event):
        global PERSONA
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        # Persona toggle
        p_label = "😈 Mode: EVIL" if PERSONA == "normal" else "🐶 Mode: NORMAL"
        p_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            p_label, "menuPersona:", "")
        p_item.setTarget_(self)
        p_item.setEnabled_(True)
        menu.addItem_(p_item)
        menu.addItem_(NSMenuItem.separatorItem())

        # Scale submenu
        scale_menu = NSMenu.alloc().init()
        scale_menu.setAutoenablesItems_(False)
        for s in SCALES:
            label = f"{int(s*100)}%" if s != 1.0 else "100% (default)"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "menuScale:", "")
            item.setTag_(int(s * 100))
            item.setTarget_(self)
            item.setEnabled_(True)
            scale_menu.addItem_(item)
        st = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Size", "", "")
        st.setSubmenu_(scale_menu)
        menu.addItem_(st)
        menu.addItem_(NSMenuItem.separatorItem())

        # Props toggle
        if self._props:
            menu.addItem_(NSMenuItem.separatorItem())
            for name in sorted(self._props.keys()):
                checked = "✓ " if name in self._active_props else "   "
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"{checked}Prop: {name}", "menuProp:", "")
                item.setTarget_(self)
                item.setEnabled_(True)
                item.setRepresentedObject_(name)
                menu.addItem_(item)

        # Animation states
        for row in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
            name, frames, _ = ROWS[row]
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{name} ({frames}f)", "menuPick:", "")
            item.setTag_(row)
            item.setTarget_(self)
            item.setEnabled_(True)
            menu.addItem_(item)
        # Evil specials
        if PERSONA == "evil":
            if self._evil_angry:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"angry ({len(self._evil_angry)}f)", "menuPick:", "")
                item.setTag_(20); item.setTarget_(self); item.setEnabled_(True)
                menu.addItem_(item)
            if self._evil_grin:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"grin ({len(self._evil_grin)}f)", "menuPick:", "")
                item.setTag_(21); item.setTarget_(self); item.setEnabled_(True)
                menu.addItem_(item)
            if self._evil_smirk:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"smirk ({len(self._evil_smirk)}f)", "menuPick:", "")
                item.setTag_(22); item.setTarget_(self); item.setEnabled_(True)
                menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())
        q = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "menuQuit:", "q")
        q.setTarget_(self)
        q.setEnabled_(True)
        menu.addItem_(q)
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self._view)

    def menuPersona_(self, sender):
        self.toggle_persona()

    def menuScale_(self, sender):
        s = sender.tag() / 100.0
        self.resize_window(s)

    def menuProp_(self, sender):
        name = sender.representedObject()
        if name in self._active_props:
            self._active_props.discard(name)
        else:
            self._active_props.add(name)
        self.show_cell()

    def menuPick_(self, sender):
        tag = sender.tag()
        if tag == 20:
            self.set_temp("angry")
        elif tag == 21:
            self.set_temp("grin")
        elif tag == 22:
            self.set_temp("smirk")
        else:
            name, _, _ = ROWS[tag]
            self.set_temp(name)

    def menuQuit_(self, sender):
        NSApp().terminate_(None)


# ── MAIN ────────────────────────────────────────────────

def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    global _ctrl
    _ctrl = PetController.alloc().init()
    _ctrl.launch()
    NSApp().run()

if __name__ == "__main__":
    main()
