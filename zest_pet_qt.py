#!/usr/bin/env python3
"""Zest Desktop Pet — PyQt6 cross-platform version with full feature parity."""
import os, sys, time, math, random
from PIL import Image, ImageQt
from PyQt6.QtWidgets import (QApplication, QMenu, QLabel, QWidgetAction, QWidget, QVBoxLayout)
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QPixmap, QAction

# ── CONFIG ──────────────────────────────────────────────
def get_base():
    try: return sys._MEIPASS
    except: return os.path.dirname(os.path.abspath(__file__))
BASE = get_base()
CELL_W, CELL_H = 192, 208
SCALE = 1.0; SCALES = [0.5, 0.75, 1.0, 1.5, 2.0]
ROWS = {0:("idle",6,6),1:("running-right",8,10),2:("running-left",8,10),3:("waving",4,6),4:("jumping",5,8),5:("failed",8,5),6:("waiting",6,4),7:("running",6,10),8:("review",6,6),9:("look-A",8,4),10:("look-B",8,4)}
TEMP_DUR = {"waving":2,"jumping":1.5,"failed":2.5,"running-right":0.8,"running-left":0.8,"looking":3,"angry":4,"grin":4,"smirk":4,"poop":5}
EVIL_SPECIAL = {"angry":(20,10,"evil-angry-normalized",True),"grin":(21,8,"evil-grin-normalized",True),"smirk":(22,8,"evil-smirk-normalized",True),"poop":(23,5,"evil-poop-normalized",False)}
EVIL_STATIC = {"idle":(0,6,"evil-idle-normalized",True),"runr":(1,10,"evil-runr-frames",False),"runl":(2,10,"evil-runl-frames",False),"look":(9,4,"evil-look-frames",False)}

def pil_to_qpixmap(pil_img):
    return QPixmap.fromImage(ImageQt.ImageQt(pil_img))

def load_frames(dpath, count):
    ns, pl = [], []
    if not os.path.isdir(dpath): return ns, pl
    for i in range(count):
        fp = os.path.join(dpath, f"{i:02d}.png")
        if os.path.exists(fp):
            pi = Image.open(fp)
            pl.append(pi.copy()); ns.append(pil_to_qpixmap(pi))
    return ns, pl

