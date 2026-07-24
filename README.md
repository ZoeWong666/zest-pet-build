# Zest Desktop Pet

A cute chihuahua desktop companion with 11 animations, evil mode, props, and Claude status integration.

## Quick Start (macOS)
```bash
python3 zest_pet.py
```

## Features
- 🐶 Normal mode: 9 standard animations + 16 look directions
- 😈 Evil mode: idle, running, look, angry, grin, smirk (right-click toggle)
- 🏠 Props: dog house (add PNGs to `decoded/props/`)
- 🤖 Claude status sync: pet reacts to Claude's state

## Adding New Animations
1. Generate 8-frame strip with `references/layout-guides/8frame.png`
2. Place in `decoded/`
3. Run frame extraction + normalization

## Windows
See `Zest.exe` or build from `zest_pet_pygame.py`
