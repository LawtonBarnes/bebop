"""bebop's menu screens -- a small stack-based navigation model. Each
screen knows how to render itself and what Up/Down/Select do to it;
bebop.py holds the stack (app.stack) and dispatches input to whichever
screen is on top. Back is handled once, centrally, in bebop.py's
handle_keycode (pop the stack / no-op at the root) rather than per
screen, since that behavior never varies by screen type.

Songs, Artists, Playlists, and Settings' Shuffle/Repeat are all real
(MPD-backed) -- MPD's own state is the single source of truth
everywhere except Shuffle/Repeat's 3-way mode, which MPD has no
equivalent concept of, so bebop's settings.ini is the durable default
for that (see apply_startup_playback_settings).
"""
import random
import shutil
import sys

from mpd import MPDError

from config import REPEAT_MODES, SHUFFLE_MODES, TEXT_COLORS
from nowplaying import NowPlayingScreen

VERSION = "1.0"  # bebop's own version, distinct from each app's VERSION convention elsewhere in the fleet

ROOT_ITEMS = ["Playlists", "Artists", "Songs", "Settings"]
SETTINGS_ITEMS = ["Shuffle", "Repeat", "Text Color", "About"]


class ListScreen:
    """A plain vertical list of selectable rows -- the workhorse for
    every menu bebop has (root menu, Settings, the three radio pickers,
    Songs, and Playlists/Artists' placeholder screens all reuse this).
    Scrolls to keep the selected row visible -- needed for real library
    lists (1000+ songs), harmless for the handful of 4-item menus."""

    def __init__(self, title, items, on_select=None, chevrons=True):
        self.title = title
        self.items = items  # list of label strings
        self.selected = 0
        self.scroll = 0
        self.on_select = on_select  # callback(app, index) or None
        self.chevrons = chevrons

    def move(self, step):
        # Up/Down send step=+-1; Left/Right send a larger page step
        # (see bebop.py's PAGE_STEP) for fast-scrolling long lists --
        # both just fall through the same wraparound move.
        if not self.items:
            return
        self.selected = (self.selected + step) % len(self.items)

    def select(self, app):
        if self.on_select and self.items:
            self.on_select(app, self.selected)

    def render(self, renderer, canvas, color):
        top_y = renderer.draw_header(canvas, self.title, color)
        if not self.items:
            empty = renderer.small_font.render("NOTHING HERE YET", True, color)
            canvas.blit(empty, (renderer.safe_rect.centerx - empty.get_width() // 2, top_y + 20))
            return
        visible_rows = max(1, (renderer.safe_rect.bottom - top_y) // renderer.ROW_H)
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + visible_rows:
            self.scroll = self.selected - visible_rows + 1
        visible_items = self.items[self.scroll:self.scroll + visible_rows]
        renderer.draw_menu_rows(canvas, visible_items, self.selected - self.scroll, top_y, color, chevrons=self.chevrons)


class InfoScreen:
    """A static, non-selectable block of text -- used for About."""

    def __init__(self, title, lines):
        self.title = title
        self.lines = lines  # list of strings

    def move(self, step):
        pass

    def select(self, app):
        pass

    def render(self, renderer, canvas, color):
        top_y = renderer.draw_header(canvas, self.title, color)
        y = top_y + 10
        for line in self.lines:
            surf = renderer.row_font.render(line, True, color)
            canvas.blit(surf, (renderer.safe_rect.left + 8, y))
            y += renderer.ROW_H


def build_root_menu(app):
    return ListScreen("bebop", ROOT_ITEMS, on_select=_root_select, chevrons=True)


def _root_select(app, index):
    label = ROOT_ITEMS[index]
    if label == "Settings":
        app.push_screen(build_settings_menu(app))
    elif label == "Songs":
        app.push_screen(build_songs_screen(app))
    elif label == "Artists":
        app.push_screen(build_artists_screen(app))
    elif label == "Playlists":
        app.push_screen(build_playlists_screen(app))


def _shuffle_by_album(entries):
    """Groups entries by (albumartist-or-artist, album), preserving
    each group's existing internal order (already track-sorted where
    that matters -- see _track_sort_key), then shuffles which group
    comes next. Individual albums/playlists still play straight
    through internally -- only which album comes next is randomized.
    A single-album group (playing one album, or a playlist that
    happens to be one album) degrades to a no-op, same algorithm,
    nothing special-cased."""
    groups = {}
    order = []
    for e in entries:
        key = (e.get("albumartist") or e.get("artist", ""), e.get("album", ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    random.shuffle(order)
    return [e for key in order for e in groups[key]]


def _apply_shuffle_to_mpd(app, mode):
    try:
        # "albums" is handled by bebop itself at queue-build time (see
        # _shuffle_by_album/_queue_and_play) -- MPD's own random must
        # stay off, or it would ALSO shuffle within the already-
        # album-shuffled queue at the individual-song level.
        app.mpd.call("random", 1 if mode == "songs" else 0)
    except MPDError as exc:
        print(f"MPD random failed: {exc}", file=sys.stderr)


def _apply_repeat_to_mpd(app, mode):
    try:
        if mode == "one":
            app.mpd.call("repeat", 1)
            app.mpd.call("single", 1)
        elif mode == "all":
            app.mpd.call("repeat", 1)
            app.mpd.call("single", 0)
        else:
            app.mpd.call("repeat", 0)
            app.mpd.call("single", 0)
    except MPDError as exc:
        print(f"MPD repeat/single failed: {exc}", file=sys.stderr)


def apply_startup_playback_settings(app):
    """Pushes bebop's persisted Shuffle/Repeat defaults to MPD once at
    launch (called from bebop.py's __init__), so they take effect even
    if this session never opens Settings -- MPD has no equivalent of
    bebop's 3-way Shuffle modes (just its own 2-state random flag), so
    settings.ini is the durable source of truth here, applied on top
    of whatever MPD's own state happened to be."""
    _apply_shuffle_to_mpd(app, app.settings.shuffle)
    _apply_repeat_to_mpd(app, app.settings.repeat)


def _reshuffle_current_queue_and_play(app):
    """Called right after picking Shuffle -> Songs/Albums (not Off,
    which shouldn't interrupt whatever's already playing) -- reorders
    whatever's currently queued to match the new mode and starts
    playing from the top, then jumps straight to Now Playing so the
    effect is immediately audible/visible instead of leaving the user
    on Settings wondering if anything happened (the user's own report,
    2026-08-23). Falls back to the whole library (same source Songs
    uses) if nothing was queued yet this session."""
    try:
        entries = app.mpd.call("playlistinfo")
        if not entries:
            entries = [e for e in app.mpd.call("listallinfo") if "file" in e]
    except MPDError as exc:
        print(f"MPD queue fetch for reshuffle failed: {exc}", file=sys.stderr)
        return
    if not entries:
        return  # genuinely nothing to play (empty library)

    if app.settings.shuffle == "albums":
        ordered = _shuffle_by_album(entries)
    else:
        ordered = list(entries)
        random.shuffle(ordered)

    try:
        app.mpd.call("clear")
        for e in ordered:
            app.mpd.call("add", e["file"])
        app.mpd.call("play", 0)
    except MPDError as exc:
        print(f"MPD reshuffle/play failed: {exc}", file=sys.stderr)
        return
    app.push_screen(NowPlayingScreen(app))


def _set_shuffle_mode(app, mode):
    app.settings.set_shuffle(mode)
    _apply_shuffle_to_mpd(app, mode)
    if mode == "off":
        # Turning shuffle off shouldn't interrupt whatever's already
        # playing -- just apply the flag and return to Settings, same
        # as Repeat/Text Color.
        app.stack[-1] = build_settings_menu(app)
    else:
        _reshuffle_current_queue_and_play(app)


def _set_repeat_mode(app, mode):
    app.settings.set_repeat(mode)
    _apply_repeat_to_mpd(app, mode)
    app.stack[-1] = build_settings_menu(app)


def _set_text_color(app, name):
    app.settings.set_text_color(name)
    app.stack[-1] = build_settings_menu(app)


def _queue_and_play(app, entries, index):
    """Shared by every "play this list of MPD entries starting at
    index" path (Songs, an album's tracks, a saved playlist) -- clears
    the queue, loads the whole list (album-shuffled first if that's
    the active Shuffle mode) so Left/Right (previous/next) on Now
    Playing walks through the same order actually queued, and plays
    the chosen song specifically (not just queue position `index`,
    once shuffling may have moved it)."""
    selected_file = entries[index]["file"]
    ordered = _shuffle_by_album(entries) if app.settings.shuffle == "albums" else entries
    play_index = next(i for i, e in enumerate(ordered) if e["file"] == selected_file)
    try:
        app.mpd.call("clear")
        for e in ordered:
            app.mpd.call("add", e["file"])
        app.mpd.call("play", play_index)
    except MPDError as exc:
        print(f"MPD queue/play failed: {exc}", file=sys.stderr)
        return
    app.push_screen(NowPlayingScreen(app))


def _song_label(entry):
    return entry.get("title") or entry["file"].rsplit("/", 1)[-1]


def build_artists_screen(app):
    try:
        artists = sorted({d["artist"] for d in app.mpd.call("list", "artist") if d.get("artist")})
    except MPDError as exc:
        print(f"MPD list artist failed: {exc}", file=sys.stderr)
        return ListScreen("Artists", [])

    def on_select(app, index):
        app.push_screen(build_albums_screen(app, artists[index]))

    return ListScreen("Artists", artists, on_select=on_select, chevrons=True)


def build_albums_screen(app, artist):
    try:
        albums = sorted({d["album"] for d in app.mpd.call("list", "album", "artist", artist) if d.get("album")})
    except MPDError as exc:
        print(f"MPD list album failed: {exc}", file=sys.stderr)
        return ListScreen(artist, [])

    def on_select(app, index):
        app.push_screen(build_album_songs_screen(app, artist, albums[index]))

    return ListScreen(artist, albums, on_select=on_select, chevrons=True)


def _track_sort_key(entry):
    # Album tracks play in track-number order, not alphabetical --
    # MPD's own `find` already tends to return them that way for a
    # normally-tagged library, but this makes it a guarantee rather
    # than an accident. "track" can be "3" or "3/12" (disc-relative);
    # untagged/unparsable falls back after every numbered track, then
    # by title, rather than crashing the sort.
    track = entry.get("track", "")
    try:
        return (0, int(str(track).split("/")[0]))
    except ValueError:
        return (1, entry.get("title", entry.get("file", "")))


def build_album_songs_screen(app, artist, album):
    try:
        entries = app.mpd.call("find", "artist", artist, "album", album)
    except MPDError as exc:
        print(f"MPD find (album songs) failed: {exc}", file=sys.stderr)
        return ListScreen(album, [])

    entries.sort(key=_track_sort_key)
    labels = [_song_label(e) for e in entries]

    def on_select(app, index):
        _queue_and_play(app, entries, index)

    return ListScreen(album, labels, on_select=on_select, chevrons=False)


def build_playlists_screen(app):
    try:
        names = sorted(p["playlist"] for p in app.mpd.call("listplaylists"))
    except MPDError as exc:
        print(f"MPD listplaylists failed: {exc}", file=sys.stderr)
        return ListScreen("Playlists", [])

    def on_select(app, index):
        app.push_screen(build_playlist_songs_screen(app, names[index]))

    return ListScreen("Playlists", names, on_select=on_select, chevrons=True)


def build_playlist_songs_screen(app, name):
    try:
        entries = app.mpd.call("listplaylistinfo", name)
    except MPDError as exc:
        print(f"MPD listplaylistinfo failed: {exc}", file=sys.stderr)
        return ListScreen(name, [])

    labels = [_song_label(e) for e in entries]

    def on_select(app, index):
        # `load` (not clear+add) -- MPD's native "put this saved
        # playlist's contents on the queue" command, simpler than
        # rebuilding it file-by-file like _queue_and_play does for
        # lists bebop assembled itself (Songs, an album) rather than
        # loaded from a playlist MPD already has stored.
        try:
            app.mpd.call("clear")
            app.mpd.call("load", name)
            app.mpd.call("play", index)
        except MPDError as exc:
            print(f"MPD load playlist failed: {exc}", file=sys.stderr)
            return
        app.push_screen(NowPlayingScreen(app))

    return ListScreen(name, labels, on_select=on_select, chevrons=False)


def build_songs_screen(app):
    """All songs, alphabetical by title (per the brief's menu
    structure) -- queries MPD fresh every time this screen is entered
    rather than caching, matching bebop's "graphical MPD client only"
    architecture (MPD is the single source of truth)."""
    try:
        entries = [e for e in app.mpd.call("listallinfo") if "file" in e]
    except MPDError as exc:
        print(f"MPD listallinfo failed: {exc}", file=sys.stderr)
        return ListScreen("Songs", [])

    entries.sort(key=lambda e: (e.get("title") or e["file"]).lower())
    labels = [_song_label(e) for e in entries]

    def on_select(app, index):
        _queue_and_play(app, entries, index)

    return ListScreen("Songs", labels, on_select=on_select, chevrons=False)


def build_settings_menu(app):
    # Shows the current value inline (e.g. "Shuffle: Songs") -- without
    # this, picking a value and landing back here gave no visible
    # confirmation anything had changed (the user's own report,
    # 2026-08-23). SETTINGS_ITEMS (dispatch keys, in _settings_select)
    # stays separate from these display labels, order matters but text
    # doesn't.
    labels = [
        f"Shuffle: {app.settings.shuffle.capitalize()}",
        f"Repeat: {app.settings.repeat.capitalize()}",
        f"Text Color: {app.settings.text_color.capitalize()}",
        "About",
    ]
    return ListScreen("Settings", labels, on_select=_settings_select, chevrons=True)


def _settings_select(app, index):
    label = SETTINGS_ITEMS[index]
    if label == "Shuffle":
        app.push_screen(_radio_screen("Shuffle", SHUFFLE_MODES, app.settings.shuffle, _set_shuffle_mode))
    elif label == "Repeat":
        app.push_screen(_radio_screen("Repeat", REPEAT_MODES, app.settings.repeat, _set_repeat_mode))
    elif label == "Text Color":
        app.push_screen(_radio_screen(
            "Text Color", list(TEXT_COLORS.keys()), app.settings.text_color, _set_text_color,
        ))
    elif label == "About":
        app.push_screen(build_about_screen(app))


def _radio_screen(title, values, current_value, setter):
    """A single-choice picker (Shuffle/Repeat/Text Color) -- the cursor
    starts on whichever value is already active (so the highlighted row
    itself shows the current setting, no separate checkmark glyph
    needed). Picking a value calls setter(app, value) and trusts it to
    handle its own navigation -- Repeat/Text Color always return to a
    freshly-rebuilt Settings screen, but Shuffle -> Songs/Albums jumps
    to Now Playing instead (see _set_shuffle_mode), so that decision
    can't be made generically here."""
    labels = [v.capitalize() for v in values]
    screen = ListScreen(title, labels, chevrons=False)
    screen.selected = values.index(current_value) if current_value in values else 0

    def on_select(app, index):
        setter(app, values[index])

    screen.on_select = on_select
    return screen


def build_about_screen(app):
    try:
        song_count = app.mpd.call("stats").get("songs", "--")
    except MPDError as exc:
        print(f"MPD stats failed: {exc}", file=sys.stderr)
        song_count = "--"

    try:
        usage = shutil.disk_usage(app.config.music_directory)
        capacity = f"{usage.total / 1e9:.1f} GB"
        available = f"{usage.free / 1e9:.1f} GB"
    except OSError as exc:
        print(f"disk_usage({app.config.music_directory}) failed: {exc}", file=sys.stderr)
        capacity = available = "--"

    return InfoScreen("About", [
        f"SONGS: {song_count}",
        f"CAPACITY: {capacity}",
        f"AVAILABLE: {available}",
        f"VERSION: {VERSION}",
    ])
