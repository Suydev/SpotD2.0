#!/usr/bin/env python3
"""
SpotDL — Spotify & YouTube Downloader
Uses Spotify's public web-player token — no credentials, no Premium required.
"""

from flask import (Flask, render_template, request, jsonify, send_file,
                   flash, redirect, url_for)
import os, sys, json, zipfile, tempfile, threading, time, shutil, re
from datetime import datetime, timedelta
import requests as _req
sys.path.append('.')
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'spotdl-secret-key')

# ─────────────────────────────────────────────────────────────────────────────
# Spotify data — scraped from the public embed page.
# No API key, no login, no Premium required. Works for any public playlist.
# ─────────────────────────────────────────────────────────────────────────────

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# ─────────────────────────────────────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────────────────────────────────────

download_status_dict: dict = {}

DEFAULT_SETTINGS = {
    'max_songs':     60,
    'chunk_size':    15,
    'audio_quality': 'mp3-320',
    'video_quality': '720p',
}
app_settings = dict(DEFAULT_SETTINGS)

# Persist preferences across restarts
CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'config.json')

def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_config(data: dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    cfg = _load_config()
    cfg.update(data)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

def _load_settings():
    cfg = _load_config()
    for k in DEFAULT_SETTINGS:
        if k in cfg:
            app_settings[k] = cfg[k]

_load_settings()

# ─────────────────────────────────────────────────────────────────────────────
# File cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _delete_session_file(status: dict):
    fp = status.get('zip_file')
    if fp and os.path.exists(fp):
        try:
            os.remove(fp)
        except Exception as e:
            print(f"[cleanup] Could not delete {fp}: {e}")
    if fp:
        parent = os.path.dirname(fp)
        if parent != tempfile.gettempdir() and os.path.isdir(parent):
            shutil.rmtree(parent, ignore_errors=True)

def _schedule_file_delete(path: str, delay: int = 60):
    def _do():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"[cleanup] Deleted post-download: {os.path.basename(path)}")
        except Exception as e:
            print(f"[cleanup] Error deleting {path}: {e}")
        parent = os.path.dirname(path)
        if parent != tempfile.gettempdir() and os.path.isdir(parent):
            shutil.rmtree(parent, ignore_errors=True)
    threading.Thread(target=_do, daemon=True).start()

def _cleanup_worker():
    while True:
        time.sleep(1800)
        cutoff = datetime.now() - timedelta(hours=2)
        active  = {'initializing', 'fetching_playlist', 'downloading', 'creating_zip'}
        to_del  = []
        for dl_id, s in list(download_status_dict.items()):
            ts_str = s.get('completed_at') or s.get('started_at')
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except Exception:
                continue
            if ts < cutoff and s.get('status') not in active:
                to_del.append(dl_id)
        for dl_id in to_del:
            s = download_status_dict.pop(dl_id, {})
            _delete_session_file(s)
        if to_del:
            print(f"[cleanup] Removed {len(to_del)} expired session(s)")

threading.Thread(target=_cleanup_worker, daemon=True, name='cleanup').start()

# ─────────────────────────────────────────────────────────────────────────────
# Spotify helpers — public API (no credentials)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_spotify_url(url: str) -> tuple[str, str]:
    """
    Return (spotify_type, spotify_id) for a playlist or album URL.
    Accepts open.spotify.com links and spotify: URIs.
    """
    url = url.strip().split('?')[0]
    for kind in ('playlist', 'album', 'track'):
        if f'/{kind}/' in url:
            return kind, url.split(f'/{kind}/')[1].split('/')[0]
        if f'spotify:{kind}:' in url:
            return kind, url.split(f'spotify:{kind}:')[1]
    raise ValueError(
        "Unsupported Spotify URL. Please paste a playlist or album link "
        "from open.spotify.com."
    )

