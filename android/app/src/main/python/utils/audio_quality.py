"""
Audio quality management and metadata handling
"""

import os
import subprocess
from pathlib import Path
import mutagen
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC, TCON, COMM
from mutagen.flac import FLAC
import requests
from PIL import Image

class AudioQualityManager:
    def __init__(self):
        self.supported_formats = ['mp3', 'flac', 'm4a', 'ogg']
        self.preferred_quality = 'best'
    
    def get_optimal_ytdl_config(self, temp_dir, is_mobile=False):
        """Get optimized yt-dlp configuration for maximum audio quality"""
        
        # Format selection - prioritize FLAC, then high-quality MP3
        format_selector = (
            # Best FLAC audio available
            'bestaudio[acodec=flac]/bestaudio[ext=flac]/'
            # Best Opus/WebM audio (often highest quality)
            'bestaudio[acodec=opus]/bestaudio[ext=webm]/'
            # High quality M4A
            'bestaudio[acodec=aac][abr>=256]/bestaudio[ext=m4a][abr>=256]/'
            # High quality MP3
            'bestaudio[acodec=mp3][abr>=320]/bestaudio[ext=mp3][abr>=320]/'
            # Fallback to best available
            'bestaudio/best'
        )
        
        config = {
            'format': format_selector,
            'outtmpl': '',  # Will be set dynamically
            
            # Audio processing
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '0',  # Best quality (VBR)
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
            'writethumbnail': False,
            'writeinfojson': False,
            
            # Performance settings
            'concurrent_fragment_downloads': 2 if is_mobile else 4,
            'retries': 3,
            'fragment_retries': 3,
            'timeout': 30,
            
            # Output control
            'quiet': False,
            'no_warnings': False,
            'extractaudio': True,
            'keepvideo': False,
            'noplaylist': True,
            
            # Mobile optimizations
            'socket_timeout': 30,
            'http_chunk_size': 1048576 if is_mobile else 10485760,  # 1MB vs 10MB chunks
        }
        
        # Add mobile-specific optimizations
        if is_mobile:
            config.update({
                'max_downloads': 1,
                'concurrent_fragment_downloads': 1,
                'http_chunk_size': 524288,  # 512KB chunks
                'buffer_size': 1024,
            })
        
        return config
    
    def embed_metadata(self, audio_file, track_info, artwork_path=None):
        """Embed comprehensive metadata into audio file"""
        try:
            audio_file = Path(audio_file)
            
            # Determine file type and use appropriate library
            if audio_file.suffix.lower() == '.mp3':
                return self._embed_mp3_metadata(audio_file, track_info, artwork_path)
            elif audio_file.suffix.lower() == '.flac':
                return self._embed_flac_metadata(audio_file, track_info, artwork_path)
            else:
                # Try to convert to MP3 if unsupported format
                mp3_file = audio_file.with_suffix('.mp3')
                if self._convert_to_mp3(audio_file, mp3_file):
                    audio_file.unlink()  # Remove original
                    return self._embed_mp3_metadata(mp3_file, track_info, artwork_path)
                else:
                    print(f"⚠️  Unsupported format: {audio_file.suffix}")
                    return False
                    
        except Exception as e:
            print(f"❌ Metadata embedding error: {e}")
            return False
    
    def _embed_mp3_metadata(self, mp3_file, track_info, artwork_path=None):
        """Embed metadata into MP3 file using mutagen"""
        try:
            # Load or create ID3 tags
            try:
                audio = MP3(mp3_file, ID3=ID3)
            except:
                audio = MP3(mp3_file)
            
            if audio.tags is None:
                audio.add_tags()
            
            tags = audio.tags
            
            # Clear existing tags to avoid conflicts
            tags.clear()
            
            # Basic metadata
            tags.add(TIT2(encoding=3, text=track_info['name']))
            tags.add(TPE1(encoding=3, text=', '.join(track_info['artists'])))
            tags.add(TALB(encoding=3, text=track_info['album']))
            tags.add(TPE2(encoding=3, text=track_info['album_artist']))
            
            # Track numbers
            tags.add(TRCK(encoding=3, text=f"{track_info['track_number']}/{track_info.get('total_tracks', '')}"))
            tags.add(TPOS(encoding=3, text=str(track_info['disc_number'])))
            
            # Release date
            if track_info['release_year']:
                tags.add(TDRC(encoding=3, text=str(track_info['release_year'])))
            
            # Genre
            if track_info.get('genres'):
                tags.add(TCON(encoding=3, text=', '.join(track_info['genres'])))
            
            # Comments
            comment_text = f"Downloaded from YouTube | Spotify: {track_info.get('spotify_url', '')}"
            if track_info.get('isrc'):
                comment_text += f" | ISRC: {track_info['isrc']}"
            tags.add(COMM(encoding=3, lang='eng', desc='', text=comment_text))
            
            # Album artwork
            if artwork_path and Path(artwork_path).exists():
                with open(artwork_path, 'rb') as f:
                    artwork_data = f.read()
                
                tags.add(APIC(
                    encoding=3,
                    mime='image/jpeg',
                    type=3,  # Cover (front)
                    desc='Album Cover',
                    data=artwork_data
                ))
                print("🎨 Embedded album artwork")
            
            # Save tags
            audio.save(v2_version=3)
            print("✅ MP3 metadata embedded successfully")
            return True
            
        except Exception as e:
            print(f"❌ MP3 metadata error: {e}")
            return False
    
    def _embed_flac_metadata(self, flac_file, track_info, artwork_path=None):
        """Embed metadata into FLAC file"""
        try:
            audio = FLAC(flac_file)
            
            # Clear existing tags
            audio.clear()
            
            # Basic metadata
            audio['TITLE'] = track_info['name']
            audio['ARTIST'] = ', '.join(track_info['artists'])
            audio['ALBUM'] = track_info['album']
            audio['ALBUMARTIST'] = track_info['album_artist']
            audio['TRACKNUMBER'] = str(track_info['track_number'])
            audio['DISCNUMBER'] = str(track_info['disc_number'])
            
            # Release date
            if track_info['release_year']:
                audio['DATE'] = str(track_info['release_year'])
            
            # Genre
            if track_info.get('genres'):
                audio['GENRE'] = ', '.join(track_info['genres'])
            
            # Additional metadata
            if track_info.get('isrc'):
                audio['ISRC'] = track_info['isrc']
            
            # Comments
            audio['COMMENT'] = f"Downloaded from YouTube | Spotify: {track_info.get('spotify_url', '')}"
            
            # Album artwork
            if artwork_path and Path(artwork_path).exists():
                with open(artwork_path, 'rb') as f:
                    artwork_data = f.read()
                
                picture = mutagen.flac.Picture()
                picture.type = 3  # Cover (front)
                picture.mime = 'image/jpeg'
                picture.desc = 'Album Cover'
                picture.data = artwork_data
                
                audio.add_picture(picture)
                print("🎨 Embedded FLAC artwork")
            
            # Save
            audio.save()
            print("✅ FLAC metadata embedded successfully")
            return True
            
        except Exception as e:
            print(f"❌ FLAC metadata error: {e}")
            return False
    
    def _convert_to_mp3(self, input_file, output_file):
        """Convert audio file to MP3 using ffmpeg"""
        try:
            cmd = [
                'ffmpeg', '-i', str(input_file),
                '-codec:a', 'libmp3lame',
                '-b:a', '320k',
                '-y',  # Overwrite output file
                str(output_file)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"✅ Converted to MP3: {output_file.name}")
                return True
            else:
                print(f"❌ Conversion failed: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"❌ Conversion error: {e}")
            return False
    
    def optimize_audio_quality(self, audio_file):
        """Apply audio quality optimizations"""
        try:
            # Analyze audio properties
            audio = mutagen.File(audio_file)
            if audio is None:
                return False
            
            # Check bitrate and quality
            bitrate = audio.info.bitrate if hasattr(audio.info, 'bitrate') else 0
            duration = audio.info.length if hasattr(audio.info, 'length') else 0
            
            print(f"📊 Audio info: {bitrate} kbps, {duration:.1f}s")
            
            # Apply quality enhancements if needed
            if bitrate < 256:
                print("⚠️  Low bitrate detected, quality may be limited")
            
            return True
            
        except Exception as e:
            print(f"❌ Quality analysis error: {e}")
            return False
    
    def verify_audio_integrity(self, audio_file):
        """Verify audio file integrity"""
        try:
            # Try to load the file
            audio = mutagen.File(audio_file)
            if audio is None:
                return False
            
            # Check if file has audio content
            if hasattr(audio.info, 'length') and audio.info.length > 0:
                return True
            else:
                return False
                
        except Exception:
            return False
