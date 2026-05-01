# SpotDL

A modern, dark-themed multi-page web app for downloading Spotify playlists and YouTube videos in high quality, built with Flask.

## Features
- **Spotify Playlist Downloader**: Paste any public Spotify playlist URL to download high-quality audio. No API key, no Premium, no login required — track data is scraped from the public Spotify embed page.
- **YouTube Video Downloader**: Direct video downloads from YouTube URLs (360p–1080p).
- **Flexible Quality**: MP3 128/192/320 kbps or FLAC; video 360p–1080p.
- **Chunked Batch Downloads**: Configurable batch size (default 15 songs/zip). Large playlists download in sequential batches.
- **Auto Cleanup**: Files auto-deleted 60 seconds after download; background cleanup removes old sessions every 30 minutes.
- **Modern UI**: Dark glassmorphism design with real-time progress tracking.
- **ZIP Packaging**: Each batch is zipped for one-click retrieval.

## How Spotify Data is Fetched
Spotify's public embed page (`https://embed.spotify.com/?uri=spotify:playlist:{id}`) contains full track metadata (title, artist) embedded as JSON. The app scrapes this with no credentials whatsoever — completely free for any public playlist (returns up to 100 tracks).

## Project Structure
- `src/web_app.py` — Flask server, routes, embed scraper, downloader
- `src/templates/` — Jinja2 HTML templates (base, index, status, downloads, settings, deploy)
- `src/static/` — PWA manifest, icons
- `data/config.json` — Persisted user preferences (max_songs, chunk_size, audio_quality, video_quality)
- `data/sessions/` — Active download session state
- `downloads/` — ZIP files served to users (auto-cleaned)
- `requirements.txt` — Python dependencies
- `docker-compose.yml` — Container deployment config

## Running
The workflow installs dependencies from `requirements.txt` and starts the Flask server on port 5000:
```
pip install -q -r requirements.txt && python3 src/web_app.py
```

## Deployment
A `docker-compose.yml` is provided. The Deploy page in the app gives step-by-step instructions for free cloud hosting on Koyeb.
# SpotD2.0