def fetch_playlist_tracks(playlist_url: str):
    """
    Return (name, list_of_track_dicts) by scraping the public Spotify embed page.
    Supports playlist and album URLs.
    No API key, no login, no Premium required.  Returns up to 100 tracks.

    Each track dict has: name, artist, album, spotify_url, track_number,
                         cover_url (album art, pre-filled for albums), year.
    """
    kind, sid = _parse_spotify_url(playlist_url)
    if kind not in ('playlist', 'album'):
        raise ValueError("Please paste a Spotify playlist or album URL, not a single track link.")

    embed_url = f'https://embed.spotify.com/?uri=spotify:{kind}:{sid}'
    r = _req.get(embed_url, headers=_BROWSER_HEADERS, timeout=20)
    r.raise_for_status()

    scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
    for s in scripts:
        if 'trackList' not in s:
            continue
        try:
            data   = json.loads(s)
            entity = data['props']['pageProps']['state']['data']['entity']
            name   = entity.get('name') or entity.get('title') or 'Playlist'

            # Cover art (album-level; for playlists each track is fetched individually later)
            vi         = entity.get('visualIdentity') or {}
            imgs       = sorted(vi.get('image') or [], key=lambda x: x.get('maxWidth') or 0, reverse=True)
            entity_art = imgs[0]['url'] if imgs else ''

            # Release year (available for albums, sometimes None)
            rd          = entity.get('releaseDate') or {}
            entity_year = rd.get('isoString', '')[:4] if isinstance(rd, dict) else ''

            tracks = []
            for idx, t in enumerate(entity.get('trackList', []), start=1):
                title    = t.get('title', '')
                subtitle = t.get('subtitle', '')
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
                    'cover_url':    entity_art if kind == 'album' else '',
                    'year':         entity_year if kind == 'album' else '',
                    'preview_url':  preview_url,   # Spotify 30-second preview (MP3, CORS *)
                })
            return name, tracks
        except (json.JSONDecodeError, KeyError):
            continue

    raise RuntimeError(
        "Could not parse track list from the Spotify embed. "
        "Make sure the playlist or album is public and the URL is correct."
    )


def search_public_playlists(q: str) -> list:
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Per-track metadata: cover art, year, album name
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_track_embed_meta(spotify_url: str) -> dict:
    """
    Given a Spotify track URL or URI, scrape the embed page to get
    cover art URL, release year, and album name.
    Returns a dict with keys: cover_url, year, album  (all may be empty).
    """
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
        r = _req.get(
            f'https://embed.spotify.com/?uri=spotify:track:{tid}',
            headers=_BROWSER_HEADERS, timeout=12,
        )
        if not r.ok:
            return {}
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', r.text, re.DOTALL)
        for s in scripts:
            if 'entity' not in s:
                continue
            try:
                data   = json.loads(s)
                entity = data['props']['pageProps']['state']['data']['entity']
                vi     = entity.get('visualIdentity') or {}
                imgs   = sorted(vi.get('image') or [], key=lambda x: x.get('maxWidth') or 0, reverse=True)
                cover  = imgs[0]['url'] if imgs else ''
                rd     = entity.get('releaseDate') or {}
                year   = rd.get('isoString', '')[:4] if isinstance(rd, dict) else ''
                album  = entity.get('subtitle') or ''
                return {'cover_url': cover, 'year': year, 'album': album}
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    except Exception:
        pass
    return {}


def _download_image(url: str) -> bytes | None:
    """Download a URL and return raw bytes (used for cover art)."""
    try:
        r = _req.get(url, timeout=10, headers=_BROWSER_HEADERS)
        if r.ok and r.content:
            return r.content
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Lyrics — lrclib.net (free, no auth, no rate limit)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_lyrics(title: str, artist: str, album: str = '') -> str:
    """Return plain-text lyrics, or '' if not found."""
    try:
        params: dict = {'track_name': title, 'artist_name': artist}
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
# Audio file tagger — embeds metadata with mutagen (MP3 / FLAC)
# ─────────────────────────────────────────────────────────────────────────────

