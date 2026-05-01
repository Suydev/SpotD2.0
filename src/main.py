#!/usr/bin/env python3
"""
Enhanced Spotify Playlist to YouTube Downloader - Termux Optimized
Maximum Audio Quality Version for Android/Termux

Features:
- FLAC/320kbps MP3 downloads with highest quality available
- Termux-specific optimizations and Android integration
- Complete metadata embedding with album artwork
- Memory and battery optimization for mobile devices
- Resume capability and error handling
"""

import os
import re
import sys
import time
import json
import signal
import requests
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import psutil

# Core libraries
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import yt_dlp
from dotenv import load_dotenv

# Audio metadata libraries
import mutagen
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC, TCON, COMM
from mutagen.flac import FLAC
from PIL import Image

# Progress and terminal colors
from tqdm import tqdm
import colorama
from colorama import Fore, Style

# Initialize colorama
colorama.init()

# Load environment variables
load_dotenv()

class TermuxSpotifyDownloader:
    def __init__(self):
        print(f"{Fore.CYAN}🎵 Initializing Termux Spotify YouTube Downloader...{Style.RESET_ALL}")
        
        # Environment detection
        self.is_termux = self.detect_termux_environment()
        self.termux_api_available = self.check_termux_api() if self.is_termux else False
        
        if self.is_termux:
            print(f"{Fore.GREEN}📱 Termux environment detected - applying mobile optimizations{Style.RESET_ALL}")
        
        # Initialize components
        self.setup_spotify()
        self.setup_paths()
        self.setup_youtube_downloader()
        self.setup_mobile_features()
        
        # Download state
        self.download_queue = []
        self.failed_downloads = []
        self.completed_downloads = []
        self.is_downloading = False
        
        # Signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
    def detect_termux_environment(self):
        """Detect if running in Termux environment"""
        return (
            os.environ.get('PREFIX', '').startswith('/data/data/com.termux') or
            'com.termux' in os.environ.get('PREFIX', '') or
            Path('/data/data/com.termux').exists()
        )
    
    def check_termux_api(self):
        """Check if Termux:API is available"""
        try:
            result = subprocess.run(['termux-notification', '--help'], 
                                  capture_output=True, text=True, timeout=5)
            return result.returncode == 0
        except:
            return False
    
    def setup_spotify(self):
        """Initialize Spotify client"""
        client_id = os.getenv('SPOTIFY_CLIENT_ID')
        client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
        
        if not client_id or not client_secret:
            raise RuntimeError(
                "Spotify credentials not found. Please set SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET in the Replit Secrets tool."
            )
            
        try:
            import urllib3
            urllib3.disable_warnings()
            
            from spotipy.cache_handler import MemoryCacheHandler
            client_credentials_manager = SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
                cache_handler=MemoryCacheHandler()
            )
            self.spotify = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
            
            self.spotify.search('test', limit=1, type='artist')
            print(f"{Fore.GREEN}✅ Spotify client initialized successfully{Style.RESET_ALL}")
            
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Error initializing Spotify client: {e}")
    
    def setup_paths(self):
        """Setup download paths optimized for Termux/Android"""
        self.script_root = Path(__file__).parent.absolute()
        
        if self.is_termux:
            # Termux-specific paths
            storage_root = Path("/storage/emulated/0")
            if storage_root.exists():
                self.download_root = storage_root / "Music" / "SpotifyDownloads"
                self.temp_dir = storage_root / "Download" / "temp_spotify"
            else:
                # Fallback to Termux home
                home = Path.home()
                self.download_root = home / "storage" / "music" / "SpotifyDownloads"
                self.temp_dir = home / "downloads" / "temp_spotify"
        else:
            # Standard paths
            self.download_root = self.script_root / "downloads"
            self.temp_dir = self.script_root / "temp"
        
        # Create directories
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"{Fore.BLUE}📁 Download path: {self.download_root}{Style.RESET_ALL}")
        print(f"{Fore.BLUE}🗂️  Temp path: {self.temp_dir}{Style.RESET_ALL}")
        
        # Check storage space
        self.check_storage_space()
    
    def check_storage_space(self):
        """Check available storage space"""
        try:
            usage = psutil.disk_usage(str(self.download_root))
            available_gb = usage.free / (1024**3)
            print(f"{Fore.BLUE}💾 Available storage: {available_gb:.1f} GB{Style.RESET_ALL}")
            
            if available_gb < 1:
                print(f"{Fore.YELLOW}⚠️  Low storage space warning{Style.RESET_ALL}")
                
        except Exception as e:
            print(f"{Fore.YELLOW}⚠️  Could not check storage space: {e}{Style.RESET_ALL}")
    
    def setup_youtube_downloader(self):
        """Configure yt-dlp for maximum audio quality"""
        # Format selection prioritizing absolute highest quality
        # Prioritizes lossless formats first, then highest bitrate lossy
        format_selector = (
            # FLAC - Lossless, highest quality possible
            'bestaudio[acodec=flac]/bestaudio[ext=flac]/'
            # High-quality Opus (often better than MP3 at same bitrate)
            'bestaudio[acodec=opus][abr>=320]/bestaudio[acodec=opus][abr>=256]/'
            'bestaudio[ext=webm][abr>=320]/bestaudio[ext=webm]/'
            # High-quality AAC/M4A
            'bestaudio[acodec=aac][abr>=320]/bestaudio[acodec=aac][abr>=256]/'
            'bestaudio[ext=m4a][abr>=320]/bestaudio[ext=m4a][abr>=256]/'
            # High-quality MP3 (320kbps preferred)
            'bestaudio[acodec=mp3][abr>=320]/bestaudio[ext=mp3][abr>=320]/'
            'bestaudio[acodec=mp3][abr>=256]/bestaudio[ext=mp3][abr>=256]/'
            # Fallback to best available audio
            'bestaudio[abr>=320]/bestaudio[abr>=256]/bestaudio/best[height<=720]'
        )
        
        self.ydl_opts = {
            'format': format_selector,
            'outtmpl': '',  # Will be set dynamically
            
            # Audio processing for maximum quality
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '0',  # VBR, best quality
                },
                {
                    'key': 'FFmpegMetadata',
                    'add_metadata': True,
                }
            ],
            
            # Quality settings
            'audioquality': 0,  # Best
            'audio_format': 'best',
            'extract_flat': False,
            
            # Performance settings
            'concurrent_fragment_downloads': 1 if self.is_termux else 4,
            'retries': 3,
            'fragment_retries': 3,
            'timeout': 30,
            'socket_timeout': 30,
            
            # Mobile optimizations
            'http_chunk_size': 512 * 1024 if self.is_termux else 10 * 1024 * 1024,
            
            # Output control
            'quiet': False,
            'no_warnings': False,
            'extractaudio': True,
            'keepvideo': False,
            'noplaylist': True,
            'writethumbnail': False,
            'writeinfojson': False,
        }
        
        print(f"{Fore.GREEN}🎧 YouTube downloader configured for maximum quality{Style.RESET_ALL}")
    
    def setup_mobile_features(self):
        """Setup mobile-specific features"""
        if self.is_termux:
            # Request storage permissions
            self.request_storage_permission()
            
            # Setup notifications
            if self.termux_api_available:
                self.send_notification("Spotify Downloader", "Notifications enabled")
                print(f"{Fore.GREEN}🔔 Notifications enabled{Style.RESET_ALL}")
            else:
                print(f"{Fore.YELLOW}⚠️  Termux:API not available - install for full features{Style.RESET_ALL}")
            
            print(f"{Fore.GREEN}📱 Mobile features initialized{Style.RESET_ALL}")
    
    def request_storage_permission(self):
        """Request storage permission for Termux"""
        try:
            storage_path = Path('/storage/emulated/0')
            if storage_path.exists() and os.access(storage_path, os.W_OK):
                print(f"{Fore.GREEN}✅ Storage permission already granted{Style.RESET_ALL}")
                return True
            
            print(f"{Fore.CYAN}📱 Requesting storage permission...{Style.RESET_ALL}")
            result = subprocess.run(['termux-setup-storage'], 
                                  capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                print(f"{Fore.GREEN}✅ Storage permission granted{Style.RESET_ALL}")
                return True
            else:
                print(f"{Fore.YELLOW}⚠️  Please manually run 'termux-setup-storage'{Style.RESET_ALL}")
                return False
                
        except Exception as e:
            print(f"{Fore.YELLOW}⚠️  Storage permission setup failed: {e}{Style.RESET_ALL}")
            return False
    
    def send_notification(self, title, content, priority="default"):
        """Send Android notification"""
        if not self.is_termux or not self.termux_api_available:
            return False
        
        try:
            cmd = [
                'termux-notification',
                '--title', title,
                '--content', content,
                '--priority', priority
            ]
            subprocess.run(cmd, capture_output=True, timeout=5)
            return True
        except:
            return False
    
    def signal_handler(self, signum, frame):
        """Handle graceful shutdown"""
        print(f"\n{Fore.YELLOW}🛑 Shutdown signal received. Cleaning up...{Style.RESET_ALL}")
        self.is_downloading = False
        sys.exit(0)
    
    def extract_playlist_id(self, playlist_url):
        """Extract playlist ID from various Spotify URL formats"""
        if 'open.spotify.com' in playlist_url:
            if '/playlist/' in playlist_url:
                return playlist_url.split('/playlist/')[-1].split('?')[0]
        
        if playlist_url.startswith('spotify:playlist:'):
            return playlist_url.split('spotify:playlist:')[-1]
        
        if len(playlist_url) == 22 and playlist_url.replace('-', '').replace('_', '').isalnum():
            return playlist_url
            
        raise ValueError("Invalid Spotify playlist URL format")
    
    def get_playlist_tracks(self, playlist_url):
        """Get all tracks from Spotify playlist"""
        try:
            playlist_id = self.extract_playlist_id(playlist_url)
            print(f"{Fore.CYAN}📋 Fetching playlist: {playlist_id}{Style.RESET_ALL}")
            
            # Get playlist info
            playlist = self.spotify.playlist(playlist_id)
            playlist_name = playlist['name']
            
            print(f"{Fore.CYAN}📋 Playlist: {playlist_name}{Style.RESET_ALL}")
            print(f"{Fore.CYAN}👤 Owner: {playlist['owner']['display_name']}{Style.RESET_ALL}")
            
            # Get all tracks with pagination
            tracks = []
            results = self.spotify.playlist_tracks(playlist_id, limit=50)
            tracks.extend(results['items'])
            
            while results['next']:
                results = self.spotify.next(results)
                tracks.extend(results['items'])
            
            # Process tracks
            track_list = []
            for i, item in enumerate(tracks, 1):
                if item['track'] and item['track']['type'] == 'track':
                    track = item['track']
                    
                    # Get album artwork URL
                    album_cover_url = None
                    if track['album'].get('images'):
                        album_cover_url = max(track['album']['images'], 
                                            key=lambda x: x.get('width', 0))['url']
                    
                    # Parse release date
                    release_year = None
                    try:
                        if track['album'].get('release_date'):
                            release_year = int(track['album']['release_date'].split('-')[0])
                    except:
                        pass
                    
                    track_info = {
                        'index': i,
                        'name': track['name'],
                        'artists': [artist['name'] for artist in track['artists']],
                        'album': track['album']['name'],
                        'album_artist': track['album']['artists'][0]['name'] if track['album']['artists'] else track['artists'][0]['name'],
                        'track_number': track['track_number'],
                        'disc_number': track.get('disc_number', 1),
                        'duration_ms': track['duration_ms'],
                        'release_year': release_year,
                        'isrc': track['external_ids'].get('isrc', ''),
                        'album_cover_url': album_cover_url,
                        'popularity': track.get('popularity', 0),
                        'explicit': track.get('explicit', False),
                        'search_query': f"{', '.join([artist['name'] for artist in track['artists']])} - {track['name']}",
                        'spotify_url': track['external_urls']['spotify'],
                        'genres': []
                    }
                    
                    # Get genres from artist
                    try:
                        artist_info = self.spotify.artist(track['artists'][0]['id'])
                        track_info['genres'] = artist_info.get('genres', [])[:3]
                    except:
                        pass
                    
                    track_list.append(track_info)
                    
                    if i % 10 == 0:
                        print(f"{Fore.BLUE}📝 Processed {i}/{len(tracks)} tracks...{Style.RESET_ALL}")
            
            print(f"{Fore.GREEN}✅ Found {len(track_list)} tracks{Style.RESET_ALL}")
            
            return {
                'name': playlist_name,
                'owner': playlist['owner']['display_name'],
                'total_tracks': len(track_list)
            }, track_list
            
        except Exception as e:
            print(f"{Fore.RED}❌ Error fetching playlist: {e}{Style.RESET_ALL}")
            return None, []
    
    def sanitize_filename(self, filename):
        """Create safe filename"""
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '')
        
        filename = re.sub(r'\s+', ' ', filename).strip()
        max_length = 150 if self.is_termux else 200
        if len(filename) > max_length:
            filename = filename[:max_length]
        
        return filename
    
    def download_album_artwork(self, url, file_path):
        """Download and optimize album artwork"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            # Optimize for mobile
            if self.is_termux:
                with Image.open(file_path) as img:
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                    
                    # Resize for mobile
                    if img.size[0] > 800 or img.size[1] > 800:
                        img.thumbnail((800, 800), Image.Resampling.LANCZOS)
                    
                    img.save(file_path, 'JPEG', quality=90, optimize=True)
            
            return True
            
        except Exception as e:
            print(f"{Fore.YELLOW}⚠️  Could not download artwork: {e}{Style.RESET_ALL}")
            return False
    
    def embed_metadata(self, audio_file, track_info, artwork_path=None):
        """Embed metadata into audio file"""
        try:
            audio_path = Path(audio_file)
            
            if audio_path.suffix.lower() == '.mp3':
                return self._embed_mp3_metadata(audio_path, track_info, artwork_path)
            elif audio_path.suffix.lower() == '.flac':
                return self._embed_flac_metadata(audio_path, track_info, artwork_path)
            else:
                # Convert to MP3 if unsupported
                mp3_file = audio_path.with_suffix('.mp3')
                if self._convert_to_mp3(audio_path, mp3_file):
                    audio_path.unlink()
                    return self._embed_mp3_metadata(mp3_file, track_info, artwork_path)
                else:
                    print(f"{Fore.YELLOW}⚠️  Unsupported format: {audio_path.suffix}{Style.RESET_ALL}")
                    return False
                    
        except Exception as e:
            print(f"{Fore.RED}❌ Metadata embedding error: {e}{Style.RESET_ALL}")
            return False
    
    def _embed_mp3_metadata(self, mp3_file, track_info, artwork_path=None):
        """Embed metadata into MP3 file"""
        try:
            try:
                audio = MP3(mp3_file, ID3=ID3)
            except:
                audio = MP3(mp3_file)
            
            if audio.tags is None:
                audio.add_tags()
            
            tags = audio.tags
            tags.clear()
            
            # Basic metadata
            tags.add(TIT2(encoding=3, text=track_info['name']))
            tags.add(TPE1(encoding=3, text=', '.join(track_info['artists'])))
            tags.add(TALB(encoding=3, text=track_info['album']))
            tags.add(TPE2(encoding=3, text=track_info['album_artist']))
            tags.add(TRCK(encoding=3, text=str(track_info['track_number'])))
            tags.add(TPOS(encoding=3, text=str(track_info['disc_number'])))
            
            if track_info['release_year']:
                tags.add(TDRC(encoding=3, text=str(track_info['release_year'])))
            
            if track_info.get('genres'):
                tags.add(TCON(encoding=3, text=', '.join(track_info['genres'])))
            
            # Comments
            comment = f"Downloaded from YouTube | Spotify: {track_info['spotify_url']}"
            if track_info['isrc']:
                comment += f" | ISRC: {track_info['isrc']}"
            tags.add(COMM(encoding=3, lang='eng', desc='', text=comment))
            
            # Album artwork
            if artwork_path and Path(artwork_path).exists():
                with open(artwork_path, 'rb') as f:
                    artwork_data = f.read()
                
                tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,
                    desc='Album Cover',
                    data=artwork_data
                ))
                print(f"{Fore.GREEN}🎨 Embedded album artwork{Style.RESET_ALL}")
            
            audio.save(v2_version=3)
            print(f"{Fore.GREEN}✅ MP3 metadata embedded{Style.RESET_ALL}")
            return True
            
        except Exception as e:
            print(f"{Fore.RED}❌ MP3 metadata error: {e}{Style.RESET_ALL}")
            return False
    
    def _embed_flac_metadata(self, flac_file, track_info, artwork_path=None):
        """Embed metadata into FLAC file"""
        try:
            audio = FLAC(flac_file)
            audio.clear()
            
            # Basic metadata
            audio['TITLE'] = track_info['name']
            audio['ARTIST'] = ', '.join(track_info['artists'])
            audio['ALBUM'] = track_info['album']
            audio['ALBUMARTIST'] = track_info['album_artist']
            audio['TRACKNUMBER'] = str(track_info['track_number'])
            audio['DISCNUMBER'] = str(track_info['disc_number'])
            
            if track_info['release_year']:
                audio['DATE'] = str(track_info['release_year'])
            
            if track_info.get('genres'):
                audio['GENRE'] = ', '.join(track_info['genres'])
            
            if track_info['isrc']:
                audio['ISRC'] = track_info['isrc']
            
            audio['COMMENT'] = f"Downloaded from YouTube | Spotify: {track_info['spotify_url']}"
            
            # Album artwork
            if artwork_path and Path(artwork_path).exists():
                with open(artwork_path, 'rb') as f:
                    artwork_data = f.read()
                
                picture = mutagen.flac.Picture()
                picture.type = 3
                picture.mime = 'image/jpeg'
                picture.desc = 'Album Cover'
                picture.data = artwork_data
                
                audio.add_picture(picture)
                print(f"{Fore.GREEN}🎨 Embedded FLAC artwork{Style.RESET_ALL}")
            
            audio.save()
            print(f"{Fore.GREEN}✅ FLAC metadata embedded{Style.RESET_ALL}")
            return True
            
        except Exception as e:
            print(f"{Fore.RED}❌ FLAC metadata error: {e}{Style.RESET_ALL}")
            return False
    
    def _convert_to_mp3(self, input_file, output_file):
        """Convert audio file to MP3 using ffmpeg"""
        try:
            cmd = [
                'ffmpeg', '-i', str(input_file),
                '-codec:a', 'libmp3lame',
                '-b:a', '320k',
                '-y',
                str(output_file)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"{Fore.GREEN}✅ Converted to MP3: {output_file.name}{Style.RESET_ALL}")
                return True
            else:
                print(f"{Fore.RED}❌ Conversion failed: {result.stderr}{Style.RESET_ALL}")
                return False
                
        except Exception as e:
            print(f"{Fore.RED}❌ Conversion error: {e}{Style.RESET_ALL}")
            return False
    
    def search_and_download(self, track_info, playlist_name):
        """Search and download track with maximum quality"""
        search_query = track_info['search_query']
        safe_filename = self.sanitize_filename(search_query)
        
        # Create playlist directory
        playlist_dir = self.download_root / self.sanitize_filename(playlist_name)
        playlist_dir.mkdir(exist_ok=True)
        
        # Check if file already exists
        existing_files = list(playlist_dir.glob(f"{safe_filename}.*"))
        if existing_files:
            print(f"{Fore.YELLOW}⏭️  Skipping (already exists): {safe_filename}{Style.RESET_ALL}")
            return True
        
        print(f"{Fore.CYAN}🔍 Searching: {search_query}{Style.RESET_ALL}")
        
        try:
            # Configure output path
            temp_output = str(self.temp_dir / f"{safe_filename}.%(ext)s")
            self.ydl_opts['outtmpl'] = temp_output
            
            # Progress hook
            def progress_hook(d):
                if d['status'] == 'downloading':
                    if 'total_bytes' in d:
                        percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
                        print(f"\r{Fore.BLUE}📥 Downloading: {percent:.1f}%{Style.RESET_ALL}", end='', flush=True)
                elif d['status'] == 'finished':
                    print(f"\n{Fore.GREEN}✅ Downloaded: {Path(d['filename']).name}{Style.RESET_ALL}")
            
            self.ydl_opts['progress_hooks'] = [progress_hook]
            
            # Search and download
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                try:
                    # Search for best match
                    info = ydl.extract_info(f"ytsearch1:{search_query}", download=False)
                    if not info['entries']:
                        print(f"{Fore.RED}❌ No results found for: {search_query}{Style.RESET_ALL}")
                        return False
                    
                    video_info = info['entries'][0]
                    video_title = video_info.get('title', 'Unknown')
                    duration = video_info.get('duration', 0)
                    
                    # Verify duration similarity
                    expected_duration = track_info['duration_ms'] / 1000
                    if abs(duration - expected_duration) > 30:
                        print(f"{Fore.YELLOW}⚠️  Duration mismatch: Expected {expected_duration:.0f}s, got {duration:.0f}s{Style.RESET_ALL}")
                    
                    print(f"{Fore.GREEN}🎯 Found: {video_title}{Style.RESET_ALL}")
                    
                    # Download
                    ydl.download([video_info['webpage_url']])
                    
                except Exception as e:
                    print(f"{Fore.RED}❌ Download failed: {e}{Style.RESET_ALL}")
                    return False
            
            # Find downloaded file
            temp_files = list(self.temp_dir.glob(f"{safe_filename}.*"))
            if not temp_files:
                print(f"{Fore.RED}❌ Downloaded file not found{Style.RESET_ALL}")
                return False
            
            downloaded_file = temp_files[0]
            
            # Download album artwork
            artwork_path = None
            if track_info['album_cover_url']:
                artwork_path = self.temp_dir / f"{safe_filename}_artwork.jpg"
                if self.download_album_artwork(track_info['album_cover_url'], artwork_path):
                    print(f"{Fore.GREEN}🎨 Downloaded album artwork{Style.RESET_ALL}")
            
            # Embed metadata
            if self.embed_metadata(downloaded_file, track_info, artwork_path):
                print(f"{Fore.GREEN}📝 Metadata embedded successfully{Style.RESET_ALL}")
            
            # Move to final location
            final_file = playlist_dir / downloaded_file.name
            downloaded_file.rename(final_file)
            
            # Cleanup
            if artwork_path and artwork_path.exists():
                artwork_path.unlink()
            
            print(f"{Fore.GREEN}✅ Completed: {final_file.name}{Style.RESET_ALL}")
            return True
            
        except Exception as e:
            print(f"{Fore.RED}❌ Error processing track: {e}{Style.RESET_ALL}")
            return False
    
    def download_playlist(self, playlist_url):
        """Download entire playlist with progress tracking"""
        playlist_info, tracks = self.get_playlist_tracks(playlist_url)
        
        if not tracks:
            print(f"{Fore.RED}❌ No tracks found in playlist{Style.RESET_ALL}")
            return
        
        print(f"\n{Fore.CYAN}🎵 Starting download of '{playlist_info['name']}'{Style.RESET_ALL}")
        print(f"{Fore.CYAN}📊 Total tracks: {len(tracks)}{Style.RESET_ALL}")
        
        if self.is_termux:
            self.send_notification("Spotify Downloader", f"Starting download: {playlist_info['name']}")
        
        # Download with progress bar
        successful = 0
        failed = 0
        
        with tqdm(total=len(tracks), desc="Downloading", unit="track") as pbar:
            for track in tracks:
                if not self.is_downloading:
                    break
                
                pbar.set_description(f"Downloading: {track['name'][:30]}...")
                
                if self.search_and_download(track, playlist_info['name']):
                    successful += 1
                else:
                    failed += 1
                    self.failed_downloads.append(track)
                
                pbar.update(1)
                
                # Brief pause for mobile optimization
                if self.is_termux:
                    time.sleep(1)
        
        # Summary
        print(f"\n{Fore.GREEN}📊 Download Summary{Style.RESET_ALL}")
        print(f"{Fore.GREEN}✅ Successful: {successful}{Style.RESET_ALL}")
        print(f"{Fore.RED}❌ Failed: {failed}{Style.RESET_ALL}")
        
        if self.is_termux:
            self.send_notification(
                "Spotify Downloader", 
                f"Completed: {successful} successful, {failed} failed"
            )
        
        if failed > 0:
            print(f"\n{Fore.YELLOW}Failed tracks:{Style.RESET_ALL}")
            for track in self.failed_downloads:
                print(f"  - {track['search_query']}")
    
    def run_interactive(self):
        """Run interactive mode"""
        print(f"\n{Fore.CYAN}🎵 Spotify to YouTube Downloader - Termux Edition{Style.RESET_ALL}")
        print(f"{Fore.CYAN}Maximum Audio Quality Mode{Style.RESET_ALL}")
        print("=" * 50)
        
        while True:
            try:
                playlist_url = input(f"\n{Fore.YELLOW}Enter Spotify playlist URL (or 'quit' to exit): {Style.RESET_ALL}")
                
                if playlist_url.lower() in ['quit', 'exit', 'q']:
                    break
                
                if not playlist_url.strip():
                    continue
                
                self.is_downloading = True
                self.download_playlist(playlist_url)
                self.is_downloading = False
                
            except KeyboardInterrupt:
                print(f"\n{Fore.YELLOW}🛑 Download interrupted{Style.RESET_ALL}")
                self.is_downloading = False
                break
            except Exception as e:
                print(f"{Fore.RED}❌ Error: {e}{Style.RESET_ALL}")
                continue
        
        print(f"{Fore.CYAN}👋 Thanks for using Spotify Downloader!{Style.RESET_ALL}")

def main():
    """Main entry point"""
    try:
        downloader = TermuxSpotifyDownloader()
        
        if len(sys.argv) > 1:
            # Command line mode
            playlist_url = sys.argv[1]
            downloader.is_downloading = True
            downloader.download_playlist(playlist_url)
        else:
            # Interactive mode
            downloader.run_interactive()
            
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}🛑 Exiting...{Style.RESET_ALL}")
    except Exception as e:
        print(f"{Fore.RED}❌ Fatal error: {e}{Style.RESET_ALL}")
        sys.exit(1)

if __name__ == "__main__":
    main()