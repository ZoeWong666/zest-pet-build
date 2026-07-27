#!/usr/bin/env python3
"""Zest Desktop Pet — standalone macOS companion."""

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
PERSONA = "normal"

ROWS = {
    0:("idle",6,6), 1:("running-right",8,10), 2:("running-left",8,10),
    3:("waving",4,6), 4:("jumping",5,8), 5:("failed",8,5),
    6:("waiting",6,4), 7:("running",6,10), 8:("review",6,6),
    9:("look-A",8,4), 10:("look-B",8,4),
}
TEMP_DUR = {"waving":2.0,"jumping":1.5,"failed":2.5,"running-right":0.8,"running-left":0.8,"looking":3.0,"poop":5.0,"angry":4.0,"grin":4.0,"smirk":4.0}
DOUBLE_CLICK_POOL = ["waving","jumping","failed","running","review"]

# Evil specials: name -> (row, fps, dir_name, dynamic_size)
EVIL_SPECIAL = {
    "angry":(20,10,"evil-angry-normalized",True),
    "grin":(21,8,"evil-grin-normalized",True),
    "smirk":(22,8,"evil-smirk-normalized",True),
    "poop":(23,5,"evil-poop-normalized",False),
}
EVIL_STATIC = {"idle":(0,6,"evil-idle-normalized",True),"runr":(1,10,"evil-runr-frames",False),"runl":(2,10,"evil-runl-frames",False),"look":(9,4,"evil-look-frames",False)}

# ── HELPERS ─────────────────────────────────────────────
def pil_to_nsimage(pil_img):
    buf = io.BytesIO(); pil_img.save(buf, format="PNG")
    return NSImage.alloc().initWithData_(Foundation.NSData.dataWithBytes_length_(buf.getvalue(), buf.tell()))

