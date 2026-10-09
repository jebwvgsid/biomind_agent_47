"""Existing VT cell reconstruction reused for welcome PTY evidence.
Source: /public/home/liusiqi/03.毕设/Biomind_v1_generate_impl_20260921/tests/cli_visual_pty.py
Original SHA256: 8197feac392b0267ba77e24e0926a7bf77da23f4a67f9b1cff5b7b7ca0c9c50c
No product terminal or editor replacement. Adapter changes: modern keyboard
protocol CSI private markers (<>=) parse as parameters; CSI u keyboard protocol
and CSI c Device Attributes query are nonvisual and receive no synthetic reply.
"""
import re
import json

class Screen:
    """VT cursor/erase and ANSI palette cells, with unsupported controls logged."""
    def __init__(self, cols, rows):
        from wcwidth import wcwidth
        self.width = wcwidth
        self.cols, self.rows, self.x, self.y = cols, rows, 0, 0
        self.style = (39, False, False, 49, False, False)
        self.grid = [[(' ', self.style)] * cols for _ in range(rows)]
        self.pending, self.unknown = '', []
    def down(self):
        self.y += 1
        if self.y >= self.rows:
            self.grid.pop(0)
            self.grid.append([(' ', (39, False, False, 49, False, False))] * self.cols)
            self.y = self.rows - 1
    def feed(self, text):
        self.pending += text
        replies = []
        while self.pending:
            if self.pending.startswith('\x1b['):
                match = re.match(r'\x1b\[([0-?]*)([ -/]*)([@-~])', self.pending)
                if not match:
                    break
                args, inter, op = match.groups()
                self.pending = self.pending[match.end():]
                nums = [int(v) if v else 0 for v in args.lstrip('?<>=').split(';')]
                n = nums[0] or 1
                if op == 'm':
                    color, bold, dim, background, reverse, underline = self.style
                    values = iter(nums)
                    for value in values:
                        if value == 0: color, bold, dim, background, reverse, underline = 39, False, False, 49, False, False
                        elif value == 1: bold = True
                        elif value == 2: dim = True
                        elif value == 22: bold, dim = False, False
                        elif 30 <= value <= 37 or 90 <= value <= 97 or value == 39: color = value
                        elif 40 <= value <= 47 or value == 49: background = value
                        elif value in (38, 48):
                            mode = next(values, None)
                            if mode == 5: paint = ('palette', next(values, 0))
                            elif mode == 2: paint = ('rgb', *(next(values, 0) for _ in range(3)))
                            else: self.unknown.append(f'SGR color mode {mode}'); continue
                            if value == 38: color = paint
                            else: background = paint
                        elif value in (7, 27): reverse = value == 7
                        elif value in (4, 24): underline = value == 4
                        elif value not in (3, 23): self.unknown.append(f'SGR {value}')
                    self.style = color, bold, dim, background, reverse, underline
                elif op == 'A': self.y = max(0, self.y - n)
                elif op == 'B': self.y = min(self.rows - 1, self.y + n)
                elif op == 'C': self.x = min(self.cols - 1, self.x + n)
                elif op == 'D': self.x = max(0, self.x - n)
                elif op in 'Hf':
                    self.y = min(self.rows - 1, (nums[0] or 1) - 1)
                    self.x = min(self.cols - 1, (nums[1] if len(nums) > 1 and nums[1] else 1) - 1)
                elif op == 'G': self.x = min(self.cols - 1, n - 1)
                elif op == 'K':
                    left = 0 if nums[0] in (1, 2) else self.x
                    right = self.cols if nums[0] in (0, 2) else self.x + 1
                    self.grid[self.y][left:right] = [(' ', self.style)] * (right - left)
                elif op == 'J':
                    if nums[0] in (2, 3): self.grid = [[(' ', self.style)] * self.cols for _ in range(self.rows)]
                    elif nums[0] == 0:
                        self.grid[self.y][self.x:] = [(' ', self.style)] * (self.cols - self.x)
                        for row in range(self.y + 1, self.rows): self.grid[row] = [(' ', self.style)] * self.cols
                    else: self.unknown.append(match.group())
                elif op == 'n' and nums[0] == 6: replies.append(f'\x1b[{self.y+1};{self.x+1}R')
                elif op in 'hlquc': pass
                else: self.unknown.append(match.group())
                continue
            if self.pending[0] == '\x1b':
                if len(self.pending) < 2: break
                op = self.pending[:2]
                self.pending = self.pending[2:]
                if op not in ('\x1b=', '\x1b>'): self.unknown.append(op)
                continue
            char, self.pending = self.pending[0], self.pending[1:]
            if char == '\r': self.x = 0
            elif char == '\n': self.down()
            elif char == '\b': self.x = max(0, self.x - 1)
            elif ord(char) < 32: pass
            else:
                width = max(0, self.width(char))
                if not width:
                    if self.x: self.grid[self.y][self.x-1] = (self.grid[self.y][self.x-1][0] + char, self.style)
                    continue
                if self.x + width > self.cols: self.x = 0; self.down()
                self.grid[self.y][self.x] = (char, self.style)
                if width == 2: self.grid[self.y][self.x+1] = ('', self.style)
                self.x += width
        return replies
    def text(self):
        return '\n'.join(''.join(cell[0] for cell in row).rstrip() for row in self.grid)
    def snapshot(self, folder, name):
        from PIL import Image, ImageDraw, ImageFont
        (folder / (name + '.txt')).write_text(self.text())
        (folder / (name + '.cells.json')).write_text(json.dumps(self.grid, ensure_ascii=False))
        regular = ImageFont.truetype('/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc', 18, index=7)
        bold = ImageFont.truetype('/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc', 18, index=7)
        for theme, background, foreground, muted, accent in (
            ('dark', '#161b20', '#e5e9ee', '#8b969f', '#6bc4cb'),
            ('light', '#fafbf9', '#263b36', '#737d78', '#20747b')):
            image = Image.new('RGB', (self.cols * 10 + 32, self.rows * 26 + 32), background)
            draw = ImageDraw.Draw(image)
            def paint(value, default):
                if isinstance(value, tuple):
                    if value[0] == 'rgb': return tuple(value[1:])
                    n = value[1]
                    if n >= 232: return (8 + (n-232)*10,) * 3
                    if n >= 16:
                        n -= 16
                        ramp = [0, 95, 135, 175, 215, 255]
                        return ramp[n//36], ramp[n//6%6], ramp[n%6]
                    value = 30 + n % 8
                return {31: '#d2574f', 33: '#b58a35', 32: '#55a574', 36: accent}.get(value, default)
            for y, row in enumerate(self.grid):
                for x, (char, (color, weight, dim, back, reverse, underline)) in enumerate(row):
                    ink = paint(color, muted if dim else foreground)
                    paper = paint(back, background)
                    if reverse: ink, paper = paper, ink
                    draw.rectangle((16+x*10, 10+y*26, 16+(x+1)*10, 36+y*26), fill=paper)
            for y, row in enumerate(self.grid):
                for x, (char, (color, weight, dim, back, reverse, underline)) in enumerate(row):
                    ink = paint(back, background) if reverse else paint(color, muted if dim else foreground)
                    draw.text((16+x*10, 10+y*26), char, fill=ink, font=bold if weight else regular)
                    if underline: draw.line((16+x*10, 34+y*26, 26+x*10, 34+y*26), fill=ink)
            image.save(folder / f'{name}-{theme}.png')
