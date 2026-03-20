from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import subprocess
import tempfile
import os
import re
import sys

# Auto-install yt-dlp if not found
def _ensure_ytdlp():
    import shutil
    if shutil.which('yt-dlp'):
        return
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'yt-dlp'], check=True)
    except Exception:
        pass

_ensure_ytdlp()

def _ytdlp_cmd():
    import shutil
    if shutil.which('yt-dlp'):
        return 'yt-dlp'
    # fallback: run as python module
    return None

YTDLP = _ytdlp_cmd()

def _run_ytdlp(args):
    import shutil
    if shutil.which('yt-dlp'):
        cmd = ['yt-dlp'] + args
    else:
        cmd = [sys.executable, '-m', 'yt_dlp'] + args
    return cmd

def extract_video_id(url):
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m[1]
    # maybe raw ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url
    return None

def ytdlp_get_info(video_id):
    """Get video info using yt-dlp"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    result = subprocess.run(
        _run_ytdlp([
            '--dump-json', '--no-playlist', '--skip-download',
            '--extractor-args', 'youtube:player_client=android,web',
            url
        ]),
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise Exception(f"yt-dlp info failed: {result.stderr[:200]}")
    return json.loads(result.stdout)

def ytdlp_get_formats(info):
    """Extract available formats from yt-dlp info"""
    formats = info.get('formats', [])
    
    mp4_formats = []
    seen_heights = set()
    for f in reversed(formats):
        height = f.get('height')
        ext = f.get('ext', '')
        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')
        if height and ext in ('mp4', 'webm') and vcodec != 'none' and acodec != 'none':
            if height not in seen_heights:
                seen_heights.add(height)
                mp4_formats.append({
                    'format_id': f.get('format_id'),
                    'quality': f'{height}p',
                    'height': height,
                    'ext': ext,
                    'filesize': f.get('filesize') or f.get('filesize_approx')
                })
    
    mp4_formats.sort(key=lambda x: x['height'], reverse=True)
    return mp4_formats[:5]  # top 5 quality

def ytdlp_download(video_id, format_type, quality):
    """Download using yt-dlp, return file bytes"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, 'output.%(ext)s')
        
        if format_type == 'mp3':
            bitrate = quality if quality in ['320', '192', '128'] else '192'
            cmd = _run_ytdlp([
                '--no-playlist',
                '--extractor-args', 'youtube:player_client=android,web',
                '-x', '--audio-format', 'mp3',
                '--audio-quality', f'{bitrate}K',
                '-o', out_template,
                url
            ])
        else:
            height = quality.replace('p', '') if quality.endswith('p') else '720'
            cmd = _run_ytdlp([
                '--no-playlist',
                '--extractor-args', 'youtube:player_client=android,web',
                '-f', f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best[height<={height}]',
                '--merge-output-format', 'mp4',
                '-o', out_template,
                url
            ])
        
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise Exception(f"yt-dlp download failed: {result.stderr.decode()[:300]}")
        
        # Find output file
        for fname in os.listdir(tmpdir):
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, 'rb') as f:
                return f.read(), fname