def pad_to_cell(pi, cw=CELL_W, ch=CELL_H):
    canvas = Image.new("RGBA",(cw,ch),(0,0,0,0))
    s = min(cw/pi.size[0], ch/pi.size[1])
    r = pi.resize((int(pi.size[0]*s), int(pi.size[1]*s)), Image.LANCZOS)
    canvas.paste(r, ((cw-r.size[0])//2, (ch-r.size[1])//2), r)
    return canvas

# ── PET LABEL ───────────────────────────────────────────
class PetLabel(QLabel):
    def __init__(self, ctrl):
        super().__init__()
        self._ctrl = ctrl
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_start = None; self._dragging = False; self._click_time = 0

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.globalPosition().toPoint()
            self._dragging = False; self._click_time = time.time()
            self._ctrl._drag_win_start = self.window().pos()
        elif e.button() == Qt.MouseButton.RightButton:
            self._ctrl.show_menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_start and e.buttons() & Qt.MouseButton.LeftButton:
            d = e.globalPosition().toPoint() - self._drag_start
            if abs(d.x())>3 or abs(d.y())>3:
                self._dragging = True
                self.window().move(self._ctrl._drag_win_start + d)
                dx = e.globalPosition().toPoint().x() - self._drag_start.x()
                if dx > 3: self._ctrl.set_temp("running-right")
                elif dx < -3: self._ctrl.set_temp("running-left")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._dragging=False; self._ctrl._temp_state=None; self._ctrl.switch_to("idle")
            elif time.time()-self._click_time < 0.3:
                self._ctrl.set_temp("waving")
            self._drag_start = None

# ── CONTROLLER ──────────────────────────────────────────
class PetController:
    def __init__(self):
        self._cells = {}; self._cells_pil = {}
        self._evil = {}; self._overrides = {}
        self._row = 0; self._frame = 0
        self._timer = None; self._anim_timer = None
        self._temp_state = None; self._temp_start = 0.0
        self._persistent = False; self._persona = "normal"
        self._props = {}; self._active_props = set()

    def launch(self):
        self.app = QApplication(sys.argv)
        self.win = QWidget()
        self.win.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.win.setFixedSize(int(CELL_W*SCALE), int(CELL_H*SCALE))
        screen = self.app.primaryScreen().availableGeometry()
        self.win.move(screen.right()-int(CELL_W*SCALE)-120, int((screen.height()-CELL_H*SCALE)/2))
        self.label = PetLabel(self)
        self.label.setFixedSize(int(CELL_W*SCALE), int(CELL_H*SCALE))
        layout = QVBoxLayout(self.win); layout.setContentsMargins(0,0,0,0); layout.addWidget(self.label)
        self.win.show()
        self.load_all()
        self.switch_to("idle")
        self._anim_timer = QTimer(); self._anim_timer.timeout.connect(self.tick)
        self._anim_timer.start(16)  # ~60fps for smooth prop animation
        print("Zest (PyQt6) 🐾 | right-click→menu | drag→move")
        self.app.exec()

    def load_all(self):
        img = Image.open(os.path.join(BASE,"final","spritesheet-extended.webp"))
        if img.mode!="RGBA": img=img.convert("RGBA")
        for row,(name,frames,_) in ROWS.items():
            for col in range(frames):
                left,top = col*CELL_W, row*CELL_H
                if left+CELL_W<=img.size[0] and top+CELL_H<=img.size[1]:
                    cell = img.crop((left,top,left+CELL_W,top+CELL_H))
                    self._cells_pil[(row,col)]=cell.copy()
                    self._cells[(row,col)]=pil_to_qpixmap(cell)
        decoded = os.path.join(BASE,"decoded")
        for key,(row,fps,dn,pad) in EVIL_STATIC.items():
            ns,pl=load_frames(os.path.join(decoded,dn),6 if key=="idle" else 8)
            if ns: self._evil[key]={"ns":ns,"pil":pl,"row":row,"fps":fps}
        for name,(row,fps,dn,dyn) in EVIL_SPECIAL.items():
            count=15 if name=="poop" else 8
            ns,pl=load_frames(os.path.join(decoded,dn),count)
            if ns: self._evil[name]={"ns":ns,"pil":pl,"row":row,"fps":fps,"dyn":dyn}
        for row,dn in [(6,"waiting-frames"),(5,"failed-frames")]:
            ns,pl=load_frames(os.path.join(decoded,dn),8)
            if ns: self._overrides[row]={"ns":ns,"pil":pl}
        pd=os.path.join(BASE,"decoded","props")
        if os.path.isdir(pd):
            for fn in sorted(os.listdir(pd)):
                if fn.endswith(".png"):
                    name=fn[:-4]; img=Image.open(os.path.join(pd,fn)).convert("RGBA")
                    px=img.load()
                    for x in range(img.size[0]):
                        for y in range(img.size[1]):
                            r,g,b,a=px[x,y]
                            if r>230 and g>230 and b>230: px[x,y]=(0,0,0,0)
                    pw,ph=img.size; s=(CELL_H*1.0)/ph if name=="home" else min(CELL_W*0.5/pw,CELL_H*0.5/ph)
                    s=min(s,3.0); img=img.resize((int(pw*s),int(ph*s)),Image.LANCZOS)
                    self._props[name]=img
        print(f"  Spritesheet: {len(self._cells)} cells, Evil: {len(self._evil)} anims, Overrides: {len(self._overrides)}, Props: {len(self._props)}")

    def switch_to(self, name):
        if name in EVIL_SPECIAL:
            row,fps,_,_=EVIL_SPECIAL[name]; self._row=row; self._frame=0; self._frame_interval=1.0/fps; self.show_cell(); return
        for key,(row,fps,_,_) in EVIL_STATIC.items():
            if (key=="idle" and name=="idle") or (key=="runr" and name=="running-right") or (key=="runl" and name=="running-left") or (key=="look" and name=="look-A"):
                self._row=row; self._frame=0; self._frame_interval=1.0/fps; self.show_cell(); return
        for row,(n,frames,fps) in ROWS.items():
            if n==name: self._row=row; self._frame=0; self._frame_interval=1.0/fps; self.show_cell(); return

    def set_temp(self, name, persistent=False):
        self._temp_state=name; self._temp_start=time.time(); self._persistent=persistent; self.switch_to(name)

    def _get_evil_frame(self, key, fidx):
        e=self._evil.get(key)
        if not e or not e["ns"]: return None, None
        idx=fidx%len(e["ns"]); pil=e["pil"][idx] if e["pil"] else None; ns=e["ns"][idx]
        if pil and e.get("dyn"):
            mw=max(im.size[0] for im in e["pil"])
            if pil.size[0]<mw:
                c=Image.new("RGBA",(mw,pil.size[1]),(0,0,0,0)); c.paste(pil,((mw-pil.size[0])//2,0),pil)
                pil=c; ns=pil_to_qpixmap(c)
        return pil, ns

    def show_cell(self):
        base_ns=None; base_pil=None
        evil_rows={0:"idle",1:"runr",2:"runl",9:"look"}
        if self._persona=="evil" and self._row in evil_rows:
            k=evil_rows[self._row]
            if k in self._evil: base_pil,base_ns=self._get_evil_frame(k,self._frame)
        if base_ns is None:
            for key,(row,_,_,_) in EVIL_SPECIAL.items():
                if self._row==row and key in self._evil: base_pil,base_ns=self._get_evil_frame(key,self._frame); break
        if base_ns is None:
            ov=self._overrides.get(self._row)
            if ov: idx=self._frame%len(ov["ns"]); base_pil=ov["pil"][idx] if ov["pil"] else None; base_ns=ov["ns"][idx]
        if base_ns is None:
            base_pil=self._cells_pil.get((self._row,self._frame)); base_ns=self._cells.get((self._row,self._frame))

        # Composite props
        if self._active_props and base_pil is not None:
            w,h=CELL_W,CELL_H; canvas=Image.new("RGBA",(w,h),(0,0,0,0))
            for name in sorted(self._active_props):
                if name not in self._props: continue
                prop=self._props[name].copy()
                if prop.size[0]>w: prop=prop.crop(((prop.size[0]-w)//2,0,(prop.size[0]-w)//2+w,prop.size[1]))
                canvas.paste(prop,((w-prop.size[0])//2, h-prop.size[1]+10),prop)
            pet=base_pil.resize((w,h),Image.LANCZOS); canvas.paste(pet,(0,0),pet)
            self.label.setPixmap(pil_to_qpixmap(canvas))
        elif base_ns is not None:
            self.label.setPixmap(base_ns.scaled(int(CELL_W*SCALE),int(CELL_H*SCALE),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    _ftimer = 0.0
    def tick(self):
        self._ftimer += 0.016
        if self._ftimer >= self._frame_interval:
            self._ftimer -= self._frame_interval
            max_f = ROWS.get(self._row,(None,1,1))[1]
            for key,(row,_,_,_) in EVIL_SPECIAL.items():
                if row==self._row and key in self._evil: max_f=len(self._evil[key]["ns"]); break
            ov=self._overrides.get(self._row)
            if ov: max_f=len(ov["ns"])
            if self._persona=="evil":
                er={0:"idle",1:"runr",2:"runl",9:"look"}
                if self._row in er:
                    e=self._evil.get(er[self._row])
                    if e: max_f=len(e["ns"])
            self._frame=(self._frame+1)%max_f
            self.show_cell()
        # Always redraw for animated props
        if self._active_props: self.show_cell()
        if self._temp_state and not self._persistent:
            if time.time()-self._temp_start>=TEMP_DUR.get(self._temp_state,2.0):
                self._temp_state=None; self._persistent=False; self.switch_to("idle")

    # ── MENU ─────────────────────────────────────────
    def show_menu(self, pos):
        menu=QMenu()
        pa=menu.addAction("😈 Evil Mode" if self._persona=="normal" else "🐶 Normal Mode")
        pa.triggered.connect(self.toggle_persona)
        menu.addSeparator()
        sm=menu.addMenu("Size")
        for s in SCALES:
            a=sm.addAction(f"{int(s*100)}%" if s!=1.0 else "100% (default)")
            a.triggered.connect(lambda checked,sc=s: self.resize_window(sc))
        menu.addSeparator()
        if self._props:
            for name in sorted(self._props.keys()):
                ck="✓ " if name in self._active_props else "   "
                a=menu.addAction(f"{ck}Prop: {name}")
                a.triggered.connect(lambda checked,n=name: self.toggle_prop(n))
            menu.addSeparator()
        for row in [0,1,2,3,4,5,6,7,8,9,10]:
            n,f,_=ROWS[row]; a=menu.addAction(f"{n} ({f}f)"); a.triggered.connect(lambda checked,r=row: self.menu_pick(r))
        if self._persona=="evil":
            for key,(tag,_,_,_) in EVIL_SPECIAL.items():
                if key in self._evil:
                    a=menu.addAction(f"{key} ({len(self._evil[key]['ns'])}f)"); a.triggered.connect(lambda checked,k=key: self.set_temp(k,True))
        menu.addSeparator(); a=menu.addAction("Quit"); a.triggered.connect(self.app.quit)
        menu.exec(pos)

    def toggle_persona(self):
        self._persona="evil" if self._persona=="normal" else "normal"; self.switch_to("idle")

    def toggle_prop(self, name):
        if name in self._active_props: self._active_props.discard(name)
        else: self._active_props.add(name)
        self.show_cell()

    def menu_pick(self, row):
        for key,(tag,_,_,_) in EVIL_SPECIAL.items():
            if tag==row: self.set_temp(key,True); return
        self._persistent=True; self._temp_state=None; self.switch_to(ROWS[row][0])

    def resize_window(self, scale):
        global SCALE; SCALE=scale
        w,h=int(CELL_W*SCALE),int(CELL_H*SCALE)
        self.win.setFixedSize(w,h); self.label.setFixedSize(w,h)
        self.show_cell()

if __name__=="__main__":
    PetController().launch()
