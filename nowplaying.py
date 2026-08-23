"""bebop's Now Playing screen -- plain text + a progress bar, no
artwork (see albumart.py's Album Art Mode for the art-only screen).
Pulls status/currentsong fresh from MPD on every render, via
app.mpd.call() (see mpdclient.py) -- no locally-cached playback state,
so it can never drift from what MPD is actually doing.

Lines here are packed tight (font linesize + LINE_GAP), not the menu's
padded ROW_H -- these are plain info lines, not selectable/highlighted
rows, so they don't need a tap-target's breathing room.
"""
import sys
from pathlib import Path

import pygame
from mpd import MPDError

LINE_GAP = 8
BAR_HEIGHT = 20
BAR_GAP_ABOVE = 14
BAR_GAP_BELOW = 10


def format_mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class NowPlayingScreen:
    def __init__(self, app):
        self.app = app

    def move(self, step):
        # Left/Right -- bebop.py sends a page-sized step for consistency
        # with list screens, but only the sign matters here: previous/
        # next track, not "skip N."
        try:
            self.app.mpd.call("previous" if step < 0 else "next")
        except MPDError as exc:
            print(f"MPD previous/next failed: {exc}", file=sys.stderr)

    def select(self, app):
        # Enter -- play/pause. The brief's "OK toggles Now Playing /
        # Album Art Mode" is on the hamburger button instead (see
        # bebop.py's handle_keycode) -- Enter/OK was needed for
        # play/pause, so the toggle moved to the other free button.
        try:
            status = app.mpd.call("status")
            if status.get("state") == "play":
                app.mpd.call("pause", 1)
            else:
                app.mpd.call("play")
        except MPDError as exc:
            print(f"MPD play/pause failed: {exc}", file=sys.stderr)

    def render(self, renderer, canvas, color):
        f = renderer.font
        line_h = f.get_linesize() + LINE_GAP
        left = renderer.safe_rect.left
        right = renderer.safe_rect.right

        try:
            status = self.app.mpd.call("status")
            current = self.app.mpd.call("currentsong")
        except MPDError as exc:
            print(f"MPD status/currentsong failed: {exc}", file=sys.stderr)
            status, current = {}, {}

        state = status.get("state", "stop")
        icon = ">" if state == "play" else "||"
        y = renderer.draw_header(canvas, f"{icon} Now Playing", color)

        def line(text, centered=False):
            nonlocal y
            fitted = renderer.fit_text(f, text, renderer.safe_rect.width)
            surf = f.render(fitted, True, color)
            x = renderer.safe_rect.centerx - surf.get_width() // 2 if centered else left
            canvas.blit(surf, (x, y))
            y += line_h

        total = int(status.get("playlistlength", 0) or 0)
        if total and "song" in status:
            line(f'{int(status["song"]) + 1} of {total}')
        else:
            line("QUEUE EMPTY")

        title = current.get("title") or (Path(current["file"]).stem if current.get("file") else "--")
        line(title, centered=True)
        line(current.get("artist", ""), centered=True)
        line(current.get("album", ""), centered=True)

        y += BAR_GAP_ABOVE
        bar_rect = pygame.Rect(left, y, right - left, BAR_HEIGHT)
        pygame.draw.rect(canvas, color, bar_rect, 2, border_radius=BAR_HEIGHT // 2)
        elapsed = float(status.get("elapsed", 0) or 0)
        duration = float(status.get("duration", 0) or 0)
        frac = 0.0 if not duration else max(0.0, min(1.0, elapsed / duration))
        inset = 3
        fill_w = int((bar_rect.width - inset * 2) * frac)
        if fill_w > 0:
            pygame.draw.rect(
                canvas, color,
                (bar_rect.x + inset, bar_rect.y + inset, fill_w, BAR_HEIGHT - inset * 2),
                border_radius=(BAR_HEIGHT - inset * 2) // 2,
            )
        y = bar_rect.bottom + BAR_GAP_BELOW

        elapsed_surf = f.render(format_mmss(elapsed), True, color)
        remaining_surf = f.render("-" + format_mmss(duration - elapsed), True, color)
        canvas.blit(elapsed_surf, (left, y))
        canvas.blit(remaining_surf, (right - remaining_surf.get_width(), y))
