"""
Mobile device optimizations for Termux environment
"""

import os
import time
import psutil
import subprocess
from pathlib import Path
from PIL import Image

class MobileOptimizer:
    def __init__(self):
        self.low_memory_threshold = 100 * 1024 * 1024  # 100MB
        self.low_battery_threshold = 15  # 15%
        self.max_image_size = (800, 800)  # Max artwork size
        
    def check_system_resources(self):
        """Check available system resources"""
        try:
            # Memory check
            memory = psutil.virtual_memory()
            available_memory = memory.available
            
            # CPU check
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Storage check
            disk = psutil.disk_usage('/')
            available_storage = disk.free
            
            return {
                'memory_available': available_memory,
                'memory_percent': memory.percent,
                'cpu_percent': cpu_percent,
                'storage_available': available_storage,
                'storage_percent': (disk.used / disk.total) * 100
            }
            
        except Exception as e:
            print(f"⚠️  Could not check system resources: {e}")
            return None
    
    def check_battery_optimization(self):
        """Check battery status and optimization"""
        try:
            # Check if termux-battery-status is available
            result = subprocess.run(['termux-battery-status'], 
                                  capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                import json
                battery_info = json.loads(result.stdout)
                
                level = battery_info.get('percentage', 100)
                status = battery_info.get('status', 'Unknown')
                
                print(f"🔋 Battery: {level}% ({status})")
                
                if level < self.low_battery_threshold:
                    print(f"⚠️  Low battery warning: {level}%")
                    return False
                
                return True
            else:
                return True  # Assume OK if can't check
                
        except Exception:
            return True  # Assume OK if can't check
    
    def should_continue_download(self):
        """Determine if download should continue based on system state"""
        # Check battery
        if not self.check_battery_optimization():
            return False
        
        # Check memory
        resources = self.check_system_resources()
        if resources:
            if resources['memory_available'] < self.low_memory_threshold:
                print(f"⚠️  Low memory warning: {resources['memory_available'] / 1024 / 1024:.0f}MB available")
                return False
            
            if resources['cpu_percent'] > 90:
                print(f"⚠️  High CPU usage: {resources['cpu_percent']:.1f}%")
                time.sleep(5)  # Brief pause
        
        # Check network (if available)
        if not self.check_network_connection():
            print("⚠️  Network connection issues")
            return False
        
        return True
    
    def check_network_connection(self):
        """Check network connectivity"""
        try:
            # Try to ping Google's DNS
            result = subprocess.run(['ping', '-c', '1', '-W', '3', '8.8.8.8'], 
                                  capture_output=True, timeout=5)
            return result.returncode == 0
        except:
            return True  # Assume OK if can't check
    
    def optimize_image(self, image_path, max_size=None):
        """Optimize image for mobile storage"""
        try:
            if max_size is None:
                max_size = self.max_image_size
            
            with Image.open(image_path) as img:
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                # Resize if too large
                if img.size[0] > max_size[0] or img.size[1] > max_size[1]:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Save with mobile-optimized quality
                img.save(image_path, 'JPEG', quality=85, optimize=True)
            
            return True
            
        except Exception as e:
            print(f"⚠️  Image optimization failed: {e}")
            return False
    
    def get_mobile_ytdl_opts(self):
        """Get mobile-optimized yt-dlp options"""
        return {
            # Limit concurrent downloads
            'max_downloads': 1,
            'concurrent_fragment_downloads': 1,
            
            # Smaller buffer sizes
            'http_chunk_size': 512 * 1024,  # 512KB
            'buffer_size': 1024,
            
            # More conservative timeouts
            'socket_timeout': 20,
            'timeout': 30,
            
            # Limit retries to save battery
            'retries': 2,
            'fragment_retries': 2,
            
            # Memory optimizations
            'keepvideo': False,
            'writeinfojson': False,
            'writethumbnail': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
        }
    
    def cleanup_temp_files(self, temp_dir, max_age_hours=24):
        """Clean up old temporary files"""
        try:
            temp_path = Path(temp_dir)
            if not temp_path.exists():
                return
            
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            
            cleaned_count = 0
            freed_space = 0
            
            for file_path in temp_path.rglob('*'):
                if file_path.is_file():
                    file_age = current_time - file_path.stat().st_mtime
                    
                    if file_age > max_age_seconds:
                        try:
                            file_size = file_path.stat().st_size
                            file_path.unlink()
                            cleaned_count += 1
                            freed_space += file_size
                        except:
                            pass
            
            if cleaned_count > 0:
                print(f"🧹 Cleaned {cleaned_count} temp files, freed {freed_space / 1024 / 1024:.1f}MB")
            
        except Exception as e:
            print(f"⚠️  Temp cleanup failed: {e}")
    
    def monitor_download_progress(self, callback=None):
        """Monitor download progress and system resources"""
        def progress_monitor():
            while True:
                resources = self.check_system_resources()
                if resources:
                    if resources['memory_percent'] > 85:
                        print("⚠️  High memory usage detected")
                        if callback:
                            callback('high_memory')
                    
                    if resources['storage_percent'] > 90:
                        print("⚠️  Low storage space")
                        if callback:
                            callback('low_storage')
                
                time.sleep(30)  # Check every 30 seconds
        
        return progress_monitor
    
    def enable_low_memory_mode(self):
        """Enable optimizations for low-memory devices"""
        print("📱 Enabling low-memory mode...")
        
        # Reduce Python memory usage
        import gc
        gc.set_threshold(100, 5, 5)  # More aggressive garbage collection
        
        # Set environment variables
        os.environ['PYTHONHASHSEED'] = '0'
        os.environ['MALLOC_TRIM_THRESHOLD_'] = '100000'
        
        return {
            'max_concurrent_downloads': 1,
            'http_chunk_size': 256 * 1024,  # 256KB
            'buffer_size': 512,
            'retries': 1,
            'fragment_retries': 1,
        }
    
    def create_mobile_config(self):
        """Create mobile-optimized configuration"""
        config = {
            'performance': {
                'max_concurrent': 1,
                'chunk_size': 512 * 1024,
                'timeout': 30,
                'retries': 2
            },
            'quality': {
                'prefer_format': 'mp3',
                'max_bitrate': 320,
                'image_size': self.max_image_size
            },
            'limits': {
                'memory_threshold': self.low_memory_threshold,
                'battery_threshold': self.low_battery_threshold,
                'max_file_size': 50 * 1024 * 1024  # 50MB
            }
        }
        
        return config