def ytdlp_fallback_download(video_id, format_type, quality):
    """Fallback: try with different yt-dlp options"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, 'output.%(ext)s')
        
        if format_type == 'mp3':
            cmd = _run_ytdlp([
                '--no-playlist',
                '-x', '--audio-format', 'mp3',
                '--audio-quality', '192K',
                '--extractor-args', 'youtube:player_client=android',
                '-o', out_template,
                url
            ])
        else:
            cmd = _run_ytdlp([
                '--no-playlist',
                '-f', 'best[ext=mp4]/best',
                '--extractor-args', 'youtube:player_client=android',
                '-o', out_template,
                url
            ])
        
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise Exception(f"Fallback download failed: {result.stderr.decode()[:300]}")
        
        for fname in os.listdir(tmpdir):
            fpath = os.path.join(tmpdir, fname)
            with open(fpath, 'rb') as f:
                return f.read(), fname

def cobalt_get_url(video_id, format_type, quality):
    """Fallback: cobalt.tools API"""
    import urllib.request
    
    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    payload = {
        "url": yt_url,
        "downloadMode": "audio" if format_type == 'mp3' else "auto",
        "audioFormat": "mp3" if format_type == 'mp3' else "best",
        "audioBitrate": quality if format_type == 'mp3' else "128",
        "videoQuality": quality.replace('p','') if format_type == 'mp4' else "720"
    }
    
    req = urllib.request.Request(
        'https://api.cobalt.tools/',
        data=json.dumps(payload).encode(),
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
    )
    
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    
    if data.get('status') in ('redirect', 'stream', 'tunnel'):
        stream_url = data.get('url')
        # Download from cobalt stream URL
        with urllib.request.urlopen(stream_url, timeout=60) as resp:
            content = resp.read()
            fname = f"audio.mp3" if format_type == 'mp3' else f"video.mp4"
            return content, fname
    
    raise Exception(f"Cobalt error: {data.get('error', {}).get('code', 'unknown')}")

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path == '/api' or path == '/api/':
            self._json({'status': 'ok', 'service': 'Hybrid Downloader'})

        elif path == '/api/info':
            self._handle_info(params)

        elif path == '/api/download':
            self._handle_download(params)

        else:
            self._error(404, 'Not found')

    def _handle_info(self, params):
        url_or_id = params.get('url', '').strip()
        if not url_or_id:
            return self._error(400, 'Parameter url diperlukan')

        video_id = extract_video_id(url_or_id)
        if not video_id:
            return self._error(400, 'URL YouTube tidak valid')

        try:
            info = ytdlp_get_info(video_id)
            formats = ytdlp_get_formats(info)

            thumbnails = info.get('thumbnails', [])
            best_thumb = max(thumbnails, key=lambda t: t.get('width', 0), default={})

            resp = {
                'status': True,
                'videoId': video_id,
                'title': info.get('title', ''),
                'channelTitle': info.get('uploader', '') or info.get('channel', ''),
                'lengthSeconds': info.get('duration', 0),
                'viewCount': info.get('view_count', 0),
                'thumbnail': best_thumb.get('url') or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                'mp4Formats': formats
            }
            self._json(resp)

        except Exception as e:
            self._error(500, str(e))

    def _handle_download(self, params):
        url_or_id = params.get('url', '').strip()
        fmt = params.get('format', 'mp3')
        quality = params.get('quality', '192')

        if not url_or_id:
            return self._error(400, 'Parameter url diperlukan')

        video_id = extract_video_id(url_or_id)
        if not video_id:
            return self._error(400, 'URL YouTube tidak valid')

        errors = []
        file_bytes = None
        filename = None

        # Attempt 1: yt-dlp normal
        try:
            file_bytes, filename = ytdlp_download(video_id, fmt, quality)
        except Exception as e:
            errors.append(f'yt-dlp: {e}')

        # Attempt 2: yt-dlp android client fallback
        if file_bytes is None:
            try:
                file_bytes, filename = ytdlp_fallback_download(video_id, fmt, quality)
            except Exception as e:
                errors.append(f'yt-dlp fallback: {e}')

        # Attempt 3: cobalt.tools
        if file_bytes is None:
            try:
                file_bytes, filename = cobalt_get_url(video_id, fmt, quality)
            except Exception as e:
                errors.append(f'cobalt: {e}')

        if file_bytes is None:
            return self._error(500, 'Semua metode download gagal: ' + ' | '.join(errors))

        # Sanitize filename
        safe_name = re.sub(r'[^\w\s\-.]', '', filename).strip()
        if not safe_name:
            safe_name = f"hybrid_{video_id}.{fmt}"

        content_type = 'audio/mpeg' if fmt == 'mp3' else 'video/mp4'

        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"')
        self.send_header('Content-Length', str(len(file_bytes)))
        self.end_headers()
        self.wfile.write(file_bytes)

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        body = json.dumps({'status': False, 'error': msg}).encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def log_message(self, format, *args):
        pass
