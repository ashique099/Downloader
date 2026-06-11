import os
import time
import uuid
import shutil
import threading
import tempfile
from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
from werkzeug.utils import secure_filename

# Initialize Flask App
app = Flask(__name__)
CORS(app)  # Enable Cross-Origin Resource Sharing (CORS)

# Define directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, 'downloads')
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Global task storage and thread safety lock
download_tasks = {}
task_lock = threading.Lock()

def format_duration(seconds):
    """Converts duration in seconds to HH:MM:SS or MM:SS format."""
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"

def detect_platform(url, extractor_key):
    """Detects platform based on URL patterns or yt-dlp extractor keys."""
    url_lower = url.lower()
    ek_lower = extractor_key.lower() if extractor_key else ""
    
    if 'youtube' in ek_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'instagram' in ek_lower or 'instagram.com' in url_lower:
        return 'instagram'
    elif 'facebook' in ek_lower or 'fb.watch' in url_lower or 'facebook.com' in url_lower or 'fb.gg' in url_lower:
        return 'facebook'
    elif 'tiktok' in ek_lower or 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'twitter' in ek_lower or 'x.com' in url_lower or 'twitter.com' in url_lower:
        return 'twitter'
    elif 'vimeo' in ek_lower or 'vimeo.com' in url_lower:
        return 'vimeo'
    elif 'dailymotion' in ek_lower or 'dailymotion.com' in url_lower:
        return 'dailymotion'
    else:
        return 'website'

def format_size(bytes_size):
    """Formating size in bytes to human readable format."""
    if not bytes_size or bytes_size <= 0:
        return "Unknown size"
    bytes_size = float(bytes_size)
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} TB"

