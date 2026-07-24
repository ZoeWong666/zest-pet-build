#!/usr/bin/env python3
"""
Zest Desktop Pet — cross-platform version (Windows/macOS/Linux).
Uses pygame. Works standalone or can be packaged with pyinstaller.

Install: pip install pygame pillow pyinstaller
Build exe: pyinstaller --onefile --windowed --add-data "final/spritesheet-extended.webp:final" --name Zest zest_pet_pygame.py
"""

import os
import sys
import time
import math
from PIL import Image
import ctypes

# ── CONFIG ──────────────────────────────────────────────
CELL_W, CELL_H = 192, 208
SCALE = 1.0
FPS = 30  # base tick rate

ROWS = {
    0:  ("idle",          6,  8),
    1:  ("running-right", 8,  10),
    2:  ("running-left",  8,  10),
    3:  ("waving",        4,  6),
    4:  ("jumping",       5,  8),
    5:  ("failed",        8,  8),
    6:  ("waiting",       6,  6),
    7:  ("running",       6,  10),
    8:  ("review",        6,  6),
    9:  ("look-A",        8,  4),
    10: ("look-B",        8,  4),
}
TEMP_DUR = {
    "waving": 2.0, "jumping": 1.5, "failed": 2.5,
    "running-right": 0.8, "running-left": 0.8, "looking": 3.0
}

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

# ── MAIN ────────────────────────────────────────────────

def main():
    import pygame
    pygame.init()

    # Load spritesheet
    ss_path = resource_path(os.path.join("final", "spritesheet-extended.webp"))
    sheet = Image.open(ss_path)
    if sheet.mode != "RGBA":
        sheet = sheet.convert("RGBA")
    sw, sh = sheet.size
    print(f"Spritesheet: {sw}x{sh}")

    # Pre-crop cells as pygame surfaces
    cells = {}
    for row, (name, frames, _fps) in ROWS.items():
        for col in range(frames):
            left, top = col * CELL_W, row * CELL_H
            if left + CELL_W <= sw and top + CELL_H <= sh:
                cell_pil = sheet.crop((left, top, left + CELL_W, top + CELL_H))
                mode = cell_pil.mode
                size = cell_pil.size
                surf = pygame.image.fromstring(cell_pil.tobytes("raw", mode), size, mode)
                cells[(row, col)] = surf

    # Window
    win_w = int(CELL_W * SCALE)
    win_h = int(CELL_H * SCALE)
    screen_info = pygame.display.Info()
    screen_w, screen_h = screen_info.current_w, screen_info.current_h

    # Position center-right
    os.environ['SDL_VIDEO_WINDOW_POS'] = f"{screen_w - win_w - 120},{int((screen_h - win_h) / 2)}"

    # Borderless window
    flags = pygame.NOFRAME
    # Try to enable transparency on supported platforms
    try:
        from pygame._sdl2 import Window
    except:
        pass

    window = pygame.display.set_mode((win_w, win_h), flags)
    pygame.display.set_caption("Zest")

    # Make window transparent (platform-specific)
    if sys.platform == "win32":
        # Windows: use layered window
        hwnd = pygame.display.get_wm_info()["window"]
        WS_EX_LAYERED = 0x80000
        WS_EX_TRANSPARENT = 0x20
        ctypes.windll.user32.SetWindowLongW(hwnd, -20,
            ctypes.windll.user32.GetWindowLongW(hwnd, -20) | WS_EX_LAYERED)
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0x00FF00, 0, 1)  # green key
    elif sys.platform == "darwin":
        # macOS: keep colored bg, handle via chroma surface
        pass

    # Chroma key color (green — not in pet palette)
    CHROMA = (0, 255, 0)

    clock = pygame.time.Clock()

    # State
    current_row = 0   # 0 = idle
    current_frame = 0
    frame_timer = 0.0
    temp_state = None
    temp_start = 0.0
    dragging = False
    drag_start = (0, 0)
    win_start = (0, 0)

    def get_row_fps(row):
        return ROWS[row][2]

    def switch_to(name):
        nonlocal current_row, current_frame, frame_timer
        for r, (n, frames, fps) in ROWS.items():
            if n == name:
                current_row = r
                current_frame = 0
                frame_timer = 0.0
                return

    def set_temp(name):
        nonlocal temp_state, temp_start
        temp_state = name
        temp_start = time.time()
        switch_to(name)

    switch_to("idle")
    print("Zest is here! (pygame cross-platform) 🐾")

    running = True
    last_mouse_x = 0

    while running:
        dt = clock.tick(FPS) / 1000.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # left
                    dragging = False
                    drag_start = pygame.mouse.get_pos()
                    win_pos = pygame.display.get_window_size()
                    # Get actual window position
                    try:
                        from pygame._sdl2 import Window as SDL2Window
                        sdl_win = SDL2Window.from_display_module()
                        win_start = sdl_win.position
                    except:
                        win_start = (0, 0)
                    last_mouse_x = pygame.mouse.get_pos()[0] + (win_start[0] if win_start else 0)
                elif event.button == 3:  # right
                    set_temp("failed")

            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1:
                    if not dragging:
                        set_temp("waving")
                    else:
                        dragging = False
                        temp_state = None
                        switch_to("idle")

            elif event.type == pygame.MOUSEMOTION:
                if pygame.mouse.get_pressed()[0]:
                    dx = event.rel[0]
                    dy = event.rel[1]
                    if abs(dx) > 1 or abs(dy) > 1:
                        if not dragging:
                            dragging = True
                            try:
                                from pygame._sdl2 import Window as SDL2Window
                                sdl_win = SDL2Window.from_display_module()
                                win_start = sdl_win.position
                            except:
                                win_start = (0, 0)
                            drag_start = pygame.mouse.get_pos()
                        # Move window
                        try:
                            from pygame._sdl2 import Window as SDL2Window
                            sdl_win = SDL2Window.from_display_module()
                            cur_pos = sdl_win.position
                            sdl_win.position = (cur_pos[0] + dx, cur_pos[1] + dy)
                        except:
                            pass
                        if dx > 1:
                            set_temp("running-right")
                        elif dx < -1:
                            set_temp("running-left")

        # Update frame
        row_fps = get_row_fps(current_row)
        frame_interval = 1.0 / row_fps
        frame_timer += dt
        if frame_timer >= frame_interval:
            frame_timer -= frame_interval
            frames = ROWS[current_row][1]
            current_frame = (current_frame + 1) % frames

        # Expire temp state
        if temp_state:
            dur = TEMP_DUR.get(temp_state, 2.0)
            if time.time() - temp_start >= dur:
                temp_state = None
                switch_to("idle")

        # Draw
        window.fill(CHROMA)
        surf = cells.get((current_row, current_frame))
        if surf is not None:
            if SCALE != 1.0:
                sw2 = int(CELL_W * SCALE)
                sh2 = int(CELL_H * SCALE)
                surf = pygame.transform.smoothscale(surf, (sw2, sh2))
            window.blit(surf, (0, 0))

        # Set colorkey for transparency
        window.set_colorkey(CHROMA)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
