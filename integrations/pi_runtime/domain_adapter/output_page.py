"""Bound an existing text page before calculating its native offset/cursor."""
def page_text(text, limits):
    if not limits:
        return text
    used = 0
    lines = 1
    end = 0
    for char in text:
        size = len(char.encode('utf-8'))
        if used + size > limits['maxBytes'] or lines > limits['maxLines']:
            break
        used += size
        end += 1
        if char == '\n':
            lines += 1
    return text[:end]