def estimate_size(formats_list, target_height, duration_secs):
    """Estimates file size based on available formats and duration."""
    video_size = 0
    audio_size = 0
    
    # Try to find the best audio size
    audio_formats = [f for f in formats_list if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    if audio_formats:
        audio_formats.sort(key=lambda x: x.get('abr') or x.get('tbr') or 0, reverse=True)
        best_audio = audio_formats[0]
        audio_size = best_audio.get('filesize') or best_audio.get('filesize_approx') or 0
        if not audio_size and duration_secs and best_audio.get('tbr'):
            audio_size = (best_audio['tbr'] * duration_secs * 1000) / 8

    if target_height == 'best':
        # Find best combined stream or best individual video
        video_formats = [f for f in formats_list if f.get('vcodec') != 'none']
        if video_formats:
            video_formats.sort(key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)
            best_video = video_formats[0]
            video_size = best_video.get('filesize') or best_video.get('filesize_approx') or 0
            if not video_size and duration_secs and best_video.get('tbr'):
                video_size = (best_video['tbr'] * duration_secs * 1000) / 8
    else:
        # Find format matching standard height
        matching_video = [f for f in formats_list if f.get('vcodec') != 'none' and f.get('height') == target_height]
        if matching_video:
            matching_video.sort(key=lambda x: x.get('tbr') or 0, reverse=True)
            best_video = matching_video[0]
            video_size = best_video.get('filesize') or best_video.get('filesize_approx') or 0
            if not video_size and duration_secs and best_video.get('tbr'):
                video_size = (best_video['tbr'] * duration_secs * 1000) / 8
        else:
            # Fallback estimation values based on typical bitrates for resolutions (bits per sec)
            rates = {
                1080: 3500000 / 8,
                720: 1800000 / 8,
                480: 800000 / 8,
                360: 450000 / 8,
                240: 250000 / 8,
                144: 100000 / 8
            }
            rate = rates.get(target_height, 600000 / 8)
            video_size = rate * (duration_secs or 0)
            
    total_size = video_size + audio_size
    return format_size(total_size)

def format_speed(speed_bytes):
    """Converts transfer speed in bytes per second to human-readable format."""
    if not speed_bytes:
        return "Calculating..."
    speed = float(speed_bytes)
    for unit in ['B/s', 'KB/s', 'MB/s', 'GB/s']:
        if speed < 1024.0:
            return f"{speed:.1f} {unit}"
        speed /= 1024.0
    return f"{speed:.1f} TB/s"

def progress_hook(task_id):
    """Returns a progress hook function bound to a specific task_id."""
    def hook(d):
        status = d.get('status')
        with task_lock:
            if task_id not in download_tasks:
                return
            
            task = download_tasks[task_id]
            if status == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                
                if total > 0:
                    percent = round((downloaded / total) * 100, 1)
                else:
                    percent = 0.0
                
                speed = d.get('speed')
                speed_str = format_speed(speed) if speed else d.get('_speed_str', 'Downloading...')
                
                eta = d.get('eta')
                eta_str = f"{eta}s" if eta is not None else d.get('_eta_str', 'Unknown')
                
                task['status'] = 'downloading'
                task['progress'] = percent
                task['speed'] = speed_str
                task['eta'] = eta_str
            elif status == 'finished':
                # Hook is called once downloading of a file is finished.
                # Since yt-dlp might merge or postprocess, status is set to 'processing'.
                task['status'] = 'processing'
                task['progress'] = 95.0
                task['speed'] = 'Processing / Merging files...'
                task['eta'] = 'A few seconds...'
    return hook

class YTDLLogger:
    def debug(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        pass

def download_thread(task_id, url, quality, download_type):
    """Task target function executing downloading in background."""
    task_dir = os.path.join(DOWNLOADS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # Allow per-task cookies (Netscape cookies.txt format) to be written to task dir
    cookie_path = None

    # Determine yt-dlp options based on download request
    if download_type == 'mp3':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(task_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook(task_id)],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'logger': YTDLLogger(),
        }
    else:
        # mp4 video download
        if quality == 'best':
            fmt_spec = 'bestvideo+bestaudio/best'
        else:
            height = quality.replace('p', '')
            fmt_spec = f'bestvideo[height<={height}]+bestaudio/best'
        
        ydl_opts = {
            'format': fmt_spec,
            'outtmpl': os.path.join(task_dir, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook(task_id)],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'logger': YTDLLogger(),
        }

    try:
        # If cookies were provided for this task, write them to a cookiefile and pass to yt-dlp
        if download_tasks.get(task_id) and download_tasks[task_id].get('cookies'):
            try:
                cookie_path = os.path.join(task_dir, 'cookies.txt')
                with open(cookie_path, 'w', encoding='utf-8') as cf:
                    cf.write(download_tasks[task_id]['cookies'])

                # attach cookiefile to ydl options
                ydl_opts['cookiefile'] = cookie_path
            except Exception:
                cookie_path = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Mark starting state
            with task_lock:
                download_tasks[task_id]['status'] = 'downloading'
            
            # Perform download
            ydl.download([url])
            
            # Find the output files
            files = os.listdir(task_dir)
            if not files:
                raise Exception("Downloaded file not found on disk")
            
            # Pick the final output file (ignore temporary part files)
            downloaded_file = None
            for f in files:
                if not f.endswith('.part') and not f.endswith('.ytdl'):
                    downloaded_file = f
                    break
            
            if not downloaded_file:
                downloaded_file = files[0]
            
            filepath = os.path.join(task_dir, downloaded_file)
            
            # Secure filename to prevent directory traversal
            safe_name = secure_filename(downloaded_file)
            # If secure_filename stripped out non-ascii characters completely, generate template name
            if not os.path.splitext(safe_name)[0]:
                ext = os.path.splitext(downloaded_file)[1]
                safe_name = f"download_{task_id}{ext}"
                
            safe_filepath = os.path.join(task_dir, safe_name)
            if filepath != safe_filepath:
                os.rename(filepath, safe_filepath)
                
            # Update state to completed
            with task_lock:
                download_tasks[task_id]['status'] = 'completed'
                download_tasks[task_id]['progress'] = 100.0
                download_tasks[task_id]['filename'] = safe_name
                download_tasks[task_id]['filepath'] = safe_filepath
                
    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        with task_lock:
            download_tasks[task_id]['status'] = 'failed'
            download_tasks[task_id]['error'] = tb_str
    finally:
        # remove cookie file from task dir if created
        try:
            if cookie_path and os.path.exists(cookie_path):
                os.remove(cookie_path)
        except Exception:
            pass

def cleanup_old_files():
    """Background task to delete old download directories after 15 minutes of inactivity."""
    while True:
        try:
            now = time.time()
            if os.path.exists(DOWNLOADS_DIR):
                for folder in os.listdir(DOWNLOADS_DIR):
                    folder_path = os.path.join(DOWNLOADS_DIR, folder)
                    if os.path.isdir(folder_path):
                        # If directory modified time is older than 15 mins (900s), remove it
                        if now - os.path.getmtime(folder_path) > 900:
                            shutil.rmtree(folder_path)
                            with task_lock:
                                if folder in download_tasks:
                                    del download_tasks[folder]
        except Exception as e:
            print(f"Cleanup thread warning: {e}")
        time.sleep(60)

# Start cleanup thread
threading.Thread(target=cleanup_old_files, daemon=True).start()

# --- ROUTES ---

@app.route('/')
def index():
    """Serves the main application landing page."""
    return render_template('index.html')

@app.route('/formats', methods=['POST'])
def get_formats():
    """Fetches video metadata and returns available qualities + size estimations."""
    data = request.get_json()
    # support both application/json and multipart/form-data or form-encoded
    if not data:
        data = request.form.to_dict()

    if not data or 'url' not in data:
        return jsonify({'error': 'URL is required'}), 400

    url = data['url'].strip()
    if not url:
        return jsonify({'error': 'URL cannot be empty'}), 400
        
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'logger': YTDLLogger(),
    }

    # Optional cookies support (pass Netscape cookie file contents via JSON 'cookies'
    # or upload a cookies file with key 'cookies_file' in multipart/form-data)
    cookies_text = data.get('cookies') if isinstance(data, dict) else None
    # If a file was uploaded, prefer that
    if 'cookies_file' in request.files:
        try:
            f = request.files['cookies_file']
            cookies_text = f.read().decode('utf-8')
        except Exception:
            pass
    temp_cookiefile = None
    if cookies_text:
        try:
            tf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt')
            tf.write(cookies_text)
            tf.close()
            temp_cookiefile = tf.name
            ydl_opts['cookiefile'] = temp_cookiefile
        except Exception:
            temp_cookiefile = None
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Fetch metadata
            info = ydl.extract_info(url, download=False)
            
            title = info.get('title', 'Video Title')
            duration_secs = info.get('duration', 0)
            duration_str = format_duration(duration_secs)
            
            # Select best thumbnail
            thumbnail = info.get('thumbnail')
            if not thumbnail and info.get('thumbnails'):
                thumbnail = info.get('thumbnails')[-1].get('url')
            if not thumbnail:
                thumbnail = '/static/logo.png'
                
            extractor = info.get('extractor_key', 'generic')
            platform = detect_platform(url, extractor)
            
            formats_list = info.get('formats', [])
            
            # Extract heights to detect available video qualities
            available_qualities = []
            seen_heights = set()
            for fmt in formats_list:
                height = fmt.get('height')
                # Include standard heights which have video codecs associated
                if height and fmt.get('vcodec') != 'none':
                    if height not in seen_heights:
                        seen_heights.add(height)
            
            std_heights = {
                1080: '1080p Full HD',
                720: '720p HD',
                480: '480p SD',
                360: '360p Medium',
                240: '240p Low',
                144: '144p Lowest'
            }
            
            # Sort quality list
            max_height = max(seen_heights) if seen_heights else 0
            for h in sorted(std_heights.keys(), reverse=True):
                # If maximum resolution of video is greater than or equal to standard height, list it
                if max_height >= h:
                    size_est = estimate_size(formats_list, h, duration_secs)
                    available_qualities.append({
                        'id': f'{h}p',
                        'label': std_heights[h],
                        'size': size_est
                    })
            
            # Always add "Best Quality" option
            best_size_est = estimate_size(formats_list, 'best', duration_secs)
            available_qualities.insert(0, {
                'id': 'best',
                'label': 'Best Quality',
                'size': best_size_est
            })
            
            return jsonify({
                'title': title,
                'thumbnail': thumbnail,
                'duration': duration_str,
                'platform': platform,
                'qualities': available_qualities
            })
            
    except Exception as e:
        # Provide human friendly error message
        err_msg = str(e)
        if 'unsupported url' in err_msg.lower():
            err_msg = "Unsupported website or invalid URL. Please check the link and try again."
        elif 'unable to download webpage' in err_msg.lower() or 'connection' in err_msg.lower():
            err_msg = "Unable to access the webpage. Check your internet connection or the URL's validity."
        return jsonify({'error': err_msg}), 500
    finally:
        # cleanup temp cookiefile
        try:
            if temp_cookiefile and os.path.exists(temp_cookiefile):
                os.remove(temp_cookiefile)
        except Exception:
            pass

@app.route('/download', methods=['POST'])
def start_download():
    """Initializes background download thread and registers tasks details."""
    data = request.get_json()
    # support both application/json and multipart/form-data or form-encoded
    if not data:
        data = request.form.to_dict()

    if not data or 'url' not in data:
        return jsonify({'error': 'URL is required'}), 400
        
    url = data['url'].strip()
    quality = data.get('quality', 'best')
    download_type = data.get('type', 'mp4') # 'mp4' or 'mp3'
    cookies_text = data.get('cookies') if isinstance(data, dict) else None
    # Accept uploaded cookies file in multipart/form-data under 'cookies_file'
    if 'cookies_file' in request.files:
        try:
            f = request.files['cookies_file']
            cookies_text = f.read().decode('utf-8')
        except Exception:
            cookies_text = cookies_text
    
    # Generate unique Task ID
    task_id = str(uuid.uuid4())
    
    with task_lock:
        download_tasks[task_id] = {
            'status': 'starting',
            'progress': 0.0,
            'speed': '0 KB/s',
            'eta': 'Unknown',
            'filename': '',
            'filepath': '',
            'error': '',
            'cookies': cookies_text,
            'cookies_used': bool(cookies_text)
        }
        
    # Spawn background thread for downloading
    thread = threading.Thread(
        target=download_thread,
        args=(task_id, url, quality, download_type),
        daemon=True
    )
    thread.start()
    
    return jsonify({'task_id': task_id})

@app.route('/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    """Retrieves progress data of specified download task."""
    with task_lock:
        task = download_tasks.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        return jsonify(task)

@app.route('/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serves the downloaded file and triggers a short-delay thread to wipe it."""
    with task_lock:
        task = download_tasks.get(task_id)
        if not task:
            return jsonify({'error': 'Task not found or has expired'}), 404
            
        if task['status'] != 'completed':
            return jsonify({'error': 'Task is not completed yet'}), 400
            
        filepath = task['filepath']
        filename = task['filename']
        
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on server disk'}), 404
        
    # Spin a quick thread to wipe file and clean task storage in 180 seconds
    def delayed_cleanup(path_to_clean, tid):
        time.sleep(180)
        try:
            task_dir = os.path.dirname(path_to_clean)
            if os.path.exists(task_dir) and os.path.basename(task_dir) == tid:
                shutil.rmtree(task_dir)
                with task_lock:
                    if tid in download_tasks:
                        del download_tasks[tid]
        except Exception as e:
            print(f"Delayed cleanup warning: {e}")
            
    threading.Thread(target=delayed_cleanup, args=(filepath, task_id), daemon=True).start()
    
    return send_file(filepath, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
