"""Direct-/dev/fb0 rendering primitives for bebop, matching the rest of
the fleet's headless-pygame architecture (see bars.py's module
docstring for the full rationale). This module owns the FrameBuffer
writer and a Renderer that draws bebop's iPod-style chrome (headers,
menu rows, chevrons) against a logical config.width x config.height
canvas -- FrameBuffer's own write_surface() scales that onto whatever
the real hardware resolution is, same as every other app.
"""
import mmap
import os
from pathlib import Path

import numpy as np
import pygame

BLACK = (0, 0, 0)

# The composite buffer isn't square-pixel -- displayed on a real 4:3
# CRT, the buffer's own raw pixel ratio (e.g. 720/480 = 1.5) is wider
# than the screen's physical 4:3 (1.333), so each buffer pixel is
# physically narrower than it is tall. Any art/image asset authored in
# a normal editor (square pixels) needs pre-stretching horizontally by
# this factor before it's drawn into the buffer, or it reads squished
# on the real CRT -- confirmed on real hardware via the splash screen.
# Used by both bebop.py's splash and albumart.py's Album Art Mode.
DISPLAY_ASPECT = 4 / 3


def pixel_aspect_correction(config):
    return (config.width / config.height) / DISPLAY_ASPECT


MAX_FONT_SIZE = 64
MIN_FONT_SIZE = 8
# One size for the whole interface (header, menu rows, and info text
# all use it) -- the user's explicit call after seeing the two-size
# version: "bebop" was reading larger than the menu text, so rather
# than tune two sizes to match, everything just uses this one target
# column budget now.
TARGET_WIDTH = 18


def fit_font(font_path, target_width_chars, usable_w, max_size=MAX_FONT_SIZE, min_size=MIN_FONT_SIZE):
    # Picks the largest point size whose 'M' width times target_width_chars
    # still fits usable_w pixels -- verbatim technique from channel38.py's
    # fit_font(), duplicated per this project's no-shared-library
    # convention. A hardcoded point size doesn't track how many actual
    # pixels a TTF's glyphs occupy at a given real resolution.
    for size in range(max_size, min_size - 1, -1):
        candidate = pygame.font.Font(str(font_path), size)
        if candidate.size("M")[0] * target_width_chars <= usable_w:
            return candidate
    return pygame.font.Font(str(font_path), min_size)


