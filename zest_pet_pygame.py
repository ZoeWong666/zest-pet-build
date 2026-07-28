#!/usr/bin/env python3
"""
Zest Desktop Pet — cross-platform version (Windows/macOS/Linux).
"""
import os, sys, time, random
from PIL import Image

CELL_W, CELL_H = 192, 208
SCALE = 1.0

ROWS = {
    0:("idle",6,6), 1:("running-right",8,10), 2:("running-left",8,10),
    3:("waving",4,6), 4:("jumping",5,8), 5:("failed",8,5),
    6:("waiting",6,4), 7:("running",6,10), 8:("review",6,6),
    9:("look-A",8,4), 10:("look-B",8,4),
}
TEMP_DUR = {"waving":2,"jumping":1.5,"failed":2.5,"running-right":0.8,"running-left":0.8}

def resource_path(relative_path):
    try: return os.path.join(sys._MEIPASS, relative_path)
    except: return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def main():
    import pygame
    pygame.init()

    ss_path = resource_path(os.path.join("final","spritesheet-extended.webp"))
    sheet = Image.open(ss_path)
    if sheet.mode != "RGBA": sheet = sheet.convert("RGBA")
    sw, sh = sheet.size
    print(f"Spritesheet: {sw}x{sh}")

    cells = {}
    for row,(name,frames,_) in ROWS.items():
        for col in range(frames):
            left,top = col*CELL_W, row*CELL_H
            if left+CELL_W <= sw and top+CELL_H <= sh:
                cell = sheet.crop((left,top,left+CELL_W,top+CELL_H))
                surf = pygame.image.fromstring(cell.tobytes("raw","RGBA"),cell.size,"RGBA")
                cells[(row,col)] = surf

    win_w, win_h = int(CELL_W*SCALE), int(CELL_H*SCALE)
    si = pygame.display.Info()
    os.environ['SDL_VIDEO_WINDOW_POS'] = f"{si.current_w - win_w - 120},{int((si.current_h - win_h)/2)}"

    # Use SRCALPHA for proper transparency (no green border)
    window = pygame.display.set_mode((win_w, win_h), pygame.NOFRAME | pygame.SRCALPHA)
    pygame.display.set_caption("Zest")

    # Make window topmost on Windows
    if sys.platform == "win32":
        import ctypes
        hwnd = pygame.display.get_wm_info()["window"]
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 3)

    clock = pygame.time.Clock()
    current_row, current_frame = 0, 0
    frame_timer, temp_state, temp_start = 0.0, None, 0.0
    dragging, drag_start, win_start = False, (0,0), (0,0)
    click_time = 0.0

    def switch_to(name):
        nonlocal current_row, current_frame, frame_timer
        for r,(n,frames,fps) in ROWS.items():
            if n==name: current_row=r; current_frame=0; frame_timer=0.0; return

    def set_temp(name):
        nonlocal temp_state, temp_start
        temp_state=name; temp_start=time.time(); switch_to(name)

    # Cycle through animations on right-click
    anim_list = [ROWS[r][0] for r in sorted(ROWS.keys())]
    anim_idx = 0

    switch_to("idle")
    print("Zest (pygame) 🐾 | Right-click → cycle anims | 1-9 keys → states | Drag → move")

    running = True
    while running:
        dt = clock.tick(60) / 1000.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if pygame.K_0 <= event.key <= pygame.K_9:
                    idx = event.key - pygame.K_0
                    if idx in ROWS: set_temp(ROWS[idx][0])
                elif event.key == pygame.K_ESCAPE: running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # left
                    dragging = False
                    click_time = time.time()
                    drag_start = pygame.mouse.get_pos()
                    try:
                        from pygame._sdl2 import Window as SW
                        win_start = SW.from_display_module().position
                    except: win_start = (0,0)
                elif event.button == 3:  # right — cycle animation
                    anim_idx = (anim_idx + 1) % len(anim_list)
                    set_temp(anim_list[anim_idx])
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1:
                    if dragging:
                        dragging=False; temp_state=None; switch_to("idle")
                    elif time.time() - click_time < 0.3:
                        set_temp("waving")
            elif event.type == pygame.MOUSEMOTION:
                if pygame.mouse.get_pressed()[0]:
                    dx,dy = event.rel
                    if abs(dx)>1 or abs(dy)>1:
                        dragging = True
                        try:
                            from pygame._sdl2 import Window as SW
                            p = SW.from_display_module().position
                            SW.from_display_module().position = (p[0]+dx, p[1]+dy)
                        except: pass
                        set_temp("running-right" if dx>1 else "running-left" if dx<-1 else temp_state)

        row_fps = ROWS[current_row][2]
        frame_timer += dt
        if frame_timer >= 1.0/row_fps:
            frame_timer -= 1.0/row_fps
            current_frame = (current_frame+1) % ROWS[current_row][1]

        if temp_state and time.time()-temp_start >= TEMP_DUR.get(temp_state,2.0):
            temp_state=None; switch_to("idle")

        window.fill((0,0,0,0))  # transparent
        surf = cells.get((current_row, current_frame))
        if surf is not None:
            if SCALE != 1.0:
                surf = pygame.transform.smoothscale(surf, (int(CELL_W*SCALE), int(CELL_H*SCALE)))
            window.blit(surf, (0,0))
        pygame.display.flip()

    pygame.quit()

if __name__=="__main__": main()
