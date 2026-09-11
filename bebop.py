#!/usr/bin/env python3
"""bebop -- an iPod-style MP3 player interface for composite Pi
console output. Entry point: ties config, the evdev/relay input path,
FrameBuffer output, and the menu-screen stack together. Same headless-
pygame + direct-/dev/fb0 + evdev architecture as bars.py/loudness.py/
channel38.py -- see bars.py's module docstring for the full rationale.
Runs inside its own venv (pygame-ce, not stock pygame -- see
/opt/bebop/venv, needed since pygame-ce and stock pygame can't coexist
in one interpreter and the rest of the fleet depends on stock pygame)
via the /usr/local/bin/bebop launcher.

bebop is a graphical MPD client only -- no audio decoding or library
indexing here, that's MPD's job (see mpdclient.py). Real Artists/
Playlists browsing and the Now-Playing/Album-Art OK-toggle are still
open (see project memory); Songs, Settings, and real playback control
are wired.
"""
import fcntl
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

import evdev
from evdev import ecodes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")

import pygame  # noqa: E402  (must come after SDL env vars are set)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Config, Settings  # noqa: E402
from display import FrameBuffer, Renderer, pixel_aspect_correction  # noqa: E402
from mpdclient import Client as MPDClientWrapper  # noqa: E402
from mpd import MPDError  # noqa: E402
from nowplaying import NowPlayingScreen  # noqa: E402
from albumart import AlbumArtScreen  # noqa: E402
import menu  # noqa: E402

VERSION = menu.VERSION

ORANGE = (0xFF, 0xA5, 0x00)  # matches the fleet's other splash version readouts
# VCR OSD Mono, not bebop's own ChicagoKare -- per explicit user request
# 2026-09-11, to match the other apps' splash version line font-for-font
# (an earlier version of this used ChicagoKare for UI consistency with
# the rest of bebop; overridden by this follow-up). Copied in from
# /opt/bars's copy -- not otherwise used anywhere else in bebop.
SPLASH_VERSION_FONT_PATH = Path(__file__).resolve().parent / "assets" / "VCR_OSD_MONO_1.001.ttf"
SPLASH_VERSION_FONT_SIZE = 22
SPLASH_VERSION_GAP = 20  # pixels between the bottom of the splash image and the version text

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

# See bars.py/menu.py -- Home exits with this so STRINGS/the app-menu
# knows to hand off to the health/app-menu screen instead of redrawing
# this app's own screen.
EXIT_GOTO_HOME = 42

MOUSE_MOVE_THRESHOLD = 12


def find_keyboard_devices():
    # See bars.py for the full rationale. This picks up both a real
    # remote (dongle plugged directly into production during this dev
    # phase) and, once bebop moves to McBrain, STRINGS's virtual relay
    # device -- identical code path either way, no bebop-side relay
    # integration needed.
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        print("No keyboard input device found -- running headless/unattended.", file=sys.stderr)
    return devices