def tag_audio_file(path: str, track: dict, cover_bytes: bytes | None, lyrics: str):
    """
    Write ID3 (MP3) or Vorbis (FLAC) tags from `track` dict.
    Fields written: title, artist, album, album artist, year, track number,
                    cover art (APIC/PICTURE), lyrics (USLT/LYRICS).
    Fields explicitly NOT written: publisher, label, comment.
    """
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
            # Remove unwanted tags
            for frame in ('TPUB', 'COMM', 'TCOP', 'TENC'):
                tags.delall(frame)
            # Write wanted tags
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
                tags['APIC:'] = APIC(
                    encoding=3, mime='image/jpeg', type=3,
                    desc='Cover', data=cover_bytes,
                )
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
                pic       = Picture()
                pic.type  = 3
                pic.mime  = 'image/jpeg'
                pic.desc  = 'Cover'
                pic.data  = cover_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()

    except Exception as e:
        print(f'  Tagging failed for {path}: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# Downloader
# ─────────────────────────────────────────────────────────────────────────────

class Downloader:
    def _audio_ydl_opts(self, out_dir: str, safe_name: str, quality: str) -> dict:
        codec   = 'flac' if quality == 'flac' else 'mp3'
        bitrate = {'mp3-128': '128', 'mp3-192': '192', 'mp3-320': '320', 'flac': '0'}.get(quality, '320')
        return {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(out_dir, f'{safe_name}.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': codec,
                'preferredquality': bitrate,
            }],
            'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
            'no_color': True, 'socket_timeout': 30, 'retries': 5,
        }

    def download_single_track(self, track_info: dict, out_dir: str, quality: str = 'mp3-320'):
        safe  = re.sub(r'[^\w\s-]', '', f"{track_info['artist']} - {track_info['name']}")
        safe  = re.sub(r'[-\s]+', '-', safe).strip('-')[:150]
        codec = 'flac' if quality == 'flac' else 'mp3'
        queries = [
            f"{track_info['artist']} {track_info['name']}",
            f"{track_info['name']} {track_info['artist']} lyrics",
            f"{track_info['artist']} {track_info['name']} official audio",
        ]

        found_path = None
        for i, q in enumerate(queries):
            try:
                with yt_dlp.YoutubeDL(self._audio_ydl_opts(out_dir, safe, quality)) as ydl:
                    ydl.extract_info(f"ytsearch1:{q}", download=True)
                for f in os.listdir(out_dir):
                    if safe in f and f.endswith(f'.{codec}'):
                        found_path = os.path.join(out_dir, f)
                        break
                if found_path:
                    break
            except Exception as e:
                print(f"  Attempt {i+1} failed for '{track_info['name']}': {e}")
                time.sleep(1)

        if not found_path:
            return None

        # ── Enrich metadata for playlist tracks (albums already have this) ──
        enriched = dict(track_info)
        if not enriched.get('cover_url') and enriched.get('spotify_url'):
            meta = _fetch_track_embed_meta(enriched['spotify_url'])
            if meta.get('cover_url'):
                enriched['cover_url'] = meta['cover_url']
            if meta.get('year') and not enriched.get('year'):
                enriched['year'] = meta['year']
            if meta.get('album') and not enriched.get('album'):
                enriched['album'] = meta['album']

        # ── Cover art bytes ──
        cover_bytes = _download_image(enriched['cover_url']) if enriched.get('cover_url') else None

        # ── Lyrics ──
        lyrics = fetch_lyrics(
            enriched.get('name', ''),
            enriched.get('artist', ''),
            enriched.get('album', ''),
        )

        # ── Tag the audio file ──
        tag_audio_file(found_path, enriched, cover_bytes, lyrics)

        return found_path

    def download_chunk(self, dl_id: str, all_tracks: list, start: int, end: int,
                       playlist_name: str, quality: str, chunk_size: int,
                       prev_zip: str | None = None):
        try:
            chunk    = all_tracks[start:end]
            total    = len(chunk)
            has_next = end < len(all_tracks)

            download_status_dict[dl_id].update({
                'status':        'downloading',
                'total':         total,
                'playlist_name': playlist_name,
                'chunk_label':   f"Songs {start+1}–{end} of {len(all_tracks)}",
                'has_next':      has_next,
                'next_start':    end if has_next else None,
                'quality':       quality,
                'chunk_size':    chunk_size,
            })

            out_dir = tempfile.mkdtemp(prefix=f'spotdl_{dl_id}_')
            done    = []

            for i, track in enumerate(chunk):
                download_status_dict[dl_id].update({
                    'current_song': f"{track['name']} — {track['artist']}",
                    'progress':     int((i / total) * 93),
                })
                path = self.download_single_track(track, out_dir, quality)
                if path and os.path.exists(path):
                    done.append(path)
                    download_status_dict[dl_id]['downloaded'] = len(done)

            if not done:
                raise Exception("No songs could be downloaded.")

            # Delete previous chunk ZIP before creating this one
            if prev_zip and os.path.exists(prev_zip):
                try:
                    os.remove(prev_zip)
                except Exception:
                    pass

            download_status_dict[dl_id]['status'] = 'creating_zip'
            zip_path = os.path.join(tempfile.gettempdir(), f'spotdl_{dl_id}.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for fp in done:
                    zf.write(fp, os.path.basename(fp))
            shutil.rmtree(out_dir, ignore_errors=True)

            download_status_dict[dl_id].update({
                'status':       'completed',
                'progress':     100,
                'zip_file':     zip_path,
                'downloaded':   len(done),
                'completed_at': datetime.now().isoformat(),
                'file_deleted': False,
            })
        except Exception as e:
            download_status_dict[dl_id].update({
                'status':       'error',
                'error':        str(e),
                'completed_at': datetime.now().isoformat(),
            })


downloader = Downloader()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _active_count() -> int:
    active = {'initializing', 'fetching_playlist', 'downloading', 'creating_zip'}
    return sum(1 for s in download_status_dict.values() if s.get('status') in active)

def render(template, **kw):
    kw.setdefault('active_count', _active_count())
    kw.setdefault('settings', app_settings)
    return render_template(template, **kw)

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Home
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render('index.html')

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Spotify playlist download
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/start_download', methods=['POST'])
def start_download():
    playlist_url = request.form.get('playlist_url', '').strip()
    quality      = request.form.get('audio_quality', app_settings['audio_quality'])
    max_songs    = min(int(request.form.get('max_songs',   app_settings['max_songs'])), 300)
    chunk_size   = min(int(request.form.get('chunk_size',  app_settings['chunk_size'])), 50)

    if not playlist_url or ('open.spotify.com/playlist/' not in playlist_url
                            and 'open.spotify.com/album/' not in playlist_url):
        flash('Please enter a valid Spotify playlist or album URL.', 'error')
        return redirect(url_for('index'))

    dl_id = f"dl_{int(time.time())}_{abs(hash(playlist_url)) % 9999}"
    download_status_dict[dl_id] = {
        'id': dl_id, 'status': 'fetching_playlist', 'progress': 0,
        'current_song': '', 'downloaded': 0, 'total': 0,
        'error': None, 'zip_file': None, 'file_deleted': False,
        'started_at': datetime.now().isoformat(),
        'playlist_name': 'Fetching…', 'chunk_label': '',
        'has_next': False, 'next_start': None,
        'playlist_url': playlist_url, 'quality': quality,
        'max_songs': max_songs, 'chunk_size': chunk_size,
        'all_tracks': [],
    }

    def run():
        try:
            name, tracks = fetch_playlist_tracks(playlist_url)
            tracks = tracks[:max_songs]
            download_status_dict[dl_id]['all_tracks']    = tracks
            download_status_dict[dl_id]['playlist_name'] = name
            end = min(chunk_size, len(tracks))
            downloader.download_chunk(dl_id, tracks, 0, end, name, quality, chunk_size)
        except Exception as e:
            download_status_dict[dl_id].update({'status': 'error', 'error': str(e)})

    threading.Thread(target=run, daemon=True).start()
    return redirect(url_for('download_status', download_id=dl_id))


@app.route('/next_chunk/<dl_id>')
def next_chunk(dl_id):
    status = download_status_dict.get(dl_id)
    if not status:
        flash('Session not found.', 'error')
        return redirect(url_for('index'))

    next_start    = status.get('next_start')
    all_tracks    = status.get('all_tracks', [])
    playlist_url  = status.get('playlist_url', '')
    quality       = status.get('quality',      app_settings['audio_quality'])
    chunk_size    = status.get('chunk_size',   app_settings['chunk_size'])
    prev_zip      = status.get('zip_file')
    playlist_name = status.get('playlist_name', 'Playlist')

    if next_start is None or next_start >= len(all_tracks):
        flash('No more songs.', 'error')
        return redirect(url_for('index'))

    new_id = f"dl_{int(time.time())}_{abs(hash(playlist_url + str(next_start))) % 9999}"
    end    = min(next_start + chunk_size, len(all_tracks))
    download_status_dict[new_id] = {
        'id': new_id, 'status': 'initializing', 'progress': 0,
        'current_song': '', 'downloaded': 0, 'total': 0,
        'error': None, 'zip_file': None, 'file_deleted': False,
        'started_at': datetime.now().isoformat(),
        'playlist_name': playlist_name, 'chunk_label': '',
        'has_next': False, 'next_start': None,
        'playlist_url': playlist_url, 'quality': quality,
        'chunk_size': chunk_size, 'all_tracks': all_tracks,
    }
    threading.Thread(
        target=downloader.download_chunk,
        args=(new_id, all_tracks, next_start, end, playlist_name, quality, chunk_size, prev_zip),
        daemon=True,
    ).start()
    return redirect(url_for('download_status', download_id=new_id))


@app.route('/status/<download_id>')
def download_status(download_id):
    return render('status.html', download_id=download_id)


@app.route('/api/status/<download_id>')
def get_download_status(download_id):
    status = download_status_dict.get(download_id, {'status': 'not_found', 'error': 'Not found'})
    return jsonify({k: v for k, v in status.items() if k != 'all_tracks'})


@app.route('/download/<download_id>')
def download_file(download_id):
    status = download_status_dict.get(download_id, {})

    if status.get('status') != 'completed' or not status.get('zip_file'):
        flash('Download not ready.', 'error')
        return redirect(url_for('index'))

    file_path = status['zip_file']

    if not os.path.exists(file_path):
        download_status_dict[download_id]['file_deleted'] = True
        flash('This file was already downloaded and removed from the server.', 'error')
        return redirect(url_for('downloads_page'))

    safe = "".join(c for c in status.get('playlist_name', 'download')
                   if c.isalnum() or c in ' -_').strip()
    ext  = os.path.splitext(file_path)[1] or '.zip'
    name = f"{safe or 'download'}{ext}"

    download_status_dict[download_id]['file_deleted']  = True
    download_status_dict[download_id]['downloaded_at'] = datetime.now().isoformat()
    _schedule_file_delete(file_path, delay=60)

    return send_file(file_path, as_attachment=True, download_name=name)

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Downloads history
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/downloads')
def downloads_page():
    items = []
    for dl_id, s in reversed(list(download_status_dict.items())):
        safe = {k: v for k, v in s.items() if k != 'all_tracks'}
        safe['id'] = dl_id
        items.append(safe)
    active_statuses = {'initializing', 'fetching_playlist', 'downloading', 'creating_zip'}
    stats = {
        'total':     len(items),
        'completed': sum(1 for s in items if s.get('status') == 'completed'),
        'active':    sum(1 for s in items if s.get('status') in active_statuses),
    }
    return render('downloads.html', downloads=items, stats=stats)


@app.route('/clear_downloads')
def clear_downloads():
    active = {'initializing', 'fetching_playlist', 'downloading', 'creating_zip'}
    to_del = [k for k, v in download_status_dict.items() if v.get('status') not in active]
    for k in to_del:
        s = download_status_dict.pop(k)
        _delete_session_file(s)
    flash(f'Cleared {len(to_del)} download(s).', 'success')
    return redirect(url_for('downloads_page'))

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Settings (preferences only — no credentials needed)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    global app_settings

    if request.method == 'POST':
        app_settings['max_songs']     = max(1, min(300, int(request.form.get('max_songs',  60))))
        app_settings['chunk_size']    = max(1, min(50,  int(request.form.get('chunk_size', 15))))
        app_settings['audio_quality'] = request.form.get('audio_quality', 'mp3-320')
        app_settings['video_quality'] = request.form.get('video_quality', '720p')
        _save_config({k: app_settings[k] for k in DEFAULT_SETTINGS})
        flash('Preferences saved!', 'success')
        return redirect(url_for('settings_page'))

    return render('settings.html')


@app.route('/test_connection')
def test_connection():
    try:
        # Test by fetching a small known public playlist via the embed page
        _, tracks = fetch_playlist_tracks('https://open.spotify.com/playlist/1c1jMO1OuHVuaHdx7B83R3')
        if tracks:
            return jsonify({'status': 'success', 'message': f'Spotify embed connected ✅ — {len(tracks)} tracks loaded, no credentials needed'})
        return jsonify({'status': 'error', 'message': 'Embed returned no tracks'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Deploy guide
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/deploy')
def deploy_page():
    return render('deploy.html')

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Search
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/search_playlists')
def search_playlists():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'status': 'error', 'message': 'Query required'})
    try:
        playlists = search_public_playlists(q)
        return jsonify({'status': 'success', 'playlists': playlists})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Browse & Preview
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/browse', methods=['POST', 'GET'])
def browse():
    """Show the track list for a playlist/album with playback buttons."""
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
    """Return the Spotify cover art URL for a given track URL (used for lazy loading)."""
    spotify_url = request.args.get('url', '').strip()
    if not spotify_url:
        return jsonify({'cover_url': ''})
    meta = _fetch_track_embed_meta(spotify_url)
    return jsonify({'cover_url': meta.get('cover_url', '')})


# In-memory cache for YouTube stream URLs (avoid re-extracting on each play)
_stream_cache: dict = {}
_stream_lock = threading.Lock()


@app.route('/stream')
def stream_audio():
    """
    Full-song streaming proxy: searches YouTube for artist+title,
    extracts the best audio-only URL, and proxies it with Range support.
    Results are cached for 5 minutes per track.
    """
    artist = request.args.get('artist', '').strip()
    title  = request.args.get('title',  '').strip()
    if not artist or not title:
        return 'Missing artist or title parameter', 400

    cache_key = f"{artist.lower()}::{title.lower()}"
    audio_url = mime_type = None

    with _stream_lock:
        cached = _stream_cache.get(cache_key)
        if cached and time.time() < cached['expires']:
            audio_url = cached['url']
            mime_type = cached['mime']

    if not audio_url:
        try:
            ydl_opts = {
                'format': 'bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best',
                'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{artist} {title}", download=False)
                if not info or not info.get('entries'):
                    return 'Track not found on YouTube', 404
                entry = info['entries'][0]
                # Pick best audio-only format
                best_url = entry.get('url', '')
                mime_type = 'audio/webm'
                for fmt in sorted(entry.get('formats', []),
                                  key=lambda x: x.get('abr') or 0, reverse=True):
                    if (fmt.get('acodec') not in ('none', None, '')
                            and fmt.get('vcodec') in ('none', None, '')
                            and fmt.get('url')):
                        best_url  = fmt['url']
                        ext       = fmt.get('ext', 'webm')
                        mime_type = f'audio/{ext}'
                        break
                audio_url = best_url
                with _stream_lock:
                    _stream_cache[cache_key] = {
                        'url': audio_url, 'mime': mime_type,
                        'expires': time.time() + 300,
                    }
        except Exception as e:
            return f'Stream error: {e}', 500

    if not audio_url:
        return 'No audio stream found', 404

    # Proxy upstream with Range request support (enables seeking)
    range_header = request.headers.get('Range')
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer':    'https://www.youtube.com/',
    }
    if range_header:
        headers['Range'] = range_header

    try:
        up = _req.get(audio_url, headers=headers, stream=True, timeout=30)

        # If YouTube's signed URL has expired (403/410), clear cache and tell client to retry
        if up.status_code in (403, 410):
            with _stream_lock:
                _stream_cache.pop(cache_key, None)
            return 'Stream URL expired — please press play again', 503

        def generate():
            for chunk in up.iter_content(chunk_size=32768):
                if chunk:
                    yield chunk

        resp_headers = {
            'Content-Type':  up.headers.get('Content-Type', mime_type),
            'Accept-Ranges': 'bytes',
        }
        for h in ('Content-Range', 'Content-Length'):
            if h in up.headers:
                resp_headers[h] = up.headers[h]

        return Response(generate(), status=up.status_code, headers=resp_headers)
    except Exception as e:
        return f'Proxy error: {e}', 500


# ─────────────────────────────────────────────────────────────────────────────
# Routes — YouTube video download
# ─────────────────────────────────────────────────────────────────────────────

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
    dl_id = f"video_{int(time.time())}_{abs(hash(video_url)) % 9999}"

    download_status_dict[dl_id] = {
        'id': dl_id, 'status': 'downloading', 'progress': 5,
        'current_song': 'Connecting to YouTube…',
        'started_at': datetime.now().isoformat(),
        'playlist_name': 'Video', 'quality': video_quality,
        'chunk_label': '', 'has_next': False,
        'downloaded': 0, 'total': 1, 'file_deleted': False,
    }

    def run(url, d_id):
        try:
            tmp = tempfile.mkdtemp(prefix=f'video_{d_id}_')

            def progress_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    dled  = d.get('downloaded_bytes', 0)
                    pct   = int((dled / total) * 88) if total else 10
                    speed = d.get('_speed_str', '')
                    eta   = d.get('_eta_str', '')
                    download_status_dict[d_id].update({
                        'progress':     max(5, pct),
                        'current_song': f"Downloading… {speed} · ETA {eta}".strip(' ·'),
                    })
                elif d['status'] == 'finished':
                    download_status_dict[d_id].update({
                        'progress': 92, 'current_song': 'Merging audio & video…',
                    })

            with yt_dlp.YoutubeDL({
                'format':              fmt,
                'outtmpl':             os.path.join(tmp, '%(title)s.%(ext)s'),
                'quiet':               True,
                'no_warnings':         True,
                'nocheckcertificate':  True,
                'merge_output_format': 'mp4',
                'progress_hooks':      [progress_hook],
            }) as ydl:
                info  = ydl.extract_info(url, download=True)
                title = info.get('title', 'video')

            files = os.listdir(tmp)
            mp4s  = [f for f in files if f.endswith('.mp4')]
            chosen = mp4s[0] if mp4s else (files[0] if files else None)
            if not chosen:
                raise Exception("yt-dlp produced no output file.")

            download_status_dict[d_id].update({
                'status':        'completed',
                'progress':      100,
                'zip_file':      os.path.join(tmp, chosen),
                'playlist_name': title,
                'downloaded':    1, 'total': 1,
                'current_song':  '',
                'completed_at':  datetime.now().isoformat(),
            })
        except Exception as e:
            download_status_dict[d_id].update({
                'status':       'error',
                'error':        str(e),
                'completed_at': datetime.now().isoformat(),
            })

    threading.Thread(target=run, args=(video_url, dl_id), daemon=True).start()
    return redirect(url_for('download_status', download_id=dl_id))

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port  = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=debug)
