"""
Termux-specific helper functions for Android environment
"""

import os
import subprocess
import json
from pathlib import Path

class TermuxHelper:
    def __init__(self):
        self.is_termux = self.is_termux_environment()
        self.termux_api_available = self.check_termux_api()
    
    def is_termux_environment(self):
        """Check if running in Termux environment"""
        return (
            os.environ.get('PREFIX', '').startswith('/data/data/com.termux') or
            'com.termux' in os.environ.get('PREFIX', '') or
            Path('/data/data/com.termux').exists()
        )
    
    def check_termux_api(self):
        """Check if Termux:API is available"""
        try:
            result = subprocess.run(['termux-notification', '--help'], 
                                  capture_output=True, text=True)
            return result.returncode == 0
        except FileNotFoundError:
            return False
    
    def request_storage_permission(self):
        """Request storage permission for Termux"""
        if not self.is_termux:
            return True
        
        try:
            # Check if storage is already accessible
            storage_path = Path('/storage/emulated/0')
            if storage_path.exists() and os.access(storage_path, os.W_OK):
                print("✅ Storage permission already granted")
                return True
            
            print("📱 Requesting storage permission...")
            print("Please run: termux-setup-storage")
            print("This will allow access to Android storage")
            
            # Try to setup storage automatically
            result = subprocess.run(['termux-setup-storage'], 
                                  capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                print("✅ Storage permission granted")
                return True
            else:
                print("⚠️  Please manually run 'termux-setup-storage'")
                return False
                
        except Exception as e:
            print(f"⚠️  Storage permission setup failed: {e}")
            return False
    
    def setup_notifications(self):
        """Setup notification support"""
        if not self.is_termux or not self.termux_api_available:
            return False
        
        try:
            # Test notification
            self.send_notification("Spotify Downloader", "Notifications enabled")
            print("🔔 Notifications enabled")
            return True
        except:
            print("⚠️  Notifications not available - install Termux:API")
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
                '--priority', priority,
                '--ongoing'
            ]
            
            subprocess.run(cmd, capture_output=True, timeout=5)
            return True
            
        except Exception as e:
            print(f"⚠️  Notification failed: {e}")
            return False
    
    def vibrate(self, duration=500):
        """Vibrate device"""
        if not self.is_termux or not self.termux_api_available:
            return False
        
        try:
            subprocess.run(['termux-vibrate', '-d', str(duration)], 
                          capture_output=True, timeout=5)
            return True
        except:
            return False
    
    def get_battery_status(self):
        """Get battery status information"""
        if not self.is_termux or not self.termux_api_available:
            return None
        
        try:
            result = subprocess.run(['termux-battery-status'], 
                                  capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                return None
                
        except Exception:
            return None
    
    def get_wifi_info(self):
        """Get WiFi connection information"""
        if not self.is_termux or not self.termux_api_available:
            return None
        
        try:
            result = subprocess.run(['termux-wifi-connectioninfo'], 
                                  capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                return None
                
        except Exception:
            return None
    
    def get_device_info(self):
        """Get device information"""
        info = {
            'is_termux': self.is_termux,
            'termux_api': self.termux_api_available,
            'prefix': os.environ.get('PREFIX', ''),
            'home': str(Path.home()),
            'storage_accessible': Path('/storage/emulated/0').exists()
        }
        
        # Add Android-specific info if available
        if self.is_termux:
            try:
                # Get Android version
                result = subprocess.run(['getprop', 'ro.build.version.release'], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    info['android_version'] = result.stdout.strip()
                
                # Get device model
                result = subprocess.run(['getprop', 'ro.product.model'], 
                                      capture_output=True, text=True)
                if result.returncode == 0:
                    info['device_model'] = result.stdout.strip()
                    
            except:
                pass
        
        return info
    
    def optimize_for_termux(self):
        """Apply Termux-specific optimizations"""
        if not self.is_termux:
            return
        
        # Set environment variables for better performance
        os.environ['TMPDIR'] = str(Path.home() / 'tmp')
        
        # Create temp directory if it doesn't exist
        temp_dir = Path.home() / 'tmp'
        temp_dir.mkdir(exist_ok=True)
        
        # Set reasonable limits for mobile
        try:
            import resource
            # Limit memory usage
            resource.setrlimit(resource.RLIMIT_AS, (1024*1024*1024, -1))  # 1GB
        except:
            pass
        
        print("📱 Applied Termux optimizations")