def show_splash(fb, config):
    """Matches the fleet's other splash screens (bars.py/loudness.py/
    channel38.py/joanjett's main.py): the image plus a centered orange
    VERSION line underneath it. Unlike those apps' native-resolution
    splashes, bebop's source art is scale-to-fit within the safe area
    (see the existing pixel_aspect_correction() comment below) -- so
    the version line's height is reserved from that same safe area
    *before* the image is scaled, rather than added as a fixed pixel
    offset, and the whole image+text block is then centered together."""
    if not config.splash_path.exists():
        return
    try:
        img = pygame.image.load(str(config.splash_path)).convert()
    except (pygame.error, OSError) as exc:
        print(f"Splash load failed: {exc}", file=sys.stderr)
        return
    canvas = pygame.Surface((config.width, config.height))
    canvas.fill((0, 0, 0))
    img_w, img_h = img.get_size()

    version_font = pygame.font.Font(str(SPLASH_VERSION_FONT_PATH), SPLASH_VERSION_FONT_SIZE)
    version_surf = version_font.render(f"VERSION {VERSION}", True, ORANGE)

    # Fit within the safe area, not the full frame -- the splash was
    # bleeding past the top/bottom of the CRT's visible picture the
    # same as any other content would outside the underscan margin.
    margin_x = int(config.width * config.safe_area)
    margin_y = int(config.height * config.safe_area)
    safe_w = config.width - 2 * margin_x
    safe_h = config.height - 2 * margin_y - SPLASH_VERSION_GAP - version_surf.get_height()
    # Pre-stretch horizontally for the buffer's non-square pixels (see
    # pixel_aspect_correction()) before fitting to the safe area, so the
    # final on-screen shape matches the source art's real proportions.
    corrected_w = img_w * pixel_aspect_correction(config)
    scale = min(safe_w / corrected_w, safe_h / img_h)
    scaled = pygame.transform.smoothscale(img, (int(corrected_w * scale), int(img_h * scale)))

    block_h = scaled.get_height() + SPLASH_VERSION_GAP + version_surf.get_height()
    img_y = (config.height - block_h) // 2
    canvas.blit(scaled, ((config.width - scaled.get_width()) // 2, img_y))
    text_y = img_y + scaled.get_height() + SPLASH_VERSION_GAP
    canvas.blit(version_surf, ((config.width - version_surf.get_width()) // 2, text_y))

    fb.write_surface(canvas)
    time.sleep(config.splash_seconds)


class BebopApp:
    def __init__(self):
        # pygame/SDL installs its own SIGINT/SIGTERM handler that turns
        # the signal into an SDL_QUIT event; since input is read via
        # evdev and nothing drains pygame's event queue, that would
        # silently swallow both signals. Install plain handlers so the
        # process still terminates normally -- see bars.py.
        self._quit_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self.config = Config()
        self.settings = Settings()
        self.mpd = MPDClientWrapper(self.config.mpd_host, self.config.mpd_port)
        # MPD has no equivalent of bebop's 3-way Shuffle mode (just its
        # own 2-state random flag) -- settings.ini is the durable
        # default, pushed onto MPD here so it takes effect even if this
        # session never opens Settings. See menu.py.
        menu.apply_startup_playback_settings(self)

        pygame.init()
        pygame.display.set_mode((self.config.width, self.config.height))  # headless (dummy driver); needed for .convert()

        self.fb = FrameBuffer()

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ)

        # Tell the console driver to stop drawing its own cursor/text
        # over our framebuffer writes. Only possible on a real VT (not
        # over SSH, and not without any controlling terminal at all) --
        # see bars.py.
        self.tty_fd = None
        self.console_graphics_mode = False
        try:
            self.tty_fd = os.open("/dev/tty", os.O_RDWR)
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)
            self.console_graphics_mode = True
        except OSError as exc:
            print(f"Console graphics mode not available: {exc}", file=sys.stderr)

        show_splash(self.fb, self.config)

        self.renderer = Renderer(self.config)
        self.stack = [menu.build_root_menu(self)]

        self.pending_exit_code = 0
        self._rel_accum = {"x": 0, "y": 0}

    def _handle_signal(self, signum, frame):
        self._quit_requested = True

    # -- screen stack -------------------------------------------------

    def push_screen(self, screen):
        self.stack.append(screen)

    def pop_screen(self):
        if len(self.stack) > 1:
            self.stack.pop()

    @property
    def current_screen(self):
        return self.stack[-1]

    def _toggle_album_art(self):
        # Swaps the top of the stack in place rather than push/pop --
        # Now Playing and Album Art are sibling views of the same
        # "what's playing" state, not a hierarchy, so Back from either
        # one should return to wherever Now Playing was entered from
        # (e.g. the Songs list) in a single step, not bounce to the
        # other view first.
        if isinstance(self.current_screen, AlbumArtScreen):
            self.stack[-1] = NowPlayingScreen(self)
        else:
            self.stack[-1] = AlbumArtScreen(self)

    def _toggle_play_pause(self):
        # Shared by Down (see handle_keycode) and Enter on
        # NowPlayingScreen (nowplaying.py's select()) -- Down works on
        # both Now Playing and Album Art, Enter only on Now Playing
        # (Album Art's select() stays a no-op, matching its "no
        # navigation" design).
        try:
            status = self.mpd.call("status")
            if status.get("state") == "play":
                self.mpd.call("pause", 1)
            else:
                self.mpd.call("play")
        except MPDError as exc:
            print(f"MPD play/pause failed: {exc}", file=sys.stderr)

    def _set_line_out_mute(self, muted):
        # VOL-/VOL+ are labeled for a normal volume knob, but this rig
        # has no software volume control -- MPD's own mixer is
        # deliberately "disabled" (see mpd.conf) so its ALSA PCM level
        # IS the real line-level output, same fixed level set fleet-wide
        # by set-audio-levels.service. So VOL- is really a MUTE button
        # and VOL+ is really SOUND ON: toggling ALSA PCM's playback
        # switch (mute) rather than its volume (which would need to be
        # explicitly restored afterward and could drift from the
        # enforced known-good level). MPD keeps playing/decoding the
        # whole time -- this only silences the hardware output, unlike
        # Down's play/pause which actually stops MPD. Card 0 always --
        # this Pi has exactly one playback device (see aplay -l).
        try:
            subprocess.run(
                ["amixer", "-c", "0", "sset", "PCM", "mute" if muted else "unmute"],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"amixer PCM {'mute' if muted else 'unmute'} failed: {exc}", file=sys.stderr)

    # -- rendering ------------------------------------------------------

    def render(self):
        canvas = self.renderer.new_canvas()
        self.current_screen.render(self.renderer, canvas, self.settings.color)
        self.fb.write_surface(canvas)

    # -- input ------------------------------------------------------

    def handle_keycode(self, code):
        """Returns True if the app should redraw after this key, "quit"/
        "quit_home" for the special exit paths, or False if unhandled."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME):
            return "quit_home"
        elif code == ecodes.KEY_COMPOSE and isinstance(self.current_screen, (NowPlayingScreen, AlbumArtScreen)):
            # Hamburger's normal fleet-wide meaning ("open the app
            # menu") doesn't apply inside bebop -- repurposed here,
            # only on these two screens, for the brief's "OK toggles
            # Now Playing / Album Art Mode" (moved off OK/Enter, which
            # already means play/pause). Everywhere else in bebop,
            # hamburger still falls through to the normal quit case
            # right below, matching every other app's convention.
            self._toggle_album_art()
        elif code == ecodes.KEY_UP and isinstance(self.current_screen, (NowPlayingScreen, AlbumArtScreen)):
            # Up toggles Album Art Mode on these two screens (2026-08-23
            # -- see git history for the original Hamburger-unreachable-
            # via-relay rationale). Down used to share this binding too,
            # but is now play/pause instead (2026-08-28, remote-control
            # redesign) -- Left/Right take over track-skip duty on both
            # screens below via move(), so Up/Down/Left/Right are fully
            # dedicated to playback control here, matching a real
            # iPod/media-remote layout.
            self._toggle_album_art()
        elif code == ecodes.KEY_DOWN and isinstance(self.current_screen, (NowPlayingScreen, AlbumArtScreen)):
            self._toggle_play_pause()
        elif code in (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_COMPOSE):
            return "quit"
        elif code == ecodes.KEY_UP:
            self.current_screen.move(-1)
        elif code == ecodes.KEY_DOWN:
            self.current_screen.move(1)
        elif code == ecodes.KEY_LEFT:
            self.current_screen.move(-self.current_screen.page_step())
        elif code == ecodes.KEY_RIGHT:
            self.current_screen.move(self.current_screen.page_step())
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            self.current_screen.select(self)
        elif code == ecodes.KEY_BACK:
            # True iPod behavior: Back goes up one menu level, and is a
            # no-op at the root -- there's nowhere higher to go.
            # Exiting bebop entirely is Home/Q/Esc, handled above.
            self.pop_screen()
        elif code == ecodes.KEY_VOLUMEDOWN:
            # Mute the line-level output -- works from any screen, not
            # just Now Playing/Album Art (see _set_line_out_mute).
            self._set_line_out_mute(True)
            return False
        elif code == ecodes.KEY_VOLUMEUP:
            self._set_line_out_mute(False)
            return False
        else:
            return False
        return True

    def handle_rel_event(self, code, value):
        # See bars.py -- translates the remote's air-mouse-mode movement
        # into the same discrete direction presses its keyboard-mode
        # D-pad sends.
        if code == ecodes.REL_X:
            axis = "x"
        elif code == ecodes.REL_Y:
            axis = "y"
        else:
            return False
        self._rel_accum[axis] += value
        accum = self._rel_accum[axis]
        if abs(accum) < MOUSE_MOVE_THRESHOLD:
            return False
        self._rel_accum[axis] = 0
        if axis == "x":
            synthetic = ecodes.KEY_RIGHT if accum > 0 else ecodes.KEY_LEFT
        else:
            synthetic = ecodes.KEY_DOWN if accum > 0 else ecodes.KEY_UP
        return self.handle_keycode(synthetic)

    # -- main loop ------------------------------------------------------

    def run(self):
        try:
            self.render()
            running = True
            while running and not self._quit_requested:
                # Now Playing/Album Art reflect MPD's own live state
                # (elapsed time, natural track-end auto-advance) rather
                # than anything driven by a keypress -- a shorter
                # timeout here keeps them visibly current even with
                # zero input, where every other (static) screen only
                # ever needs to redraw in response to a key.
                live_screen = isinstance(self.current_screen, (NowPlayingScreen, AlbumArtScreen))
                for key, _ in self.selector.select(timeout=0.5 if live_screen else 1):
                    device = key.fileobj
                    try:
                        events = list(device.read())
                    except OSError as exc:
                        # A device can vanish mid-session (remote
                        # dongle unplugged/replugged, a USB glitch, or
                        # -- confirmed while testing -- a relay/synthetic
                        # input device torn down elsewhere) -- drop just
                        # this one device rather than taking bebop down;
                        # every other registered device keeps working.
                        print(f"Input device {device.path} gone ({exc}), dropping it: still running", file=sys.stderr)
                        try:
                            self.selector.unregister(device)
                        except KeyError:
                            pass
                        continue
                    for event in events:
                        if event.type == ecodes.EV_KEY and event.value == 1:  # 1 == key down
                            result = self.handle_keycode(event.code)
                        elif event.type == ecodes.EV_REL:
                            result = self.handle_rel_event(event.code, event.value)
                        else:
                            continue
                        if result == "quit":
                            running = False
                        elif result == "quit_home":
                            running = False
                            self.pending_exit_code = EXIT_GOTO_HOME
                        elif result:
                            self.render()
                    if not running:
                        break
                if not running:
                    break
                if live_screen:
                    self.render()
        finally:
            # Pause MPD on the way out, for every exit path (Home/Q/Esc
            # above, and a SIGTERM from STRINGS mid-restart/reassign via
            # _handle_signal -- both funnel through this same finally
            # block since the loop just drops out of `while running`).
            # MPD is a separate daemon and keeps playing regardless of
            # whether bebop itself is on screen, so without this,
            # audio carries on through the terminal/next-app transition.
            # Only pauses if actually playing -- calling "pause 1" while
            # already stopped is an MPD protocol error, not a no-op.
            try:
                status = self.mpd.call("status")
                if status.get("state") == "play":
                    self.mpd.call("pause", 1)
            except MPDError as exc:
                print(f"MPD pause-on-exit failed: {exc}", file=sys.stderr)

            self.fb.close()
            if self.console_graphics_mode:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[2J\033[H")  # clear screen, home cursor
            if self.tty_fd is not None:
                os.close(self.tty_fd)
            pygame.quit()

        sys.exit(self.pending_exit_code)


def main():
    BebopApp().run()


if __name__ == "__main__":
    main()
