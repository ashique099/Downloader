import os
import re
import ssl
import json
import time
import uuid
import shutil
import threading
import tempfile
import subprocess
import urllib.request
import urllib.error
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

# Path to a server-side YouTube cookies file (Netscape format).
# Export from Chrome/Firefox with a cookies extension and place next to app.py.
# If this file exists it is automatically used for all YouTube requests.
SERVER_COOKIES_FILE = os.path.join(BASE_DIR, 'cookies.txt')

# ── YouTube Invidious fallback ─────────────────────────────────────────────
# When YouTube blocks the server IP, the app automatically retries by querying
# these public Invidious instances to obtain signed stream URLs.
_INVIDIOUS_INSTANCES = [
    'https://invidious.jing.rocks',
    'https://inv.tux.pizza',
    'https://yewtu.be',
    'https://invidious.privacydev.net',
    'https://inv.riverside.rocks',
    'https://iv.datura.network',
    'https://invidious.nerdvpn.de',
    'https://invidious.perennialte.ch',
    'https://invidious.flokinet.to',
]
_SSL_CTX = ssl.create_default_context()
_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'


def _extract_youtube_id(url):
    """Extract YouTube video ID from various URL formats."""
    for pat in [
        r'[?&]v=([A-Za-z0-9_-]{11})',
        r'youtu\.be/([A-Za-z0-9_-]{11})',
        r'/shorts/([A-Za-z0-9_-]{11})',
        r'/embed/([A-Za-z0-9_-]{11})',
        r'/v/([A-Za-z0-9_-]{11})',
    ]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _invidious_fetch(video_id):
    """Query Invidious instances until one returns valid video data.
    Returns (data_dict, instance_base_url) or (None, None)."""
    fields = 'title,lengthSeconds,videoThumbnails,adaptiveFormats,formatStreams'
    for base in _INVIDIOUS_INSTANCES:
        try:
            req = urllib.request.Request(
                f'{base}/api/v1/videos/{video_id}?fields={fields}',
                headers={'User-Agent': _UA},
            )
            with urllib.request.urlopen(req, timeout=12, context=_SSL_CTX) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode('utf-8'))
                    if data.get('adaptiveFormats') or data.get('formatStreams'):
                        return data, base
        except urllib.error.HTTPError as e:
            print(f"[Invidious] Instance {base} returned HTTP {e.code}: {e.reason}")
        except Exception as e:
            print(f"[Invidious] Instance {base} failed: {str(e)}")
            continue
    print("[Invidious] All fallback instances failed or are rate-limiting this server.")
    return None, None


def _stream_download(url, dest_path, task_id, pct_start=0, pct_end=100):
    """Stream-download url to dest_path, mapping byte progress to pct_start..pct_end."""
    req = urllib.request.Request(url, headers={
        'User-Agent': _UA,
        'Referer': 'https://www.youtube.com/',
    })
    pct_range = pct_end - pct_start
    with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
        total = int(resp.headers.get('Content-Length', 0))
        downloaded = 0
        with open(dest_path, 'wb') as f:
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                pct = (pct_start + round((downloaded / total) * pct_range, 1)) if total > 0 else pct_start
                with task_lock:
                    if task_id in download_tasks:
                        download_tasks[task_id]['progress'] = min(pct, pct_end)