def pad_to_cell(pil_img, cw=CELL_W, ch=CELL_H):
    canvas = Image.new("RGBA",(cw,ch),(0,0,0,0))
    iw,ih = pil_img.size
    s = min(cw/iw, ch/ih)
    r = pil_img.resize((int(iw*s), int(ih*s)), Image.LANCZOS)
    canvas.paste(r, ((cw-r.size[0])//2, (ch-r.size[1])//2), r)
    return canvas

def load_frames_from_dir(dpath, count, use_pad=False):
    """Load count PNGs from dpath. Returns (ns_list, pil_list)."""
    ns, pl = [], []
    if not os.path.isdir(dpath): return ns, pl
    for i in range(count):
        fp = os.path.join(dpath, f"{i:02d}.png")
        if os.path.exists(fp):
            pi = pad_to_cell(Image.open(fp)) if use_pad else Image.open(fp)
            pl.append(pi.copy()); ns.append(pil_to_nsimage(pi))
    return ns, pl

# ── PET VIEW ────────────────────────────────────────────
class PetView(NSView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc.super(PetView, self).initWithFrame_(frame)
        if self is None: return None
        self._cell_img = None; self._ctrl = controller; return self
    def setImage_(self, img): self._cell_img = img; self.setNeedsDisplay_(True)
    def drawRect_(self, rect):
        if self._cell_img:
            NSColor.clearColor().set()
            d = self.bounds(); s = NSMakeRect(0,0,self._cell_img.size().width,self._cell_img.size().height)
            self._cell_img.drawInRect_fromRect_operation_fraction_(d,s,NSCompositingOperationSourceOver,1.0)
    def acceptsFirstMouse_(self, e): return True
    def mouseDown_(self,e): self._ctrl.handleMouseDown_(e)
    def mouseDragged_(self,e): self._ctrl.handleMouseDragged_(e)
    def mouseUp_(self,e): self._ctrl.handleMouseUp_(e)
    def rightMouseDown_(self,e): self._ctrl.handleRightMouseDown_(e)
    def rightMouseUp_(self,e): pass
    def mouseMoved_(self,e): self._ctrl.handleMouseMoved_(e)
    def mouseEntered_(self,e): pass
    def mouseExited_(self,e): pass

# ── CONTROLLER ──────────────────────────────────────────
class PetController(NSObject):
    def init(self):
        self = objc.super(PetController, self).init()
        if self is None: return None
        self._cells = {}; self._cells_pil = {}
        self._evil = {}       # name -> {"ns":[...], "pil":[...]}
        self._overrides = {}  # row -> {"ns":[...], "pil":[...]}
        self._row = 0; self._frame = 0; self._timer = None
        self._temp_state = None; self._temp_start = 0.0; self._persistent = False
        self._dragging = False; self._drag_start = None; self._win_start = None; self._last_mouse_x = 0.0
        self._window = None; self._view = None
        self._props = {}; self._active_props = set()
        self._status_file = os.path.join(os.path.expanduser("~"), ".codex", "claude_status")
        self._last_status = "idle"; self._status_timer = None
        return self

    # ── LAUNCH ───────────────────────────────────────
    def launch(self):
        sf = NSScreen.mainScreen().visibleFrame()
        w, h = int(CELL_W*SCALE), int(CELL_H*SCALE)
        x = sf.origin.x + sf.size.width - w - 120; y = sf.origin.y + (sf.size.height - h)/2
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x,y,w,h), NSBorderlessWindowMask, NSBackingStoreBuffered, False)
        self._window.setLevel_(NSFloatingWindowLevel+1)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setOpaque_(False); self._window.setHasShadow_(False)
        self._window.setMovableByWindowBackground_(False)
        self._window.setIgnoresMouseEvents_(False)
        self._window.setCollectionBehavior_((1<<3)|(1<<0))
        self._window.setAcceptsMouseMovedEvents_(True)
        self._window.orderFrontRegardless(); self._window.makeKeyAndOrderFront_(None)
        self._view = PetView.alloc().initWithFrame_controller_(NSMakeRect(0,0,w,h), self)
        self._window.setContentView_(self._view)
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self._view.bounds(), NSTrackingMouseEnteredAndExited|NSTrackingMouseMoved|NSTrackingActiveAlways|NSTrackingInVisibleRect, self._view, None)
        self._view.addTrackingArea_(ta)
        self.load_all(); self.ensure_claude_hooks()
        self.switch_to("idle")
        self._status_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0,self,"checkStatus:",None,True)
        print("  Zest is here! 🐾\n     click→wave | right-click→menu | drag→run")

    # ── LOADING ──────────────────────────────────────
    def load_all(self):
        # Main spritesheet
        img = Image.open(SPRITESHEET_PATH)
        if img.mode != "RGBA": img = img.convert("RGBA")
        print(f"Loaded spritesheet {img.size[0]}x{img.size[1]}")
        for row,(name,frames,_) in ROWS.items():
            for col in range(frames):
                left,top = col*CELL_W, row*CELL_H
                if left+CELL_W <= img.size[0] and top+CELL_H <= img.size[1]:
                    cell = img.crop((left,top,left+CELL_W,top+CELL_H))
                    self._cells_pil[(row,col)] = cell.copy()
                    self._cells[(row,col)] = pil_to_nsimage(cell)
        print(f"  Main cells: {len(self._cells)} loaded")
        # Evil static (idle, runr, runl, look)
        decoded = os.path.join(BASE,"decoded")
        for key,(row,fps,dn,pad) in EVIL_STATIC.items():
            ns,pl = load_frames_from_dir(os.path.join(decoded,dn),6 if key=="idle" else 8, use_pad=(key=="idle"))
            if ns: self._evil[key] = {"ns":ns,"pil":pl,"row":row,"fps":fps}; print(f"Loaded evil {key}: {len(ns)}f")
        # Evil specials
        for name,(row,fps,dn,dyn) in EVIL_SPECIAL.items():
            count = 15 if name=="poop" else 8
            ns,pl = load_frames_from_dir(os.path.join(decoded,dn), count)
            if ns: self._evil[name] = {"ns":ns,"pil":pl,"row":row,"fps":fps,"dyn":dyn}; print(f"Loaded {dn}: {len(ns)}f")
        # Global overrides
        for row,dn in [(6,"waiting-frames"),(5,"failed-frames")]:
            ns,pl = load_frames_from_dir(os.path.join(decoded,dn),8)
            if ns: self._overrides[row] = {"ns":ns,"pil":pl}; print(f"Loaded {dn}: {len(ns)}f")
        # Props
        self.load_props()

    def load_props(self):
        pd = os.path.join(BASE,"decoded","props")
        if not os.path.isdir(pd): return
        for fn in sorted(os.listdir(pd)):
            if not fn.endswith(".png"): continue
            name = fn[:-4]; img = Image.open(os.path.join(pd,fn)).convert("RGBA")
            px = img.load()
            for x in range(img.size[0]):
                for y in range(img.size[1]):
                    r,g,b,a = px[x,y]
                    if r>230 and g>230 and b>230: px[x,y] = (0,0,0,0)
            pw,ph = img.size
            s = (CELL_H*1.0)/ph if name=="home" else min(CELL_W*0.5/pw, CELL_H*0.5/ph)
            s = min(s,3.0); img = img.resize((int(pw*s),int(ph*s)), Image.LANCZOS)
            self._props[name] = img; print(f"  Prop '{name}': {img.size}")
        if self._props: print(f"Loaded props: {list(self._props.keys())}")

    def composite_props(self, base_pil):
        if not self._active_props or not self._props: return pil_to_nsimage(base_pil)
        w,h = int(CELL_W*SCALE), int(CELL_H*SCALE)
        canvas = Image.new("RGBA",(w,h),(0,0,0,0))
        for name in sorted(self._active_props):
            if name not in self._props: continue
            prop = self._props[name].copy()
            ps = w/CELL_W; pw,ph = int(prop.size[0]*ps), int(prop.size[1]*ps)
            prop = prop.resize((pw,ph), Image.LANCZOS)
            if pw>w: prop = prop.crop(((pw-w)//2, 0, (pw-w)//2+w, ph)); pw=w
            canvas.paste(prop, ((w-pw)//2, h-ph+10), prop)
        pet = base_pil.resize((w,h), Image.LANCZOS)
        canvas.paste(pet, (0,0), pet)
        return pil_to_nsimage(canvas)

    # ── ANIMATION ────────────────────────────────────
    def switch_to(self, name):
        # Evil specials
        if name in EVIL_SPECIAL:
            row,fps,_,dyn = EVIL_SPECIAL[name]
            self._row = row; self._frame = 0; self.show_cell(); self.restart_timer(fps); return
        # Evil static — restore window size first
        found_evil = False
        for key, (row,fps,_,_) in EVIL_STATIC.items():
            if (key=="idle" and name=="idle") or (key=="runr" and name=="running-right") or (key=="runl" and name=="running-left") or (key=="look" and name=="look-A"):
                self._row = row; self._frame = 0; found_evil = True; break
        if found_evil:
            self._restore_window(); self.show_cell(); self.restart_timer(fps); return
        # Restore normal window
        self._restore_window()
        for row,(n,frames,fps) in ROWS.items():
            if n==name:
                self._row=row; self._frame=0; self.show_cell(); self.restart_timer(fps); return

    def _restore_window(self):
        w,h = int(CELL_W*SCALE), int(CELL_H*SCALE)
        cur = self._window.frame()
        if int(cur.size.width)!=w or int(cur.size.height)!=h:
            by = cur.origin.y; cx = cur.origin.x + cur.size.width/2
            self._window.setFrame_display_animate_(NSMakeRect(cx-w/2,by,w,h), False, False)
            self._view.setFrame_(NSMakeRect(0,0,w,h))

    def restart_timer(self, fps):
        if self._timer: self._timer.invalidate()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0/fps,self,"tick:",None,True)

    def _get_evil_frame(self, key, frame_idx):
        """Get (pil, ns) for an evil animation at frame_idx."""
        e = self._evil.get(key)
        if not e or not e["ns"]: return None, None
        idx = frame_idx % len(e["ns"])
        pil, ns = e["pil"][idx] if e["pil"] else None, e["ns"][idx]
        # Pad narrow frames if dynamic
        if pil and e.get("dyn"):
            mw = max(im.size[0] for im in e["pil"]) if e["pil"] else CELL_W
            if pil.size[0] < mw:
                c = Image.new("RGBA",(mw,pil.size[1]),(0,0,0,0))
                c.paste(pil,((mw-pil.size[0])//2,0),pil)
                pil=c; ns=pil_to_nsimage(c)
        return pil, ns

    def _get_override_frame(self, row, frame_idx):
        ov = self._overrides.get(row)
        if not ov or not ov["ns"]: return None, None
        idx = frame_idx % len(ov["ns"])
        return (ov["pil"][idx] if ov["pil"] else None), ov["ns"][idx]

    def show_cell(self):
        global PERSONA, SCALE
        for key,(row,_,_,dyn) in EVIL_SPECIAL.items():
            if self._row==row and dyn and key in self._evil:
                pl = self._evil[key]["pil"]
                if pl:
                    mw,mh = max(im.size[0] for im in pl), max(im.size[1] for im in pl)
                    cw,ch = int(mw*SCALE), int(mh*SCALE)
                    old = self._window.frame()
                    by = old.origin.y; cx = old.origin.x + old.size.width/2
                    self._window.setFrame_display_animate_(NSMakeRect(cx-cw/2,by,cw,ch),False,False)
                    self._view.setFrame_(NSMakeRect(0,0,cw,ch))
                break

        base_pil, base_ns = None, None

        # Evil static overrides — only for their specific rows
        evil_static_rows = {0:"idle",1:"runr",2:"runl",9:"look"}
        if PERSONA=="evil" and self._row in evil_static_rows:
            key = evil_static_rows[self._row]
            if key in self._evil:
                base_pil, base_ns = self._get_evil_frame(key, self._frame)

        # Evil specials
        if base_ns is None:
            for key,(row,_,_,_) in EVIL_SPECIAL.items():
                if self._row==row and key in self._evil:
                    base_pil, base_ns = self._get_evil_frame(key, self._frame); break
        # Overrides (waiting/failed)
        if base_ns is None:
            base_pil, base_ns = self._get_override_frame(self._row, self._frame)
        # Main spritesheet
        if base_ns is None:
            key = (self._row, self._frame)
            base_pil = self._cells_pil.get(key)
            base_ns = self._cells.get(key)

        if self._active_props and base_pil is not None:
            self._view.setImage_(self.composite_props(base_pil))
        elif base_ns is not None:
            self._view.setImage_(base_ns)

    def tick_(self, timer):
        global PERSONA
        # Find max_frames
        max_frames = ROWS.get(self._row, (None,1,1))[1]
        for key,(row,_,_,_) in EVIL_SPECIAL.items():
            if row==self._row and key in self._evil: max_frames = len(self._evil[key]["ns"]); break
        ov = self._overrides.get(self._row);
        if ov: max_frames = len(ov["ns"])
        evil_static_rows = {0:"idle",1:"runr",2:"runl",9:"look"}
        if PERSONA=="evil" and self._row in evil_static_rows:
            e = self._evil.get(evil_static_rows[self._row])
            if e: max_frames = len(e["ns"])
        self._frame = (self._frame+1) % max_frames
        self.show_cell()
        if self._temp_state and not self._persistent:
            if time.time()-self._temp_start >= TEMP_DUR.get(self._temp_state,2.0):
                self._temp_state = None; self._persistent = False; self.switch_to("idle")

    def set_temp(self, name, persistent=False):
        self._temp_state = name; self._temp_start = time.time()
        self._persistent = persistent; self.switch_to(name)

    # ── MOUSE ────────────────────────────────────────
    def handleMouseDown_(self, event):
        if event.clickCount() >= 2:
            self._dragging = False; self._drag_start = None
            pool = DOUBLE_CLICK_POOL.copy()
            if PERSONA=="evil":
                for k in EVIL_SPECIAL:
                    if k in self._evil: pool.append(k)
            self.set_temp(random.choice(pool), persistent=False)
        else:
            self._dragging = False
            self._drag_start = NSEvent.mouseLocation()
            self._win_start = self._window.frame().origin
            self._last_mouse_x = self._drag_start.x

    def handleMouseDragged_(self, event):
        if self._drag_start is None: return
        cur = NSEvent.mouseLocation()
        dx,dy = cur.x-self._drag_start.x, cur.y-self._drag_start.y
        if abs(dx)>2 or abs(dy)>2:
            self._dragging = True
            self._window.setFrameOrigin_(NSMakePoint(self._win_start.x+dx, self._win_start.y+dy))
            self.set_temp("running-right" if cur.x>self._last_mouse_x+2 else "running-left" if cur.x<self._last_mouse_x-2 else self._temp_state)
            self._last_mouse_x = cur.x

    def handleMouseUp_(self, event):
        if self._dragging: self._dragging=False; self._temp_state=None; self.switch_to("idle")
        elif not self._dragging and self._drag_start is not None: self.set_temp("waving")
        self._drag_start = None

    def handleRightMouseDown_(self, event): self.show_menu(event)

    def handleMouseMoved_(self, event):
        if self._persistent: return  # menu-selected, don't interrupt
        if self._temp_state and self._temp_state!="looking": return
        wf = self._window.frame()
        cx,cy = wf.origin.x+wf.size.width/2, wf.origin.y+wf.size.height/2
        mouse = NSEvent.mouseLocation()
        clock = (90 - math.degrees(math.atan2(mouse.y-cy, mouse.x-cx))) % 360
        dirs = [(9,0,0),(9,1,22.5),(9,2,45),(9,3,67.5),(9,4,90),(9,5,112.5),(9,6,135),(9,7,157.5),
                (10,0,180),(10,1,202.5),(10,2,225),(10,3,247.5),(10,4,270),(10,5,292.5),(10,6,315),(10,7,337.5)]
        r,_,_ = min(dirs, key=lambda d: min(abs(d[2]-clock),360-abs(d[2]-clock)))
        name = "look-A" if r==9 else "look-B"
        if self._row!=r: self.switch_to(name); self._temp_state="looking"; self._temp_start=time.time()

    def resize_window(self, scale):
        global SCALE; SCALE = scale
        w,h = int(CELL_W*SCALE), int(CELL_H*SCALE)
        old = self._window.frame()
        cx,cy = old.origin.x+old.size.width/2, old.origin.y+old.size.height/2
        self._window.setFrame_display_animate_(NSMakeRect(cx-w/2,cy-h/2,w,h),True,True)
        self._view.setFrame_(NSMakeRect(0,0,w,h)); self._view.setNeedsDisplay_(True)

    def toggle_persona(self):
        global PERSONA
        PERSONA = "evil" if PERSONA=="normal" else "normal"
        print(f"Persona: {PERSONA}"); self.switch_to("idle")

    # ── MENU ─────────────────────────────────────────
    def show_menu(self, event):
        global PERSONA
        menu = NSMenu.alloc().init(); menu.setAutoenablesItems_(False)
        # Persona
        p = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "😈 Mode: EVIL" if PERSONA=="normal" else "🐶 Mode: NORMAL", "menuPersona:", "")
        p.setTarget_(self); p.setEnabled_(True); menu.addItem_(p)
        menu.addItem_(NSMenuItem.separatorItem())
        # Scale
        sm = NSMenu.alloc().init(); sm.setAutoenablesItems_(False)
        for s in SCALES:
            lb = f"{int(s*100)}%" if s!=1.0 else "100%"
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(lb,"menuScale:","")
            it.setTag_(int(s*100)); it.setTarget_(self); it.setEnabled_(True); sm.addItem_(it)
        st = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Size","","")
        st.setSubmenu_(sm); menu.addItem_(st); menu.addItem_(NSMenuItem.separatorItem())
        # Props
        if self._props:
            for name in sorted(self._props.keys()):
                ck = "✓ " if name in self._active_props else "   "
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(f"{ck}Prop: {name}","menuProp:","")
                it.setTarget_(self); it.setEnabled_(True); it.setRepresentedObject_(name); menu.addItem_(it)
            menu.addItem_(NSMenuItem.separatorItem())
        # States
        for row in [0,1,2,3,4,5,6,7,8,9,10]:
            n,f,_ = ROWS[row]
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(f"{n} ({f}f)","menuPick:","")
            it.setTag_(row); it.setTarget_(self); it.setEnabled_(True); menu.addItem_(it)
        # Evil specials
        if PERSONA=="evil":
            for key,(tag,_,_,_) in EVIL_SPECIAL.items():
                if key in self._evil:
                    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(f"{key} ({len(self._evil[key]['ns'])}f)","menuPick:","")
                    it.setTag_(tag); it.setTarget_(self); it.setEnabled_(True); menu.addItem_(it)
        menu.addItem_(NSMenuItem.separatorItem())
        q = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit","menuQuit:","q")
        q.setTarget_(self); q.setEnabled_(True); menu.addItem_(q)
        NSMenu.popUpContextMenu_withEvent_forView_(menu, event, self._view)

    def menuPersona_(self,s): self.toggle_persona()
    def menuScale_(self,s): self.resize_window(s.tag()/100.0)
    def menuProp_(self,s):
        n = s.representedObject()
        self._active_props.discard(n) if n in self._active_props else self._active_props.add(n)
        self.show_cell()
    def menuPick_(self,s):
        tag = s.tag()
        for key,(row,_,_,_) in EVIL_SPECIAL.items():
            if row==tag: self.set_temp(key, persistent=True); return
        self._persistent = True; self._temp_state = None
        self.switch_to(ROWS[tag][0])
    def menuQuit_(self,s): NSApp().terminate_(None)

    # ── CLAUDE SYNC ──────────────────────────────────
    def ensure_claude_hooks(self):
        sp = os.path.join(os.path.expanduser("~"),".claude","settings.json")
        try:
            with open(sp) as f: s = f.read()
        except: return
        if '"zest_pet_status"' in s: return
        import json
        try: cfg = json.loads(s)
        except: return
        hooks = cfg.get("hooks",{}); changed = False
        for ev,cmd in [("Stop","echo 'waiting' > ~/.codex/claude_status"),("UserPromptSubmit","echo 'busy' > ~/.codex/claude_status"),("PermissionRequest","echo 'confirm' > ~/.codex/claude_status")]:
            if ev not in hooks: hooks[ev]=[]
            if not any(h.get("command","")==cmd for e in hooks[ev] for h in e.get("hooks",[])):
                hooks[ev].append({"matcher":"","hooks":[{"type":"command","command":cmd,"_note":"zest_pet_status"}]}); changed=True
        if changed: cfg["hooks"]=hooks; json.dump(cfg, open(sp,"w"),indent=2); print("  ✓ Claude hooks auto-configured")
        else: print("  (Claude hooks already present)")

    def checkStatus_(self, timer):
        if self._temp_state: return
        try:
            with open(self._status_file) as f: st = f.read().strip()
        except: return
        if st==self._last_status: return
        self._last_status = st
        m = {"waiting":"waiting","busy":"running","done":"review","idle":"idle","confirm":"waving"}
        if st in m: self.switch_to(m[st])

# ── MAIN ────────────────────────────────────────────────
def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    global _ctrl; _ctrl = PetController.alloc().init(); _ctrl.launch()
    NSApp().run()

if __name__=="__main__": main()
