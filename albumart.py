"""bebop's Album Art Mode -- a dedicated fullscreen screen showing only
square artwork, centered on the complete framebuffer. Per the brief,
this deliberately ignores the safe area (menu screens' underscan
margin doesn't apply here) and draws nothing else -- no title, no
progress bar, no borders, no status text.

Pulls the currently-playing track's art fresh from MPD/artwork.py on
every render (same "no locally-cached playback state" approach as
nowplaying.py) -- so if the track changes while sitting on this screen
(natural advance, or a skip from elsewhere), the art updates too, not
just when the screen is first entered. Falls back to bebop's own
"no album art" placeholder image when no real artwork is found, per
the user's explicit request (2026-08-23) -- supersedes the brief's
original plain-black fallback, since they supplied dedicated
placeholder art.
"""
from pathlib import Path

import pygame
from mpd import MPDError

from display import pixel_aspect_correction
from artwork import get_artwork

BLACK = (0, 0, 0)
NO_ART_PATH = Path(__file__).resolve().parent / "assets" / "no_album_art.png"

_no_art_cache = None


def _load_no_art():
    global _no_art_cache
    if _no_art_cache is None:
        _no_art_cache = pygame.image.load(str(NO_ART_PATH)).convert()
    return _no_art_cache


class AlbumArtScreen:
    def __init__(self, app):
        self.app = app

    def move(self, step):
        pass  # no navigation inside Album Art Mode -- hamburger toggles back to Now Playing

    def select(self, app):
        pass

    def render(self, renderer, canvas, color):
        # Deliberately ignores renderer.safe_rect and the menu text
        # `color` entirely -- see the module docstring. canvas already
        # starts fully black from renderer.new_canvas().
        try:
            current = self.app.mpd.call("currentsong")
        except MPDError:
            current = {}

        img = None
        if current.get("file"):
            img = get_artwork(current["file"], self.app.config)
        if img is None:
            img = _load_no_art()

        config = self.app.config
        side = config.artwork_size  # physical square side (both box dimensions -- NOT pre-widened)
        img_w, img_h = img.get_size()
        # Same technique as bebop.py's splash, in the same order: pre-
        # stretch the SOURCE image's width by the correction factor
        # first, then fit that (now wider) rectangle into a plain,
        # uncorrected side x side box, preserving its aspect ratio.
        corrected_w = img_w * pixel_aspect_correction(config)
        scale = min(side / corrected_w, side / img_h)
        draw_w, draw_h = int(corrected_w * scale), int(img_h * scale)
        scaled = pygame.transform.smoothscale(img, (draw_w, draw_h))
        x = (canvas.get_width() - draw_w) // 2
        y = (canvas.get_height() - draw_h) // 2
        canvas.blit(scaled, (x, y))
