#!/usr/bin/env python3
"""
SpotDL — Spotify & YouTube Downloader (no credentials, no Premium).

Track metadata is scraped from Spotify's public embed page; audio comes
from YouTube via yt-dlp; covers + lyrics are embedded with mutagen.
Android-compatible version.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import qrcode
import requests as _req
import yt_dlp
from dotenv import load_dotenv
from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from functools import wraps

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Paths & app  — Android (Chaquopy) + desktop compatible
#
# When running inside the Chaquopy APK, MainActivity.kt sets these env vars
# before Python starts:
#   SPOTDL_DATA_DIR      — writable internal files dir (config, sessions)
#   SPOTDL_DOWNLOADS_DIR — writable dir for downloaded audio/zips
#   SPOTDL_FFMPEG        — absolute path to the libffmpeg.so binary
#   LD_LIBRARY_PATH      — directory containing ffmpeg shared libs
#   TMPDIR               — writable temp dir
# ─────────────────────────────────────────────────────────────────────────────

_FFMPEG_LOCATION = os.environ.get('SPOTDL_FFMPEG') or None

def get_app_data_dir() -> Path:
    """Writable directory for config.json and sessions.json."""
    env = os.environ.get('SPOTDL_DATA_DIR')
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / 'data'

def get_app_downloads_dir() -> Path:
    """Writable directory where downloaded audio/video and zips are staged."""
    env = os.environ.get('SPOTDL_DOWNLOADS_DIR')
    if env:
        return Path(env)
    return get_app_data_dir().parent / 'downloads'

DATA_DIR      = get_app_data_dir()
DOWNLOADS_DIR = get_app_downloads_dir()
ROOT_DIR      = DATA_DIR.parent
SESSIONS_FILE = DATA_DIR / 'sessions.json'
CONFIG_FILE   = DATA_DIR / 'config.json'

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', secrets.token_hex(16))

_BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

ACTIVE_STATUSES = {'initializing', 'fetching_playlist', 'downloading', 'creating_zip'}

# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    'max_songs':       100,
    'chunk_size':      25,
    'audio_quality':   'mp3-320',
    'video_quality':   '720p',
    'parallel_workers': 4,        # concurrent track downloads
    'retention_hours': 72,        # how long completed files stay on disk (3 days)
}

# ─────────────────────────────────────────────────────────────────────────────
# Optional password gate (set SPOTDL_PASSWORD env var to enable)
# ─────────────────────────────────────────────────────────────────────────────
_AUTH_PASSWORD = os.environ.get('SPOTDL_PASSWORD', '').strip()
_AUTH_ENABLED  = bool(_AUTH_PASSWORD)  # Only enable if password is set

def _is_authed() -> bool:
    return (not _AUTH_ENABLED) or session.get('authed') is True

_PUBLIC_PATHS = {'/landing', '/login', '/logout', '/healthz'}

@app.before_request
def _gate():
    p = request.path or '/'
    # Admin routes have their own auth (independent of SPOTDL_PASSWORD).
    if p.startswith('/admin'):
        return None
    if not _AUTH_ENABLED or _is_authed():
        return None
    if p in _PUBLIC_PATHS or p.startswith('/static/'):
        return None
    if p == '/':
        return redirect(url_for('landing'))
    # Anything else: send them to login, then bounce back.
    if request.method == 'GET':
        return redirect(url_for('login', next=p))
    return ('Unauthorized', 401)

@app.context_processor
def _inject_auth():
    return {
        'auth_enabled':  _AUTH_ENABLED,
        'is_authed':     _is_authed(),
        'admin_enabled': _ADMIN_ENABLED,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Hidden admin console — unlocked by tapping the brand logo 7 times.
# Independent auth via SPOTDL_ADMIN_PASSWORD env var. Disabled by default for Android.
# ─────────────────────────────────────────────────────────────────────────────
_ADMIN_PASSWORD = os.environ.get('SPOTDL_ADMIN_PASSWORD', '').strip()
_ADMIN_ENABLED  = False  # Disabled by default for Android version

# Ring buffer of recent log lines, viewable from the admin console.
_LOG_BUFFER: deque = deque(maxlen=500)
_LOG_LOCK = threading.Lock()

class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with _LOG_LOCK:
                _LOG_BUFFER.append({
                    'ts': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                    'level': record.levelname,
                    'msg': line,
                })
        except Exception:
            pass

def _install_log_capture() -> None:
    h = _RingBufferHandler()
    h.setFormatter(logging.Formatter('%(message)s'))
    h.setLevel(logging.INFO)
    for name in ('werkzeug', 'spotdl', ''):
        lg = logging.getLogger(name)
        if not any(isinstance(x, _RingBufferHandler) for x in lg.handlers):
            lg.addHandler(h)
        lg.setLevel(logging.INFO)

_install_log_capture()
log = logging.getLogger('spotdl')

def _admin_log(msg: str, level: str = 'INFO') -> None:
    """Manually push a line into the admin log buffer."""
    with _LOG_LOCK:
        _LOG_BUFFER.append({
            'ts': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'level': level,
            'msg': msg,
        })

def _is_admin() -> bool:
    return _ADMIN_ENABLED and session.get('admin_authed') is True

def _require_admin():
    if not _ADMIN_ENABLED:
        return ('Admin console disabled. Set SPOTDL_ADMIN_PASSWORD to enable.', 404)
    if not _is_admin():
        return redirect(url_for('admin_login_page', next=request.path))
    return None

app_settings = dict(DEFAULT_SETTINGS)

def _read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)

def _load_settings():
    cfg = _read_json(CONFIG_FILE, {})
    for k, v in DEFAULT_SETTINGS.items():
        if k in cfg:
            app_settings[k] = cfg[k]
    # type coercion
    for k in ('max_songs', 'chunk_size', 'parallel_workers', 'retention_hours'):
        try:
            app_settings[k] = int(app_settings[k])
        except (TypeError, ValueError):
            app_settings[k] = DEFAULT_SETTINGS[k]

def _save_settings():
    _write_json(CONFIG_FILE, {k: app_settings[k] for k in DEFAULT_SETTINGS})

_load_settings()

# ─────────────────────────────────────────────────────────────────────────────
# Session store (in-memory + persisted to disk so a restart doesn't lose state)
# ─────────────────────────────────────────────────────────────────────────────

download_status_dict: dict = {}
_status_lock = threading.Lock()

# Fields too big to persist to disk (still returned in API responses)
_NON_PERSISTED = {'all_tracks', 'tracks_progress'}
# Fields hidden from API responses (e.g. internal structures)
_NON_PUBLIC    = {'all_tracks'}

def _persist_sessions():
    try:
        snapshot = {}
        with _status_lock:
            for k, v in download_status_dict.items():
                if v.get('status') in ACTIVE_STATUSES:
                    continue  # don't persist mid-flight
                snapshot[k] = {kk: vv for kk, vv in v.items() if kk not in _NON_PERSISTED}
        _write_json(SESSIONS_FILE, snapshot)
    except Exception as e:
        print(f"[persist] {e}")

def _load_sessions():
    data = _read_json(SESSIONS_FILE, {})
    if not isinstance(data, dict):
        return
    with _status_lock:
        for k, v in data.items():
            # Drop entries whose file has disappeared
            zf = v.get('zip_file')
            if zf and not os.path.exists(zf):
                continue
            v['all_tracks'] = []
            download_status_dict[k] = v

_load_sessions()

def _update_status(dl_id: str, **fields):
    with _status_lock:
        s = download_status_dict.get(dl_id)
        if s is None:
            return
        s.update(fields)

def _set_status(dl_id: str, payload: dict):
    with _status_lock:
        download_status_dict[dl_id] = payload

def _new_id(prefix: str = 'dl') -> str:
    return f"{prefix}_{int(time.time())}_{secrets.token_hex(4)}"

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup — retention-based only, no aggressive 60s deletion.
# ─────────────────────────────────────────────────────────────────────────────

def _delete_session_file(status: dict):
    fp = status.get('zip_file')
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception as e:
            print(f"[cleanup] could not delete {fp}: {e}")

def _cleanup_worker():
    """Once an hour, remove sessions whose files exceed the retention window."""
    while True:
        try:
            time.sleep(3600)
            retention = max(1, int(app_settings.get('retention_hours', 24)))
            cutoff = datetime.now() - timedelta(hours=retention)
            to_del = []
            with _status_lock:
                for dl_id, s in list(download_status_dict.items()):
                    if s.get('status') in ACTIVE_STATUSES:
                        continue
                    ts_str = s.get('completed_at') or s.get('started_at')
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except Exception:
                        continue
                    if ts < cutoff:
                        to_del.append(dl_id)
                for dl_id in to_del:
                    s = download_status_dict.pop(dl_id, {})
                    _delete_session_file(s)
            if to_del:
                print(f"[cleanup] removed {len(to_del)} expired session(s)")
                _persist_sessions()
        except Exception as e:
            print(f"[cleanup] worker error: {e}")

threading.Thread(target=_cleanup_worker, daemon=True, name='cleanup').start()

# ─────────────────────────────────────────────────────────────────────────────
# Spotify search — anonymous web token, no credentials needed.
# ─────────────────────────────────────────────────────────────────────────────

_anon_token: str | None = None
_anon_token_exp: float = 0.0
_token_lock = threading.Lock()

_TOKEN_PROBE_URL = 'https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M'

def _get_anon_token() -> str:
    """Fetch (and cache) an anonymous web-player access token.

    Spotify blocks /get_access_token from data-center IPs, but the same token
    is embedded inside any /embed/<…> page's __NEXT_DATA__ JSON. We just
    scrape it from there.
    """
    global _anon_token, _anon_token_exp
    with _token_lock:
        if _anon_token and time.time() < _anon_token_exp:
            return _anon_token
        # Try /get_access_token first (works on residential IPs)
        try:
            r = _req.get('https://open.spotify.com/get_access_token'
                         '?reason=transport&productType=embed',
                         headers=_BROWSER_HEADERS, timeout=10)
            if r.ok and r.headers.get('content-type', '').startswith('application/json'):
                d = r.json()
                if d.get('accessToken'):
                    _anon_token = d['accessToken']
                    _anon_token_exp = (d.get('accessTokenExpirationTimestampMs', 0) / 1000.0) - 60
                    return _anon_token
        except Exception:
            pass
        # Fallback: scrape token out of an embed page
        r = _req.get(_TOKEN_PROBE_URL, headers=_BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', r.text)
        if not m:
            raise RuntimeError("Could not extract Spotify access token from embed.")
        _anon_token = m.group(1)
        # Embed tokens last ~30 min; refresh every 20 min just in case.
        m2 = re.search(r'"accessTokenExpirationTimestampMs"\s*:\s*(\d+)', r.text)
        if m2:
            _anon_token_exp = (int(m2.group(1)) / 1000.0) - 60
        else:
            _anon_token_exp = time.time() + 20 * 60
        return _anon_token

def _itunes_search(term: str, entity: str, limit: int) -> list:
    """Hit iTunes Search API (free, no auth). Returns the 'results' list."""
    try:
        r = _req.get('https://itunes.apple.com/search',
                     params={'term': term, 'entity': entity,
                             'limit': limit, 'media': 'music'},
                     timeout=12)
        if r.ok:
            return r.json().get('results') or []
    except Exception as e:
        print(f"[itunes] {e}")
    return []

def _bigger_artwork(url: str, size: int = 600) -> str:
    """iTunes hands back 100x100 art; rewrite to a higher-res variant."""
    if not url:
        return ''
    return re.sub(r'/\d+x\d+(bb)?\.', f'/{size}x{size}bb.', url)

def spotify_search(query: str, types: tuple = ('track', 'playlist', 'album'),
                   limit: int = 8) -> dict:
    """Catalog search via iTunes (more reliable from cloud IPs than Spotify).

    For 'playlist', falls back to a Spotify embed-page scrape since iTunes
    has no playlist concept. Returns {tracks:[], albums:[], playlists:[]}.
    """
    out = {f'{t}s': [] for t in types}
    q = query.strip()
    if not q:
        return out

    # ─ Tracks ─
    if 'track' in types:
        for it in _itunes_search(q, 'song', limit):
            out['tracks'].append({
                'name':   it.get('trackName') or '',
                'artist': it.get('artistName') or '',
                'album':  it.get('collectionName') or '',
                'cover':  _bigger_artwork(it.get('artworkUrl100', '')),
                'duration_ms': it.get('trackTimeMillis', 0),
                'kind':   'track',
                # We don't need a Spotify URL — the downloader can resolve
                # artist+title directly via YouTube.
                'url':    '',
                'id':     str(it.get('trackId', '')),
            })

    # ─ Albums ─
    if 'album' in types:
        for it in _itunes_search(q, 'album', limit):
            out['albums'].append({
                'name':   it.get('collectionName') or '',
                'artist': it.get('artistName') or '',
                'cover':  _bigger_artwork(it.get('artworkUrl100', '')),
                'year':   (it.get('releaseDate') or '')[:4],
                'tracks_count': it.get('trackCount', 0),
                'kind':   'album',
                'url':    '',  # downloader uses album_id below
                'id':     str(it.get('collectionId', '')),
            })

    # ─ Playlists ─ (Spotify only — iTunes has none.)
    if 'playlist' in types:
        try:
            token = _get_anon_token()
            r = _req.get('https://api.spotify.com/v1/search',
                         params={'q': q, 'type': 'playlist', 'limit': limit},
                         headers={'Authorization': f'Bearer {token}',
                                  'User-Agent': _BROWSER_HEADERS['User-Agent']},
                         timeout=12)
            if r.ok:
                items = (r.json().get('playlists') or {}).get('items') or []
                for it in items:
                    if not it:
                        continue
                    imgs = it.get('images') or []
                    cover = imgs[0].get('url', '') if imgs else ''
                    out['playlists'].append({
                        'name':   it.get('name', ''),
                        'artist': (it.get('owner') or {}).get('display_name', ''),
                        'cover':  cover,
                        'tracks_count': (it.get('tracks') or {}).get('total', 0),
                        'kind':   'playlist',
                        'url':    (it.get('external_urls') or {}).get('spotify', ''),
                        'id':     it.get('id', ''),
                    })
        except Exception as e:
            print(f"[search/playlist] {e}")
    return out

def fetch_itunes_album_tracks(album_id: str) -> tuple[str, list]:
    """Look up an iTunes album by ID and return (name, tracks) for downloading."""
    try:
        r = _req.get('https://itunes.apple.com/lookup',
                     params={'id': album_id, 'entity': 'song', 'limit': 200},
                     timeout=15)
        r.raise_for_status()
        results = r.json().get('results') or []
    except Exception as e:
        raise RuntimeError(f"iTunes lookup failed: {e}")
    if not results:
        raise RuntimeError("Album not found.")

    album_meta  = next((x for x in results if x.get('wrapperType') == 'collection'), results[0])
    name        = album_meta.get('collectionName') or 'Album'
    cover       = _bigger_artwork(album_meta.get('artworkUrl100', ''))
    year        = (album_meta.get('releaseDate') or '')[:4]
    songs = [x for x in results if x.get('wrapperType') == 'track']
    songs.sort(key=lambda s: (s.get('discNumber', 1), s.get('trackNumber', 0)))

    tracks = []
    for i, s in enumerate(songs, start=1):
        tracks.append({
            'name':         s.get('trackName', '').strip(),
            'artist':       s.get('artistName', '').strip(),
            'album':        name,
            'spotify_url':  '',   # not needed
            'track_number': s.get('trackNumber', i),
            'cover_url':    cover,
            'year':         year,
            'preview_url':  s.get('previewUrl', ''),
        })
    if not tracks:
        raise RuntimeError("Album has no tracks.")
    return name, tracks

# ─────────────────────────────────────────────────────────────────────────────
# Spotify scraper — public embed page, no credentials.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_spotify_url(url: str) -> tuple[str, str]:
    url = url.strip().split('?')[0]
    for kind in ('playlist', 'album', 'track'):
        if f'/{kind}/' in url:
            return kind, url.split(f'/{kind}/')[1].split('/')[0]
        if f'spotify:{kind}:' in url:
            return kind, url.split(f'spotify:{kind}:')[1]
    raise ValueError("Unsupported Spotify URL — paste a playlist, album, or track link.")

def _extract_embed_state(html: str) -> dict | None:
    """Return the parsed __NEXT_DATA__ JSON state, robust to Spotify changes."""
    # New format: <script id="__NEXT_DATA__" type="application/json">…</script>
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Older format: any script containing trackList JSON
    for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if 'trackList' in s or '"entity"' in s:
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None

def fetch_playlist_tracks(url: str):
    """Return (name, tracks). Supports playlist / album / single track URLs."""
    kind, sid = _parse_spotify_url(url)
    embed_url = f'https://open.spotify.com/embed/{kind}/{sid}'
    r = _req.get(embed_url, headers=_BROWSER_HEADERS, timeout=20)
    r.raise_for_status()

    state = _extract_embed_state(r.text)
    if not state:
        raise RuntimeError("Could not parse Spotify embed — playlist may be private.")

    try:
        entity = state['props']['pageProps']['state']['data']['entity']
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"Unexpected Spotify embed format: {e}")

    name = entity.get('name') or entity.get('title') or 'Spotify'

    vi         = entity.get('visualIdentity') or {}
    imgs       = sorted(vi.get('image') or [],
                        key=lambda x: x.get('maxWidth') or 0, reverse=True)
    entity_art = imgs[0]['url'] if imgs else ''

    rd          = entity.get('releaseDate') or {}
    entity_year = rd.get('isoString', '')[:4] if isinstance(rd, dict) else ''

    track_list = entity.get('trackList') or []
    # Single-track URL: build a one-element list
    if kind == 'track' and not track_list:
        track_list = [{
            'title':    entity.get('name') or entity.get('title', ''),
            'subtitle': entity.get('subtitle') or '',
            'uri':      f'spotify:track:{sid}',
        }]

    tracks = []
    for idx, t in enumerate(track_list, start=1):
        title    = t.get('title', '').strip()
        subtitle = t.get('subtitle', '').strip()
        uri      = t.get('uri', '')
        tid      = uri.split(':')[-1] if ':' in uri else ''
        if not title:
            continue
        ap          = t.get('audioPreview') or {}
        preview_url = ap.get('url', '') if isinstance(ap, dict) else ''
        tracks.append({
            'name':         title,
            'artist':       subtitle,
            'album':        name if kind == 'album' else '',
            'spotify_url':  f'https://open.spotify.com/track/{tid}' if tid else '',
            'track_number': idx,
            'cover_url':    entity_art if kind in ('album', 'track') else '',
            'year':         entity_year if kind in ('album', 'track') else '',
            'preview_url':  preview_url,
        })

    if not tracks:
        raise RuntimeError("No tracks found — is the link public?")
    return name, tracks

# ─────────────────────────────────────────────────────────────────────────────
# Per-track metadata enrichment + cover art
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_track_embed_meta(spotify_url: str) -> dict:
    tid = ''
    try:
        if 'spotify:track:' in spotify_url:
            tid = spotify_url.split('spotify:track:')[1].split('?')[0].strip()
        elif '/track/' in spotify_url:
            tid = spotify_url.split('/track/')[1].split('?')[0].strip()
    except Exception:
        pass
    if not tid:
        return {}
    try:
        r = _req.get(f'https://open.spotify.com/embed/track/{tid}',
                     headers=_BROWSER_HEADERS, timeout=12)
        if not r.ok:
            return {}
        state = _extract_embed_state(r.text)
        if not state:
            return {}
        entity = state['props']['pageProps']['state']['data']['entity']
        vi     = entity.get('visualIdentity') or {}
        imgs   = sorted(vi.get('image') or [],
                        key=lambda x: x.get('maxWidth') or 0, reverse=True)
        cover  = imgs[0]['url'] if imgs else ''
        rd     = entity.get('releaseDate') or {}
        year   = rd.get('isoString', '')[:4] if isinstance(rd, dict) else ''
        album  = entity.get('subtitle') or ''
        return {'cover_url': cover, 'year': year, 'album': album}
    except Exception:
        return {}

def _download_image(url: str) -> bytes | None:
    try:
        r = _req.get(url, timeout=10, headers=_BROWSER_HEADERS)
        if r.ok and r.content:
            return r.content
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Lyrics — lrclib.net (free, no auth)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_lyrics(title: str, artist: str, album: str = '') -> str:
    try:
        params = {'track_name': title, 'artist_name': artist}
        if album:
            params['album_name'] = album
        r = _req.get('https://lrclib.net/api/get', params=params, timeout=10)
        if r.ok:
            d = r.json()
            return d.get('plainLyrics') or d.get('syncedLyrics') or ''
    except Exception:
        pass
    return ''

# ─────────────────────────────────────────────────────────────────────────────
# Audio tagger — MP3 / FLAC
# ─────────────────────────────────────────────────────────────────────────────

def tag_audio_file(path: str, track: dict, cover_bytes: bytes | None, lyrics: str):
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == '.mp3':
            from mutagen.mp3 import MP3
            from mutagen.id3 import (ID3, TIT2, TPE1, TALB, TPE2,
                                      TRCK, TDRC, APIC, USLT, error as ID3Error)
            audio = MP3(path, ID3=ID3)
            try:
                audio.add_tags()
            except ID3Error:
                pass
            tags = audio.tags
            for frame in ('TPUB', 'COMM', 'TCOP', 'TENC'):
                tags.delall(frame)
            tags['TIT2'] = TIT2(encoding=3, text=track.get('name', ''))
            tags['TPE1'] = TPE1(encoding=3, text=track.get('artist', ''))
            tags['TALB'] = TALB(encoding=3, text=track.get('album', ''))
            tags['TPE2'] = TPE2(encoding=3, text=track.get('artist', '').split(',')[0].strip())
            if track.get('year'):
                tags['TDRC'] = TDRC(encoding=3, text=str(track['year']))
            if track.get('track_number'):
                tags['TRCK'] = TRCK(encoding=3, text=str(track['track_number']))
            if cover_bytes:
                tags.delall('APIC')
                tags['APIC:'] = APIC(encoding=3, mime='image/jpeg', type=3,
                                     desc='Cover', data=cover_bytes)
            if lyrics:
                tags.delall('USLT')
                tags['USLT::eng'] = USLT(encoding=3, lang='eng', desc='', text=lyrics)
            audio.save()
        elif ext == '.flac':
            from mutagen.flac import FLAC, Picture
            audio = FLAC(path)
            for key in ('publisher', 'label', 'comment', 'encoder'):
                audio.pop(key, None)
            audio['title']       = track.get('name', '')
            audio['artist']      = track.get('artist', '')
            audio['album']       = track.get('album', '')
            audio['albumartist'] = track.get('artist', '').split(',')[0].strip()
            if track.get('year'):
                audio['date'] = str(track['year'])
            if track.get('track_number'):
                audio['tracknumber'] = str(track['track_number'])
            if lyrics:
                audio['lyrics'] = lyrics
            if cover_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = 'image/jpeg'
                pic.desc = 'Cover'
                pic.data = cover_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
    except Exception as e:
        print(f"[tag] {os.path.basename(path)}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Downloader
# ─────────────────────────────────────────────────────────────────────────────

class Downloader:
    @staticmethod
    def _safe_filename(s: str) -> str:
        s = re.sub(r'[^\w\s.-]', '', s)
        s = re.sub(r'[-\s]+', '-', s).strip('-')
        return s[:150] or 'track'

    def _audio_ydl_opts(self, out_dir: str, safe_name: str, quality: str) -> dict:
        codec   = 'flac' if quality == 'flac' else 'mp3'
        bitrate = {'mp3-128': '128', 'mp3-192': '192',
                   'mp3-320': '320', 'flac': '0'}.get(quality, '320')
        opts = {
            'format':            'bestaudio/best',
            'outtmpl':           os.path.join(out_dir, f'{safe_name}.%(ext)s'),
            'postprocessors':    [{'key': 'FFmpegExtractAudio',
                                   'preferredcodec': codec,
                                   'preferredquality': bitrate}],
            'quiet':             True,
            'no_warnings':       True,
            'nocheckcertificate': True,
            'no_color':          True,
            'socket_timeout':    30,
            'retries':           3,
            'fragment_retries':  3,
            'concurrent_fragment_downloads': 4,
            'geo_bypass':        True,
        }
        if _FFMPEG_LOCATION:
            opts['ffmpeg_location'] = _FFMPEG_LOCATION
        return opts

    def download_single_track(self, track: dict, out_dir: str, quality: str) -> str | None:
        safe  = self._safe_filename(f"{track.get('artist','')} - {track.get('name','')}")
        codec = 'flac' if quality == 'flac' else 'mp3'
        queries = [
            f"{track.get('artist','')} {track.get('name','')}",
            f"{track.get('name','')} {track.get('artist','')} audio",
        ]
        found = None
        for q in queries:
            try:
                with yt_dlp.YoutubeDL(self._audio_ydl_opts(out_dir, safe, quality)) as ydl:
                    ydl.extract_info(f"ytsearch1:{q}", download=True)
                for f in os.listdir(out_dir):
                    if f.startswith(safe[:50]) and f.endswith('.' + codec):
                        found = os.path.join(out_dir, f)
                        break
                if found:
                    break
            except Exception as e:
                print(f"[ydl] '{track.get('name')}': {e}")
                time.sleep(1)
        if not found:
            return None

        # Enrich
        enriched = dict(track)
        if not enriched.get('cover_url') and enriched.get('spotify_url'):
            meta = _fetch_track_embed_meta(enriched['spotify_url'])
            for k in ('cover_url', 'year', 'album'):
                if meta.get(k) and not enriched.get(k):
                    enriched[k] = meta[k]
        cover  = _download_image(enriched['cover_url']) if enriched.get('cover_url') else None
        lyrics = fetch_lyrics(enriched.get('name', ''),
                              enriched.get('artist', ''),
                              enriched.get('album', ''))
        tag_audio_file(found, enriched, cover, lyrics)
        return found

    def download_chunk(self, dl_id: str, all_tracks: list, start: int, end: int,
                       playlist_name: str, quality: str, chunk_size: int):
        try:
            chunk    = all_tracks[start:end]
            total    = len(chunk)
            has_next = end < len(all_tracks)
            workers  = max(1, int(app_settings.get('parallel_workers', 4)))

            # Build per-track progress list for the live UI grid
            tracks_progress = [{
                'i':      i,
                'name':   t.get('name', ''),
                'artist': t.get('artist', ''),
                'cover':  t.get('cover_url', ''),
                'status': 'pending',     # pending | downloading | done | failed
            } for i, t in enumerate(chunk)]

            _update_status(dl_id, status='downloading', total=total,
                           playlist_name=playlist_name,
                           chunk_label=f"Songs {start+1}–{end} of {len(all_tracks)}",
                           has_next=has_next,
                           next_start=end if has_next else None,
                           quality=quality, chunk_size=chunk_size,
                           tracks_progress=tracks_progress,
                           started_at=download_status_dict[dl_id].get('started_at')
                                      or datetime.now().isoformat())

            out_dir = DOWNLOADS_DIR / f'work_{dl_id}'
            out_dir.mkdir(parents=True, exist_ok=True)
            done: list[str] = []
            done_lock = threading.Lock()

            def _set_track(i: int, **fields):
                with _status_lock:
                    s = download_status_dict.get(dl_id)
                    if not s:
                        return
                    tp = s.get('tracks_progress') or []
                    if 0 <= i < len(tp):
                        tp[i].update(fields)

            def _worker(idx_track):
                idx, track = idx_track
                _set_track(idx, status='downloading')
                _update_status(dl_id,
                    current_song=f"{track.get('name','')} — {track.get('artist','')}")
                return idx, self.download_single_track(track, str(out_dir), quality)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_worker, (i, t)): i for i, t in enumerate(chunk)}
                for fut in as_completed(futures):
                    idx = futures[fut]
                    path = None
                    try:
                        idx, path = fut.result()
                    except Exception as e:
                        print(f"[worker] {e}")
                    if path and os.path.exists(path):
                        _set_track(idx, status='done')
                        with done_lock:
                            done.append(path)
                            n = len(done)
                        _update_status(dl_id, downloaded=n,
                                       progress=int((n / total) * 93))
                    else:
                        _set_track(idx, status='failed')

            if not done:
                raise RuntimeError("No songs could be downloaded.")

            _update_status(dl_id, status='creating_zip', current_song='Packaging…')

            zip_dir  = DOWNLOADS_DIR / dl_id
            zip_dir.mkdir(parents=True, exist_ok=True)
            zip_name = f"{self._safe_filename(playlist_name)}_{start+1}-{end}.zip"
            zip_path = zip_dir / zip_name
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for fp in sorted(done):
                    zf.write(fp, os.path.basename(fp))
            shutil.rmtree(out_dir, ignore_errors=True)

            _update_status(dl_id,
                status='completed', progress=100,
                zip_file=str(zip_path),
                file_size=os.path.getsize(zip_path),
                downloaded=len(done),
                completed_at=datetime.now().isoformat(),
                file_deleted=False, current_song='')
            _persist_sessions()
        except Exception as e:
            _update_status(dl_id, status='error', error=str(e),
                           completed_at=datetime.now().isoformat())
            _persist_sessions()

downloader = Downloader()

# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _active_count() -> int:
    with _status_lock:
        return sum(1 for s in download_status_dict.values()
                   if s.get('status') in ACTIVE_STATUSES)

def render(template, **kw):
    kw.setdefault('active_count', _active_count())
    kw.setdefault('settings', app_settings)
    return render_template(template, **kw)

def _public_status(s: dict) -> dict:
    return {k: v for k, v in s.items() if k not in _NON_PUBLIC}

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if _AUTH_ENABLED and not _is_authed():
        return redirect(url_for('login'))
    return render('index.html')

@app.route('/landing')
def landing():
    return render('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    nxt = request.values.get('next') or url_for('index')
    error = None
    if request.method == 'POST' and _AUTH_ENABLED:
        pw = (request.form.get('password') or '').strip()
        if pw and pw == _AUTH_PASSWORD:
            session['authed'] = True
            session.permanent = True
            return redirect(nxt)
        error = 'Incorrect access code. Try again.'
    return render('login.html', error=error, next=nxt)

@app.route('/logout')
def logout():
    session.pop('authed', None)
    return redirect(url_for('landing'))

@app.route('/healthz')
def healthz():
    return 'ok', 200

# ── Spotify download ─────────────────────────────────────────────────────────

@app.route('/start_download', methods=['POST'])
def start_download():
    playlist_url = request.form.get('playlist_url', '').strip()
    itunes_album_id = request.form.get('itunes_album_id', '').strip()
    itunes_track    = request.form.get('itunes_track', '').strip()  # "Artist|Title|cover_url"
    quality      = request.form.get('audio_quality', app_settings['audio_quality'])
    try:
        max_songs  = max(1, min(int(request.form.get('max_songs',  app_settings['max_songs'])), 500))
        chunk_size = max(1, min(int(request.form.get('chunk_size', app_settings['chunk_size'])), 100))
    except ValueError:
        max_songs, chunk_size = app_settings['max_songs'], app_settings['chunk_size']

    has_spotify = any(x in playlist_url for x in (
        'open.spotify.com/playlist/', 'open.spotify.com/album/',
        'open.spotify.com/track/', 'spotify:playlist:',
        'spotify:album:', 'spotify:track:'))

    if not (has_spotify or itunes_album_id or itunes_track):
        flash('Please enter a valid Spotify URL or pick a search result.', 'error')
        return redirect(url_for('index'))

    dl_id = _new_id('dl')
    _set_status(dl_id, {
        'id': dl_id, 'status': 'fetching_playlist', 'progress': 0,
        'current_song': '', 'downloaded': 0, 'total': 0,
        'error': None, 'zip_file': None, 'file_deleted': False,
        'started_at': datetime.now().isoformat(),
        'playlist_name': 'Fetching…', 'chunk_label': '',
        'has_next': False, 'next_start': None,
        'playlist_url': playlist_url, 'quality': quality,
        'max_songs': max_songs, 'chunk_size': chunk_size,
        'all_tracks': [],
    })

    def run():
        try:
            if itunes_album_id:
                name, tracks = fetch_itunes_album_tracks(itunes_album_id)
            elif itunes_track:
                # Format: artist|title|cover_url|album
                parts  = itunes_track.split('|')
                artist = (parts[0] if len(parts) > 0 else '').strip()
                title  = (parts[1] if len(parts) > 1 else '').strip()
                cover  = (parts[2] if len(parts) > 2 else '').strip()
                album  = (parts[3] if len(parts) > 3 else '').strip()
                name   = title or 'Track'
                tracks = [{
                    'name': title, 'artist': artist, 'album': album,
                    'spotify_url': '', 'track_number': 1,
                    'cover_url': cover, 'year': '', 'preview_url': '',
                }]
            else:
                name, tracks = fetch_playlist_tracks(playlist_url)
            tracks = tracks[:max_songs]
            with _status_lock:
                download_status_dict[dl_id]['all_tracks']    = tracks
                download_status_dict[dl_id]['playlist_name'] = name
            end = min(chunk_size, len(tracks))
            downloader.download_chunk(dl_id, tracks, 0, end, name, quality, chunk_size)
        except Exception as e:
            _update_status(dl_id, status='error', error=str(e),
                           completed_at=datetime.now().isoformat())
            _persist_sessions()

    threading.Thread(target=run, daemon=True).start()
    return redirect(url_for('download_status', download_id=dl_id))

@app.route('/next_chunk/<dl_id>')
def next_chunk(dl_id):
    with _status_lock:
        status = download_status_dict.get(dl_id)
    if not status:
        flash('Session not found.', 'error')
        return redirect(url_for('index'))

    next_start    = status.get('next_start')
    all_tracks    = status.get('all_tracks', [])
    playlist_url  = status.get('playlist_url', '')
    quality       = status.get('quality',    app_settings['audio_quality'])
    chunk_size    = status.get('chunk_size', app_settings['chunk_size'])
    playlist_name = status.get('playlist_name', 'Playlist')

    # If we lost all_tracks (after restart), re-fetch them
    if not all_tracks and playlist_url:
        try:
            _, all_tracks = fetch_playlist_tracks(playlist_url)
        except Exception as e:
            flash(f'Could not refetch playlist: {e}', 'error')
            return redirect(url_for('index'))

    if next_start is None or next_start >= len(all_tracks):
        flash('No more songs.', 'error')
        return redirect(url_for('index'))

    new_id = _new_id('dl')
    end    = min(next_start + chunk_size, len(all_tracks))
    _set_status(new_id, {
        'id': new_id, 'status': 'initializing', 'progress': 0,
        'current_song': '', 'downloaded': 0, 'total': 0,
        'error': None, 'zip_file': None, 'file_deleted': False,
        'started_at': datetime.now().isoformat(),
        'playlist_name': playlist_name, 'chunk_label': '',
        'has_next': False, 'next_start': None,
        'playlist_url': playlist_url, 'quality': quality,
        'chunk_size': chunk_size, 'all_tracks': all_tracks,
    })
    threading.Thread(
        target=downloader.download_chunk,
        args=(new_id, all_tracks, next_start, end, playlist_name, quality, chunk_size),
        daemon=True,
    ).start()
    return redirect(url_for('download_status', download_id=new_id))

@app.route('/status/<download_id>')
def download_status(download_id):
    return render('status.html', download_id=download_id)

@app.route('/api/status/<download_id>')
def get_download_status(download_id):
    with _status_lock:
        status = download_status_dict.get(download_id)
    if not status:
        return jsonify({'status': 'not_found', 'error': 'Not found'})
    return jsonify(_public_status(status))

@app.route('/download/<download_id>')
def download_file(download_id):
    with _status_lock:
        status = download_status_dict.get(download_id, {}).copy()
    if status.get('status') != 'completed' or not status.get('zip_file'):
        flash('Download not ready.', 'error')
        return redirect(url_for('index'))

    file_path = status['zip_file']
    if not os.path.exists(file_path):
        _update_status(download_id, file_deleted=True)
        flash('File no longer on the server (it may have been cleaned up).', 'error')
        return redirect(url_for('downloads_page'))

    safe = "".join(c for c in status.get('playlist_name', 'download')
                   if c.isalnum() or c in ' -_').strip() or 'download'
    ext  = os.path.splitext(file_path)[1] or '.zip'
    name = f"{safe}{ext}"
    _update_status(download_id, downloaded_at=datetime.now().isoformat())
    _persist_sessions()
    return send_file(file_path, as_attachment=True, download_name=name)

# ── History ──────────────────────────────────────────────────────────────────

@app.route('/downloads')
def downloads_page():
    items = []
    with _status_lock:
        for dl_id, s in reversed(list(download_status_dict.items())):
            entry = _public_status(s)
            entry['id'] = dl_id
            entry['file_exists'] = bool(s.get('zip_file') and os.path.exists(s['zip_file']))
            items.append(entry)
    stats = {
        'total':     len(items),
        'completed': sum(1 for s in items if s.get('status') == 'completed'),
        'active':    sum(1 for s in items if s.get('status') in ACTIVE_STATUSES),
    }
    return render('downloads.html', downloads=items, stats=stats)

@app.route('/clear_downloads')
def clear_downloads():
    with _status_lock:
        to_del = [k for k, v in download_status_dict.items()
                  if v.get('status') not in ACTIVE_STATUSES]
        for k in to_del:
            s = download_status_dict.pop(k)
            _delete_session_file(s)
            zd = DOWNLOADS_DIR / k
            if zd.exists():
                shutil.rmtree(zd, ignore_errors=True)
    _persist_sessions()
    flash(f'Cleared {len(to_del)} download(s).', 'success')
    return redirect(url_for('downloads_page'))

@app.route('/delete_download/<dl_id>')
def delete_download(dl_id):
    with _status_lock:
        s = download_status_dict.pop(dl_id, None)
    if s:
        _delete_session_file(s)
        zd = DOWNLOADS_DIR / dl_id
        if zd.exists():
            shutil.rmtree(zd, ignore_errors=True)
    _persist_sessions()
    flash('Removed.', 'success')
    return redirect(url_for('downloads_page'))

# ── Settings ─────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        try:
            app_settings['max_songs']        = max(1, min(500, int(request.form.get('max_songs', 100))))
            app_settings['chunk_size']       = max(1, min(100, int(request.form.get('chunk_size', 25))))
            app_settings['parallel_workers'] = max(1, min(8,   int(request.form.get('parallel_workers', 4))))
            app_settings['retention_hours']  = max(1, min(720, int(request.form.get('retention_hours', 24))))
        except ValueError:
            pass
        app_settings['audio_quality'] = request.form.get('audio_quality', 'mp3-320')
        app_settings['video_quality'] = request.form.get('video_quality', '720p')
        _save_settings()
        flash('Preferences saved!', 'success')
        return redirect(url_for('settings_page'))
    return render('settings.html')

@app.route('/test_connection')
def test_connection():
    try:
        _, tracks = fetch_playlist_tracks('https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M')
        return jsonify({'status': 'success',
                        'message': f'Spotify embed connected — {len(tracks)} tracks loaded.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# ── Deploy guide ─────────────────────────────────────────────────────────────

@app.route('/deploy')
def deploy_page():
    return render('deploy.html')

# ── Browse + preview ─────────────────────────────────────────────────────────

@app.route('/browse', methods=['POST', 'GET'])
def browse():
    playlist_url = (request.form.get('playlist_url') or request.args.get('url', '')).strip()
    if not playlist_url:
        flash('Please enter a Spotify URL.', 'error')
        return redirect(url_for('index'))
    try:
        name, tracks = fetch_playlist_tracks(playlist_url)
        return render('browse.html', playlist_name=name, tracks=tracks,
                      playlist_url=playlist_url)
    except Exception as e:
        flash(str(e), 'error')
        return redirect(url_for('index'))

@app.route('/track_art')
def track_art():
    spotify_url = request.args.get('url', '').strip()
    if not spotify_url:
        return jsonify({'cover_url': ''})
    meta = _fetch_track_embed_meta(spotify_url)
    return jsonify({'cover_url': meta.get('cover_url', '')})

# ── Streaming proxy (full song playback) ─────────────────────────────────────

_stream_cache: dict = {}
_stream_lock = threading.Lock()

@app.route('/stream')
def stream_audio():
    artist = request.args.get('artist', '').strip()
    title  = request.args.get('title',  '').strip()
    if not artist or not title:
        return 'Missing artist or title parameter', 400

    key = f"{artist.lower()}::{title.lower()}"
    audio_url = mime_type = None
    with _stream_lock:
        cached = _stream_cache.get(key)
        if cached and time.time() < cached['expires']:
            audio_url, mime_type = cached['url'], cached['mime']

    if not audio_url:
        try:
            ydl_opts = {
                'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
                'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
            }
            if _FFMPEG_LOCATION:
                ydl_opts['ffmpeg_location'] = _FFMPEG_LOCATION
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{artist} {title}", download=False)
            if not info or not info.get('entries'):
                return 'Track not found on YouTube', 404
            entry    = info['entries'][0]
            best_url = entry.get('url', '')
            mime_type = 'audio/webm'
            for fmt in sorted(entry.get('formats', []),
                              key=lambda x: x.get('abr') or 0, reverse=True):
                if (fmt.get('acodec') not in (None, '', 'none')
                        and fmt.get('vcodec') in (None, '', 'none')
                        and fmt.get('url')):
                    best_url  = fmt['url']
                    mime_type = f"audio/{fmt.get('ext', 'webm')}"
                    break
            audio_url = best_url
            with _stream_lock:
                _stream_cache[key] = {'url': audio_url, 'mime': mime_type,
                                      'expires': time.time() + 300}
        except Exception as e:
            return f'Stream error: {e}', 500

    if not audio_url:
        return 'No audio stream found', 404

    headers = {'User-Agent': 'Mozilla/5.0',
               'Referer':    'https://www.youtube.com/'}
    if request.headers.get('Range'):
        headers['Range'] = request.headers['Range']

    try:
        up = _req.get(audio_url, headers=headers, stream=True, timeout=30)
        if up.status_code in (403, 410):
            with _stream_lock:
                _stream_cache.pop(key, None)
            return 'Stream URL expired — press play again', 503

        def gen():
            for chunk in up.iter_content(chunk_size=32768):
                if chunk:
                    yield chunk

        resp_headers = {'Content-Type':  up.headers.get('Content-Type', mime_type),
                        'Accept-Ranges': 'bytes'}
        for h in ('Content-Range', 'Content-Length'):
            if h in up.headers:
                resp_headers[h] = up.headers[h]
        return Response(gen(), status=up.status_code, headers=resp_headers)
    except Exception as e:
        return f'Proxy error: {e}', 500

# ── YouTube video download ───────────────────────────────────────────────────

@app.route('/download_video', methods=['POST'])
def download_video():
    video_url     = request.form.get('video_url', '').strip()
    video_quality = request.form.get('video_quality', app_settings['video_quality'])
    if not video_url:
        flash('Please enter a YouTube URL.', 'error')
        return redirect(url_for('index'))

    fmt_map = {
        '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best',
        '720p':  'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best',
        '480p':  'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best',
        '360p':  'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best',
        'best':  'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    }
    fmt   = fmt_map.get(video_quality, 'bestvideo+bestaudio/best')
    dl_id = _new_id('video')

    _set_status(dl_id, {
        'id': dl_id, 'status': 'downloading', 'progress': 5,
        'current_song': 'Connecting to YouTube…',
        'started_at': datetime.now().isoformat(),
        'playlist_name': 'Video', 'quality': video_quality,
        'chunk_label': '', 'has_next': False,
        'downloaded': 0, 'total': 1, 'file_deleted': False,
    })

    def run():
        try:
            out_dir = DOWNLOADS_DIR / dl_id
            out_dir.mkdir(parents=True, exist_ok=True)

            def progress_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    dled  = d.get('downloaded_bytes', 0)
                    pct   = int((dled / total) * 88) if total else 10
                    speed = d.get('_speed_str', '')
                    eta   = d.get('_eta_str', '')
                    _update_status(dl_id, progress=max(5, pct),
                        current_song=f"Downloading… {speed} · ETA {eta}".strip(' ·'))
                elif d['status'] == 'finished':
                    _update_status(dl_id, progress=92, current_song='Merging audio & video…')

            _video_opts = {
                'format':              fmt,
                'outtmpl':             str(out_dir / '%(title)s.%(ext)s'),
                'quiet':               True,
                'no_warnings':         True,
                'nocheckcertificate':  True,
                'merge_output_format': 'mp4',
                'progress_hooks':      [progress_hook],
                'concurrent_fragment_downloads': 4,
            }
            if _FFMPEG_LOCATION:
                _video_opts['ffmpeg_location'] = _FFMPEG_LOCATION
            with yt_dlp.YoutubeDL(_video_opts) as ydl:
                info  = ydl.extract_info(video_url, download=True)
                title = info.get('title', 'video')

            files  = list(out_dir.iterdir())
            mp4s   = [f for f in files if f.suffix == '.mp4']
            chosen = mp4s[0] if mp4s else (files[0] if files else None)
            if not chosen:
                raise RuntimeError("yt-dlp produced no output file.")

            _update_status(dl_id, status='completed', progress=100,
                zip_file=str(chosen), file_size=chosen.stat().st_size,
                playlist_name=title, downloaded=1, total=1,
                current_song='', completed_at=datetime.now().isoformat())
            _persist_sessions()
        except Exception as e:
            _update_status(dl_id, status='error', error=str(e),
                completed_at=datetime.now().isoformat())
            _persist_sessions()

    threading.Thread(target=run, daemon=True).start()
    return redirect(url_for('download_status', download_id=dl_id))

# ── Search, stats, QR, share ─────────────────────────────────────────────────

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'tracks': [], 'playlists': [], 'albums': []})
    types_arg = request.args.get('types', 'track,playlist,album')
    types = tuple(t.strip() for t in types_arg.split(',') if t.strip()
                  in ('track', 'playlist', 'album'))
    if not types:
        types = ('track', 'playlist', 'album')
    try:
        limit = max(1, min(20, int(request.args.get('limit', 8))))
    except ValueError:
        limit = 8
    return jsonify(spotify_search(q, types=types, limit=limit))

@app.route('/api/stats')
def api_stats():
    with _status_lock:
        items = list(download_status_dict.values())
    completed   = [s for s in items if s.get('status') == 'completed']
    total_size  = sum(s.get('file_size', 0) or 0 for s in completed)
    total_songs = sum(s.get('downloaded', 0) or 0 for s in completed)
    active      = sum(1 for s in items if s.get('status') in ACTIVE_STATUSES)
    return jsonify({
        'total_downloads': len(completed),
        'total_size':      total_size,
        'total_songs':     total_songs,
        'active':          active,
    })

@app.route('/qr/<dl_id>')
def qr_for_download(dl_id):
    """Return a QR code PNG that points at this app's /download/<dl_id>."""
    with _status_lock:
        s = download_status_dict.get(dl_id)
    if not s or s.get('status') != 'completed':
        return 'Not ready', 404
    target = request.host_url.rstrip('/') + url_for('download_file', download_id=dl_id)
    img = qrcode.make(target, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    resp = send_file(buf, mimetype='image/png',
                     download_name=f'{dl_id}_qr.png')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

# ─────────────────────────────────────────────────────────────────────────────
# Hidden admin console routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login_page():
    if not _ADMIN_ENABLED:
        return ('Admin console disabled. Set SPOTDL_ADMIN_PASSWORD to enable.', 404)
    nxt = request.values.get('next') or url_for('admin_console')
    if not nxt.startswith('/admin'):
        nxt = url_for('admin_console')
    error = None
    if request.method == 'POST':
        pw = (request.form.get('password') or '').strip()
        if pw and pw == _ADMIN_PASSWORD:
            session['admin_authed'] = True
            session.permanent = True
            _admin_log(f'Admin unlocked from {request.remote_addr}', 'INFO')
            return redirect(nxt)
        _admin_log(f'Failed admin login from {request.remote_addr}', 'WARN')
        error = 'Wrong code. Try again.'
    return render_template('admin_login.html', error=error, next=nxt)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_authed', None)
    return redirect(url_for('landing') if _AUTH_ENABLED else url_for('index'))

@app.route('/admin')
def admin_console():
    guard = _require_admin()
    if guard is not None:
        return guard
    return render_template('admin.html', settings=app_settings)

def _disk_stats() -> dict:
    try:
        usage = shutil.disk_usage(str(DOWNLOADS_DIR))
        return {'total': usage.total, 'used': usage.used, 'free': usage.free}
    except Exception:
        return {'total': 0, 'used': 0, 'free': 0}

def _storage_stats() -> dict:
    files = list(DOWNLOADS_DIR.glob('*.zip')) + list(DOWNLOADS_DIR.glob('*.mp3')) \
            + list(DOWNLOADS_DIR.glob('*.mp4'))
    total_bytes = sum(f.stat().st_size for f in files if f.exists())
    return {
        'file_count': len(files),
        'total_bytes': total_bytes,
        'oldest': min((f.stat().st_mtime for f in files), default=0),
        'newest': max((f.stat().st_mtime for f in files), default=0),
    }

@app.route('/admin/api/state')
def admin_api_state():
    guard = _require_admin()
    if guard is not None:
        return guard
    with _status_lock:
        items = list(download_status_dict.values())
    queue = [_public_status(s) for s in items
             if s.get('status') in ACTIVE_STATUSES]
    history = [_public_status(s) for s in items
               if s.get('status') not in ACTIVE_STATUSES][-50:]
    return jsonify({
        'system': {
            'python':     sys.version.split()[0],
            'platform':   platform.platform(),
            'pid':        os.getpid(),
            'uptime_sec': int(time.time() - _START_TIME),
            'cwd':        str(ROOT_DIR),
            'auth_on':    _AUTH_ENABLED,
            'time':       datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        },
        'disk':    _disk_stats(),
        'storage': _storage_stats(),
        'queue':   queue,
        'history': history,
        'settings': app_settings,
        'totals': {
            'sessions': len(items),
            'active':   len(queue),
        },
    })

@app.route('/admin/api/logs')
def admin_api_logs():
    guard = _require_admin()
    if guard is not None:
        return guard
    n = max(1, min(500, int(request.args.get('n', 200))))
    with _LOG_LOCK:
        lines = list(_LOG_BUFFER)[-n:]
    return jsonify({'lines': lines, 'total': len(_LOG_BUFFER)})

@app.route('/admin/api/cleanup', methods=['POST'])
def admin_api_cleanup():
    guard = _require_admin()
    if guard is not None:
        return guard
    removed = 0
    cutoff = time.time() - app_settings['retention_hours'] * 3600
    with _status_lock:
        for dl_id, s in list(download_status_dict.items()):
            if s.get('status') in ACTIVE_STATUSES:
                continue
            mtime = 0
            zp = s.get('zip_path')
            if zp and Path(zp).exists():
                mtime = Path(zp).stat().st_mtime
            if mtime and mtime < cutoff:
                _delete_session_file(s)
                download_status_dict.pop(dl_id, None)
                removed += 1
    _persist_sessions()
    _admin_log(f'Manual cleanup removed {removed} sessions', 'INFO')
    return jsonify({'removed': removed})

@app.route('/admin/api/cancel-all', methods=['POST'])
def admin_api_cancel_all():
    guard = _require_admin()
    if guard is not None:
        return guard
    cancelled = 0
    with _status_lock:
        for s in download_status_dict.values():
            if s.get('status') in ACTIVE_STATUSES:
                s['status']  = 'cancelled'
                s['message'] = 'Cancelled from admin console'
                cancelled += 1
    _persist_sessions()
    _admin_log(f'Admin cancelled {cancelled} active downloads', 'WARN')
    return jsonify({'cancelled': cancelled})

@app.route('/admin/api/clear-history', methods=['POST'])
def admin_api_clear_history():
    guard = _require_admin()
    if guard is not None:
        return guard
    removed = 0
    with _status_lock:
        for dl_id, s in list(download_status_dict.items()):
            if s.get('status') in ACTIVE_STATUSES:
                continue
            _delete_session_file(s)
            download_status_dict.pop(dl_id, None)
            removed += 1
    _persist_sessions()
    _admin_log(f'Admin cleared {removed} history entries', 'WARN')
    return jsonify({'removed': removed})

# ─────────────────────────────────────────────────────────────────────────────

_START_TIME = time.time()

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port  = int(os.environ.get('PORT', 5000))
    if _ADMIN_ENABLED:
        log.info('Admin console enabled at /admin (7-tap logo to reveal).')
    else:
        log.info('Admin console disabled. Set SPOTDL_ADMIN_PASSWORD to enable.')
    # Bind to localhost only for Android security
    app.run(host='127.0.0.1', port=port, debug=debug, threaded=True)
