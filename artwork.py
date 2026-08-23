"""Album art extraction + caching for bebop. Embedded MP3 tags (via
mutagen) are preferred; cover.jpg/folder.jpg-style files in the
track's own directory are the fallback (config.toml's
artwork.fallback_filenames), per the brief.

Loaded images are decoded once per track and cached as pygame Surfaces
in memory (a bounded LRU -- see CACHE_MAX_ENTRIES) so repeated Now-
Playing/Album-Art visits for the same track don't re-read/re-decode
from disk every render. Images are downscaled to roughly
config.artwork_size on first load rather than held at whatever
resolution the source happened to be -- 400x400 is what
AlbumArtScreen actually displays at (see the brief: "square artwork
should be displayed at exactly 400x400 pixels"), and there's no
benefit to keeping a multi-megapixel surface in memory on a Pi 3B+ for
something that never appears larger than that.
"""
import io
import sys
from collections import OrderedDict
from pathlib import Path

import pygame
from mutagen import File as MutagenFile

CACHE_MAX_ENTRIES = 50  # ~50 * (<=400x400x3 bytes) is a few MB at most, plenty of headroom on a Pi 3B+

_cache = OrderedDict()  # path str -> pygame.Surface or None ("checked, no art anywhere")


def _cache_get(key):
    if key not in _cache:
        return None, False
    _cache.move_to_end(key)
    return _cache[key], True


def _cache_put(key, value):
    _cache[key] = value
    _cache.move_to_end(key)
    if len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)  # evict least-recently-used


def _decode_and_scale(data, artwork_size):
    try:
        surf = pygame.image.load(io.BytesIO(data)).convert()
    except (pygame.error, OSError) as exc:
        print(f"Artwork decode failed: {exc}", file=sys.stderr)
        return None
    w, h = surf.get_size()
    if max(w, h) > artwork_size:
        scale = artwork_size / max(w, h)
        surf = pygame.transform.smoothscale(surf, (max(1, int(w * scale)), max(1, int(h * scale))))
    return surf


def _embedded_art(path):
    """Returns raw image bytes from the file's own embedded tag art,
    or None. ID3 APIC frames (MP3, this library's actual format) show
    up as tags keyed "APIC:<description>" -- picks the front-cover
    type (3) if more than one picture is embedded, otherwise whichever
    comes first. Also checks mutagen's uniform .pictures API
    (FLAC/MP4/etc) in case the library ever grows beyond MP3."""
    try:
        audio = MutagenFile(path)
    except Exception as exc:  # noqa: BLE001 -- a library of 1000+ real-world files will have some with corrupt/unsupported tags; one bad file shouldn't break art lookup for the rest
        print(f"Tag read failed for {path}: {exc}", file=sys.stderr)
        return None
    if audio is None:
        return None

    if audio.tags is not None:
        apics = [v for k, v in audio.tags.items() if k.startswith("APIC")]
        if apics:
            front = next((p for p in apics if getattr(p, "type", None) == 3), apics[0])
            return front.data

    pictures = getattr(audio, "pictures", None)
    if pictures:
        front = next((p for p in pictures if getattr(p, "type", None) == 3), pictures[0])
        return front.data

    return None


def _fallback_file_art(path, config):
    directory = path.parent
    for name in config.artwork_fallback_filenames:
        candidate = directory / name
        if candidate.exists():
            try:
                return candidate.read_bytes()
            except OSError as exc:
                print(f"Fallback artwork read failed for {candidate}: {exc}", file=sys.stderr)
    return None


def get_artwork(mpd_file, config):
    """Returns a cached, pre-scaled pygame.Surface for the given
    track's album art (mpd_file is MPD's own "file" field -- relative
    to config.music_directory), or None if no art was found anywhere.
    Callers fall back to the no-art placeholder themselves (see
    albumart.py) -- None here specifically means "genuinely checked,
    nothing found," cached the same as a real result so a track
    confirmed art-less isn't re-checked on every visit."""
    path = config.music_directory / mpd_file
    cached, found = _cache_get(str(path))
    if found:
        return cached

    data = _embedded_art(path) or _fallback_file_art(path, config)
    surf = _decode_and_scale(data, config.artwork_size) if data else None
    _cache_put(str(path), surf)
    return surf