def _invidious_download(task_id, url, quality, download_type, task_dir):
    """Fallback YouTube downloader via Invidious API. Returns True on success."""
    if not shutil.which('ffmpeg'):
        print("[Invidious] FATAL: FFmpeg is not installed on this server. Fallback cannot merge streams.")
        with task_lock:
            if task_id in download_tasks:
                download_tasks[task_id]['error'] = "Server Error: FFmpeg is not installed. Contact administrator."
        return False

    video_id = _extract_youtube_id(url)
    if not video_id:
        return False

    with task_lock:
        if task_id in download_tasks:
            download_tasks[task_id]['speed'] = 'Retrying via fallback server...'
            download_tasks[task_id]['eta'] = 'Please wait'

    data, instance = _invidious_fetch(video_id)
    if not data:
        return False

    title = data.get('title', f'video_{video_id}')
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip()[:100] or f'video_{video_id}'

    adaptive = data.get('adaptiveFormats', [])
    legacy   = data.get('formatStreams', [])

    def abs_url(u):
        return (instance + u) if u.startswith('/') else u

    # ── MP3 path ───────────────────────────────────────────────────────────
    if download_type == 'mp3':
        audio_fmts = sorted(
            [f for f in adaptive if 'audio' in f.get('type', '')],
            key=lambda x: x.get('bitrate', 0), reverse=True
        )
        if not audio_fmts:
            return False
        best_a = audio_fmts[0]
        a_ext  = 'webm' if 'webm' in best_a.get('type', '') else 'm4a'
        a_path  = os.path.join(task_dir, f'{safe_title}.{a_ext}')
        mp3_path = os.path.join(task_dir, f'{safe_title}.mp3')

        with task_lock:
            if task_id in download_tasks:
                download_tasks[task_id]['speed'] = 'Downloading audio...'
        _stream_download(abs_url(best_a.get('url', '')), a_path, task_id, 0, 85)

        with task_lock:
            if task_id in download_tasks:
                download_tasks[task_id]['status'] = 'processing'
                download_tasks[task_id]['progress'] = 90.0
                download_tasks[task_id]['speed'] = 'Converting to MP3...'

        res = subprocess.run(
            ['ffmpeg', '-y', '-i', a_path, '-codec:a', 'libmp3lame', '-q:a', '2', mp3_path],
            capture_output=True
        )
        try:
            os.remove(a_path)
        except Exception:
            pass
        if res.returncode != 0 or not os.path.exists(mp3_path):
            return False
        final_path = mp3_path
        final_name = f'{safe_title}.mp3'

    # ── MP4 path ───────────────────────────────────────────────────────────
    else:
        target_h = 99999 if quality == 'best' else int(quality.replace('p', '') or 99999)

        video_fmts = sorted(
            [f for f in adaptive if 'video' in f.get('type', '') and f.get('height')],
            key=lambda x: (x.get('height', 0), x.get('bitrate', 0)), reverse=True
        )
        audio_fmts = sorted(
            [f for f in adaptive if 'audio' in f.get('type', '')],
            key=lambda x: x.get('bitrate', 0), reverse=True
        )

        if video_fmts and audio_fmts:
            # Adaptive streams: separate video + audio → merge with ffmpeg
            eligible = [f for f in video_fmts if f.get('height', 0) <= target_h] or video_fmts
            best_v = eligible[0]
            best_a = audio_fmts[0]

            v_ext  = 'webm' if 'webm' in best_v.get('type', '') else 'mp4'
            a_ext  = 'webm' if 'webm' in best_a.get('type', '') else 'm4a'
            v_path   = os.path.join(task_dir, f'{safe_title}_v.{v_ext}')
            a_path   = os.path.join(task_dir, f'{safe_title}_a.{a_ext}')
            out_path = os.path.join(task_dir, f'{safe_title}.mp4')

            with task_lock:
                if task_id in download_tasks:
                    download_tasks[task_id]['speed'] = 'Downloading video stream...'
            _stream_download(abs_url(best_v.get('url', '')), v_path, task_id, 0, 55)

            with task_lock:
                if task_id in download_tasks:
                    download_tasks[task_id]['speed'] = 'Downloading audio stream...'
            _stream_download(abs_url(best_a.get('url', '')), a_path, task_id, 55, 88)

            with task_lock:
                if task_id in download_tasks:
                    download_tasks[task_id]['status'] = 'processing'
                    download_tasks[task_id]['progress'] = 92.0
                    download_tasks[task_id]['speed'] = 'Merging streams...'

            res = subprocess.run(
                ['ffmpeg', '-y', '-i', v_path, '-i', a_path,
                 '-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental', out_path],
                capture_output=True
            )
            for p in (v_path, a_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            if res.returncode != 0 or not os.path.exists(out_path):
                return False
            final_path = out_path
            final_name = f'{safe_title}.mp4'

        elif legacy:
            # Legacy combined streams (no merge needed, typically up to 720p)
            eligible = sorted(legacy, key=lambda x: x.get('bitrate', 0), reverse=True)
            by_h = [f for f in eligible
                    if int((f.get('resolution') or '0x0').split('x')[-1]) <= target_h]
            best  = (by_h or eligible)[0]
            out_path = os.path.join(task_dir, f'{safe_title}.mp4')
            with task_lock:
                if task_id in download_tasks:
                    download_tasks[task_id]['speed'] = 'Downloading video...'
            _stream_download(abs_url(best.get('url', '')), out_path, task_id, 0, 98)
            if not os.path.exists(out_path):
                return False
            final_path = out_path
            final_name = f'{safe_title}.mp4'
        else:
            return False

    # Secure the final filename
    safe_name = secure_filename(final_name)
    if not os.path.splitext(safe_name)[0]:
        safe_name = f'download_{task_id}{os.path.splitext(final_name)[1]}'
    safe_filepath = os.path.join(task_dir, safe_name)
    if final_path != safe_filepath:
        try:
            os.rename(final_path, safe_filepath)
            final_path = safe_filepath
        except Exception:
            pass

    with task_lock:
        if task_id in download_tasks:
            download_tasks[task_id]['status']   = 'completed'
            download_tasks[task_id]['progress']  = 100.0
            download_tasks[task_id]['filename']  = os.path.basename(final_path)
            download_tasks[task_id]['filepath']  = final_path
    return True


def _invidious_formats(url):
    """Get video info from Invidious as /formats-compatible dict, or None on failure."""
    video_id = _extract_youtube_id(url)
    if not video_id:
        return None
    data, _ = _invidious_fetch(video_id)
    if not data:
        return None

    title        = data.get('title', 'Video')
    duration_s   = data.get('lengthSeconds', 0)
    thumbs       = data.get('videoThumbnails', [])
    thumbnail    = next(
        (t.get('url') for t in thumbs if t.get('quality') in ('maxres', 'high', 'sddefault')),
        f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
    )

    seen_heights = set()
    for f in data.get('adaptiveFormats', []):
        if 'video' in f.get('type', '') and f.get('height'):
            seen_heights.add(int(f['height']))
    for f in data.get('formatStreams', []):
        res = f.get('resolution', '')
        if 'x' in res:
            try:
                seen_heights.add(int(res.split('x')[-1]))
            except Exception:
                pass

    std = {1080: '1080p Full HD', 720: '720p HD', 480: '480p SD',
           360: '360p Medium', 240: '240p Low', 144: '144p Lowest'}
    max_h = max(seen_heights) if seen_heights else 720
    qualities = [
        {'id': f'{h}p', 'label': lbl, 'size': 'Estimated'}
        for h, lbl in sorted(std.items(), reverse=True) if max_h >= h
    ]
    qualities.insert(0, {'id': 'best', 'label': 'Best Quality', 'size': 'Estimated'})

    return {
        'title': title,
        'thumbnail': thumbnail,
        'duration': format_duration(duration_s),
        'platform': 'youtube',
        'qualities': qualities,
    }

# ── End Invidious fallback ─────────────────────────────────────────────────

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
        if not msg.startswith('[download]'):
            print(f"[yt-dlp Debug] {msg}")

    def warning(self, msg):
        print(f"[yt-dlp Warning] {msg}")

    def error(self, msg):
        print(f"[yt-dlp Error] {msg}")
def download_thread(task_id, url, quality, download_type):
    """Task target function executing downloading in background."""
    task_dir = os.path.join(DOWNLOADS_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # Allow per-task cookies (Netscape cookies.txt format) to be written to task dir
    cookie_path = None

    # Determine yt-dlp options based on download request
    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'logger': YTDLLogger(),
        'retries': 5,
        'fragment_retries': 5,
        # Bypasses: Impersonate a real browser and use server-friendly clients
        'impersonate': 'chrome',
        'extractor_args': {
            'youtube': {
                'player_client': ['tv_embedded', 'ios', 'android', 'default'],
                'skip': ['translated_subs']
            }
        }
    }

    # Auto-load server-side cookies.txt if it exists (highest-priority bypass)
    if os.path.exists(SERVER_COOKIES_FILE):
        base_opts['cookiefile'] = SERVER_COOKIES_FILE
    
    if download_type == 'mp3':
        ydl_opts = {
            **base_opts,
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(task_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [progress_hook(task_id)],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    else:
        # mp4 video download — use a fallback chain so any quality always resolves
        if quality == 'best':
            # Best separate streams → best combined → any
            fmt_spec = 'bestvideo+bestaudio/bestvideo/best'
        else:
            height = quality.replace('p', '')
            # Exact height → any height at or below → any combined → any
            fmt_spec = (
                f'bestvideo[height<={height}]+bestaudio'
                f'/bestvideo[height<={height}]'
                f'/bestvideo+bestaudio'
                f'/best'
            )

        ydl_opts = {
            **base_opts,
            'format': fmt_spec,
            'outtmpl': os.path.join(task_dir, '%(title)s.%(ext)s'),
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook(task_id)],
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
        err_str = str(e).lower()
        is_yt   = 'youtube.com' in url.lower() or 'youtu.be' in url.lower()
        is_bot  = 'sign in' in err_str or 'bot' in err_str or 'confirm' in err_str
        # Auto-retry via Invidious when YouTube blocks the server IP
        if is_yt and is_bot:
            try:
                if _invidious_download(task_id, url, quality, download_type, task_dir):
                    return  # Successfully downloaded via fallback
            except Exception:
                pass
        tb_str = traceback.format_exc()
        with task_lock:
            if download_tasks.get(task_id) and download_tasks[task_id]['status'] != 'completed':
                download_tasks[task_id]['status'] = 'failed'
                download_tasks[task_id]['error'] = str(e)
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
        'retries': 3,
        # Bypasses: Impersonate a real browser and use server-friendly clients
        'impersonate': 'chrome',
        'extractor_args': {
            'youtube': {
                'player_client': ['tv_embedded', 'ios', 'android', 'default'],
                'skip': ['translated_subs']
            }
        }
    }

    # Auto-load server-side cookies.txt if present (highest-priority bypass)
    if os.path.exists(SERVER_COOKIES_FILE):
        ydl_opts['cookiefile'] = SERVER_COOKIES_FILE

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
        err_msg  = str(e)
        err_low  = err_msg.lower()
        is_yt    = 'youtube.com' in url.lower() or 'youtu.be' in url.lower()
        is_bot   = 'sign in' in err_low or 'bot' in err_low or 'confirm' in err_low
        # Auto-retry via Invidious when YouTube blocks the server IP
        if is_yt and is_bot:
            inv_data = _invidious_formats(url)
            if inv_data:
                return jsonify(inv_data)
            err_msg = "YouTube is blocking this server and the automatic fallback also failed. Please try again in a few minutes."
            print(f"[Error Handler] YouTube block detected and Invidious fallback failed. Original error: {err_low}")
        elif 'unsupported url' in err_low:
            err_msg = "Unsupported website or invalid URL. Please check the link and try again."
        elif 'unable to download webpage' in err_low or 'connection' in err_low or 'reset' in err_low:
            err_msg = "Unable to access the webpage. Check your internet connection or the URL's validity."
        elif 'requested format is not available' in err_low:
            err_msg = "The requested video quality is not available for this link."
        elif 'video unavailable' in err_low or 'private' in err_low:
            err_msg = "This video is unavailable, private, or age-restricted."
        
        print(f"[API Error] /formats failed: {err_msg} | Root Cause: {err_low}")
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
        
    return send_file(filepath, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True, use_reloader=False)