class FrameBuffer:
    """Verbatim copy of bars.py's FrameBuffer -- see that file for the
    full rationale. Geometry is read from sysfs at open time since it
    depends on whichever output (composite/HDMI) is currently active.
    """

    def __init__(self, dev="/dev/fb0"):
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
        self.bpp = int((sys_dir / "bits_per_pixel").read_text())
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

    def write_surface(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
        if self.bpp == 16:
            r = arr[:, :, 0].astype(np.uint16) >> 3
            g = arr[:, :, 1].astype(np.uint16) >> 2
            b = arr[:, :, 2].astype(np.uint16) >> 3
            raw = ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
        else:
            alpha = np.zeros((self.height, self.width, 1), dtype=np.uint8)
            raw = np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()

        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def close(self):
        self.mm.close()
        os.close(self.fd)


class Renderer:
    """Draws bebop's iPod-style chrome onto a logical config.width x
    config.height canvas. Menu screens are drawn within the configured
    safe area; Album Art Mode (added in a later step) is the one thing
    that deliberately ignores it and draws on the full canvas instead.
    """

    def __init__(self, config):
        self.w = config.width
        self.h = config.height
        margin_x = int(self.w * config.safe_area)
        margin_y = int(self.h * config.safe_area)
        self.safe_rect = pygame.Rect(margin_x, margin_y, self.w - 2 * margin_x, self.h - 2 * margin_y)

        usable_w = self.safe_rect.width
        # One Font instance, reused under all three names -- every
        # screen (header, menu rows, static info text) reads the same
        # size. Kept as three attributes rather than collapsing the
        # call sites in menu.py, in case a deliberate size difference
        # is wanted again later.
        self.font = fit_font(config.font_path, TARGET_WIDTH, usable_w)
        self.header_font = self.font
        self.row_font = self.font
        self.small_font = self.font

        # Row height/chevron size scale off the actual chosen row font
        # rather than a hardcoded pixel count, so they track whatever
        # fit_font() picked for this resolution/safe-area combination.
        self.ROW_H = self.row_font.get_linesize() + 16
        self.chevron_size = max(6, self.row_font.get_height() // 5)

    def new_canvas(self):
        canvas = pygame.Surface((self.w, self.h))
        canvas.fill(BLACK)
        return canvas

    def draw_header(self, canvas, text, color):
        surf = self.header_font.render(text, True, color)
        x = self.safe_rect.centerx - surf.get_width() // 2
        y = self.safe_rect.top
        canvas.blit(surf, (x, y))
        underline_y = y + surf.get_height() + 4
        # Filled rect, not draw.line -- guarantees exactly 2 solid pixel
        # rows regardless of how draw.line's width parameter rounds a
        # thick line's footprint. A single-scanline-thin line only gets
        # refreshed on one of the two interlaced fields each frame and
        # visibly flickers on a real CRT (same issue/fix as
        # scrutinizer.py's panel borders -- see project memory).
        pygame.draw.rect(canvas, color, (self.safe_rect.left, underline_y, self.safe_rect.width, 2))
        return underline_y + 12  # y where row content can start

    def fit_text(self, font, text, max_width):
        """Returns `text` unchanged if it already fits max_width in
        font, otherwise trims characters off the end and appends an
        ellipsis until it does. Uses "..." (plain ASCII) rather than a
        real "…" glyph -- same reasoning as draw_chevron drawing a
        shape instead of trusting a font glyph: don't risk Chicago
        Kare not covering it."""
        if font.size(text)[0] <= max_width:
            return text
        ellipsis = "..."
        trimmed = text
        while trimmed and font.size(trimmed + ellipsis)[0] > max_width:
            trimmed = trimmed[:-1]
        return (trimmed + ellipsis) if trimmed else ellipsis

    def draw_chevron(self, canvas, right_x, center_y, color):
        # Drawn as a plain triangle rather than relying on a font glyph
        # -- guarantees it matches the mockup regardless of Chicago
        # Kare's actual glyph coverage. Scales with the row font, see
        # self.chevron_size in __init__.
        size = self.chevron_size
        points = [
            (right_x - size, center_y - size),
            (right_x, center_y),
            (right_x - size, center_y + size),
        ]
        pygame.draw.polygon(canvas, color, points)

    def draw_menu_rows(self, canvas, items, selected, top_y, color, chevrons=True):
        """items: list of label strings. Draws each as a full-width row,
        the selected one highlighted (filled bar, black text), a
        trailing chevron on every row if chevrons=True. Whole screen is
        re-rendered every frame -- no partial-redraw bookkeeping needed
        at this list size."""
        y = top_y
        # Reserve room for the chevron (plus its own left/right padding)
        # so a long label truncates before it would run under/past it,
        # rather than overlapping -- matches the plain 8px right margin
        # when there's no chevron.
        text_max_w = self.safe_rect.width - 8 - (8 + self.chevron_size * 2 + 8 if chevrons else 8)
        for i, label in enumerate(items):
            row_rect = pygame.Rect(self.safe_rect.left, y, self.safe_rect.width, self.ROW_H)
            is_selected = i == selected
            if is_selected:
                pygame.draw.rect(canvas, color, row_rect)
            text_color = BLACK if is_selected else color
            fitted = self.fit_text(self.row_font, label, text_max_w)
            text_surf = self.row_font.render(fitted, True, text_color)
            text_y = row_rect.centery - text_surf.get_height() // 2
            canvas.blit(text_surf, (row_rect.left + 8, text_y))
            if chevrons:
                self.draw_chevron(canvas, row_rect.right - 8, row_rect.centery, text_color)
            y += self.ROW_H
