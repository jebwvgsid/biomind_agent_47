#!/usr/bin/env python3
"""Render captured welcome VT cells with the already installed emoji font.

This is a visualization of original terminal cells, not a desktop screenshot.
No glyph/text is substituted in evidence cells or raw PTY bytes.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cells', nargs='+', type=Path)
    args = parser.parse_args()
    source = Path(__file__).with_name('welcome-screen.py')
    spec = importlib.util.spec_from_file_location('welcome_screen', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    font_path = Path('/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf')
    emoji = ImageFont.truetype(str(font_path), 109)
    original_text = ImageDraw.ImageDraw.text

    def text_with_emoji(self, xy, text, *params, **options):
        if text and any(0x1F000 <= ord(char) <= 0x1FAFF for char in text):
            tile = Image.new('RGBA', (160, 160))
            painter = ImageDraw.Draw(tile)
            original_text(painter, (0, 0), text, font=emoji, embedded_color=True)
            bounds = tile.getbbox()
            if bounds:
                tile = tile.crop(bounds)
                ratio = min(20 / tile.width, 22 / tile.height)
                tile = tile.resize((max(1, round(tile.width * ratio)), max(1, round(tile.height * ratio))), Image.Resampling.LANCZOS)
                self._image.paste(tile, (round(xy[0]), round(xy[1]) + 3), tile)
                return
        return original_text(self, xy, text, *params, **options)

    ImageDraw.ImageDraw.text = text_with_emoji
    for path in args.cells:
        grid = json.loads(path.read_text())
        # JSON arrays represent original color tuples. Restore their type for
        # the reused rendering code without changing cell text or coordinates.
        def tuples(value):
            return tuple(tuples(item) for item in value) if isinstance(value, list) else value
        screen = module.Screen(len(grid[0]), len(grid))
        screen.grid = [[(cell[0], tuples(cell[1])) for cell in row] for row in grid]
        name = path.name.removesuffix('.cells.json') + '-emoji'
        screen.snapshot(path.parent, name)
        (path.parent / (name + '.render.json')).write_text(json.dumps({
            'source_cells': str(path), 'source_cells_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'renderer': str(Path(__file__).resolve()), 'renderer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'emoji_font': str(font_path), 'emoji_font_sha256': hashlib.sha256(font_path.read_bytes()).hexdigest(),
            'note': 'Re-rendered same actual VT cells with an installed emoji glyph fallback; original raw/snapshot/cursor metadata unchanged.'
        }, ensure_ascii=False, indent=2))
        print(path.parent / (name + '-dark.png'))


if __name__ == '__main__':
    main()
