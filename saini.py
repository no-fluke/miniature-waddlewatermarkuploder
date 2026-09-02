import os
import re
import time
import mmap
import random
import tempfile
import datetime
import aiohttp
import aiofiles
import asyncio
import logging
import requests
import tgcrypto
import subprocess
import concurrent.futures
from math import ceil
from utils import progress_bar
from pyrogram import Client, filters
from pyrogram.types import Message
from io import BytesIO
from pathlib import Path  
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from base64 import b64decode

# ─── Random Text Watermark ────────────────────────────────────────────────────

# Margin kept away from edges so text is never clipped (fraction of frame size)
_WM_EDGE_MARGIN = 0.08   # 8% from each edge

def add_random_text_overlay(input_file: str, output_file: str, text: str) -> str:
    """
    Burns a transparent text watermark (white + black outline, NO background box)
    into a video at random positions and random time intervals.

    • Each burst: text appears for 3 s at a NEW random (x, y) on screen.
    • Gaps between bursts: random 10–150 s.
    • Quality: uses -crf 18 (visually lossless). Audio is always copied.
    • Returns output_file on success, or input_file on any failure (safe fallback).
    """
    # ── 1. Probe duration + resolution ──────────────────────────────────────
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_file,
            ],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l.strip() for l in probe.stdout.strip().splitlines() if l.strip()]
        # ffprobe outputs: width, height, duration  (order matches show_entries)
        vid_w    = int(lines[0])   if len(lines) > 0 else 1280
        vid_h    = int(lines[1])   if len(lines) > 1 else 720
        duration = float(lines[2]) if len(lines) > 2 else 0.0
    except Exception as e:
        print(f"[watermark] ffprobe failed: {e} — skipping overlay")
        return input_file

    if duration < 15:
        print(f"[watermark] Video too short ({duration:.1f}s) — skipping overlay")
        return input_file

    # ── 2. Font size relative to frame width (scales nicely across resolutions)
    fontsize = max(22, int(vid_w * 0.028))   # ~2.8% of width, min 22 px

    # Rough character width estimate: ~60% of fontsize per char
    text_w_est = len(text) * fontsize * 0.60
    text_h_est = fontsize * 1.2

    # Safe drawable area (in pixels, accounting for edge margin)
    margin_x = int(vid_w * _WM_EDGE_MARGIN)
    margin_y = int(vid_h * _WM_EDGE_MARGIN)
    safe_x_min = margin_x
    safe_x_max = max(margin_x, int(vid_w - margin_x - text_w_est))
    safe_y_min = margin_y
    safe_y_max = max(margin_y, int(vid_h - margin_y - text_h_est))

    # ── 3. Build random appearance windows with a unique (x, y) per burst ───
    SHOW_DURATION = 3
    MIN_GAP       = 10
    MAX_GAP       = 150

    appearances = []   # list of (start, end, x, y)
    t = random.uniform(MIN_GAP, MAX_GAP)
    while t + SHOW_DURATION < duration:
        rx = random.randint(safe_x_min, safe_x_max)
        ry = random.randint(safe_y_min, safe_y_max)
        appearances.append((t, t + SHOW_DURATION, rx, ry))
        t += SHOW_DURATION + random.uniform(MIN_GAP, MAX_GAP)

    if not appearances:
        print(f"[watermark] No burst windows fit in {duration:.1f}s — skipping")
        return input_file

    print(f"[watermark] {len(appearances)} burst(s) across {duration:.1f}s "
          f"at {vid_w}x{vid_h}, fontsize={fontsize}")

    # ── 4. Escape text for FFmpeg drawtext ───────────────────────────────────
    safe_text = (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
    )

    # ── 5. Build drawtext layers on a transparent clone ─────────────────────
    #
    # How the background watermark works:
    #   • We duplicate the video into two streams: [vid] and [ghost].
    #   • On [ghost] we draw the text at very low opacity (white@0.15).
    #   • We then blend [ghost] UNDER [vid] using 'addition' mode so only
    #     the faint text signal leaks through — the video pixels are dominant.
    #   • Result: text appears to glow softly from behind the video content,
    #     like a TV channel watermark, without covering anything.
    #
    # Opacity knob: fontcolor=white@0.15  (raise to @0.25 for more visible)

    drawtext_layers = []
    for (s, e, rx, ry) in appearances:
        enable = f"between(t,{s:.2f},{e:.2f})"
        layer = (
            f"drawtext="
            f"text='{safe_text}'"
            f":fontsize={fontsize}"
            f":fontcolor=white@0.15"    # ghost opacity — tweak to taste
            f":x={rx}"
            f":y={ry}"
            f":enable='{enable}'"
        )
        drawtext_layers.append(layer)

    dt_chain = ",".join(drawtext_layers)

    # filter_complex:
    #   1. split input into [vid] (original) and [base] (copy for ghost text)
    #   2. draw all burst text layers onto [base] → [ghost]
    #   3. blend: [vid] + [ghost] with addition mode
    #      addition blend = each pixel: out = min(vid + ghost, 1.0)
    #      since ghost pixels are nearly black except faint white text,
    #      only the text area adds a tiny brightness → looks like background glow
    filter_complex = (
        f"[0:v]split=2[vid][base];"
        f"[base]{dt_chain}[ghost];"
        f"[vid][ghost]blend=all_mode=addition:all_opacity=1[out]"
    )

    # ── 6. Run FFmpeg ─────────────────────────────────────────────────────────
    #
    # With many bursts (e.g. 55) the filter_complex string can exceed the OS
    # ARG_MAX limit and FFmpeg reports "No such file or directory" on the
    # -filter_complex argument itself.  Writing the filter to a temp file and
    # using -filter_complex_script removes that limit entirely.
    filter_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(filter_complex)
            filter_file = tf.name

        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_file,
                "-filter_complex_script", filter_file,   # ← no ARG_MAX limit
                "-map", "[out]",
                "-map", "0:a?",       # pass audio through if present
                "-c:v", "libx264",
                "-crf", "18",         # visually lossless
                "-preset", "fast",
                "-c:a", "copy",       # audio never re-encoded
                output_file,
            ],
            capture_output=True, text=True, timeout=3600,
        )
        if result.returncode != 0:
            print(f"[watermark] FFmpeg error (code {result.returncode}):\n"
                  f"{result.stderr[-1200:]}")
            return input_file
        print(f"[watermark] Done → {output_file}")
        return output_file
    except Exception as ex:
        print(f"[watermark] Exception: {ex}")
        return input_file
    finally:
        if filter_file and os.path.exists(filter_file):
            os.remove(filter_file)


# ─── Helpers used by main.py ──────────────────────────────────────────────────

def duration(filename):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of",
         "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return float(result.stdout)

def get_mps_and_keys(api_url):
    response = requests.get(api_url)
    response_json = response.json()
    mpd  = response_json.get('MPD')
    keys = response_json.get('KEYS')
    return mpd, keys

def exec(cmd):
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = process.stdout.decode()
    print(output)
    return output

def pull_run(work, cmds):
    with concurrent.futures.ThreadPoolExecutor(max_workers=work) as executor:
        print("Waiting for tasks to complete")
        fut = executor.map(exec, cmds)

async def aio(url, name):
    k = f'{name}.pdf'
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                f = await aiofiles.open(k, mode='wb')
                await f.write(await resp.read())
                await f.close()
    return k

async def download(url, name):
    MIME_TO_EXT = {
        'video/mp4':           'mp4',
        'video/x-matroska':    'mkv',
        'video/webm':          'webm',
        'video/quicktime':     'mov',
        'video/x-msvideo':     'avi',
        'application/pdf':     'pdf',
        'image/jpeg':          'jpg',
        'image/png':           'png',
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                content_type = resp.headers.get('Content-Type', '').split(';')[0].strip()
                ext = MIME_TO_EXT.get(content_type)
                if not ext:
                    from urllib.parse import urlparse
                    path = urlparse(str(resp.url)).path
                    _, url_ext = os.path.splitext(path)
                    ext = url_ext.lstrip('.') if url_ext else 'pdf'
                ka = f'{name}.{ext}'
                f  = await aiofiles.open(ka, mode='wb')
                await f.write(await resp.read())
                await f.close()
    return ka

async def pdf_download(url, file_name, chunk_size=1024 * 10):
    if os.path.exists(file_name):
        os.remove(file_name)
    r = requests.get(url, allow_redirects=True, stream=True)
    with open(file_name, 'wb') as fd:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                fd.write(chunk)
    return file_name

def parse_vid_info(info):
    info = info.strip()
    info = info.split("\n")
    new_info = []
    temp = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i.strip()
            i = i.split("|")[0].split(" ", 2)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.append((i[0], i[2]))
            except:
                pass
    return new_info

def vid_info(info):
    info = info.strip()
    info = info.split("\n")
    new_info = dict()
    temp = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i.strip()
            i = i.split("|")[0].split(" ", 3)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.update({f'{i[2]}': f'{i[0]}'})
            except:
                pass
    return new_info

async def decrypt_and_merge_video(mpd_url, keys_string, output_path, output_name, quality="720"):
    try:
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)

        cmd1 = (
            f'yt-dlp -f "bv[height<={quality}]+ba/b" '
            f'-o "{output_path}/file.%(ext)s" '
            f'--allow-unplayable-format --no-check-certificate '
            f'--external-downloader aria2c "{mpd_url}"'
        )
        print(f"Running command: {cmd1}")
        os.system(cmd1)

        avDir = list(output_path.iterdir())
        print(f"Downloaded files: {avDir}")
        print("Decrypting")

        video_decrypted = False
        audio_decrypted = False

        for data in avDir:
            if data.suffix == ".mp4" and not video_decrypted:
                cmd2 = f'mp4decrypt {keys_string} --show-progress "{data}" "{output_path}/video.mp4"'
                print(f"Running command: {cmd2}")
                os.system(cmd2)
                if (output_path / "video.mp4").exists():
                    video_decrypted = True
                data.unlink()
            elif data.suffix == ".m4a" and not audio_decrypted:
                cmd3 = f'mp4decrypt {keys_string} --show-progress "{data}" "{output_path}/audio.m4a"'
                print(f"Running command: {cmd3}")
                os.system(cmd3)
                if (output_path / "audio.m4a").exists():
                    audio_decrypted = True
                data.unlink()

        if not video_decrypted or not audio_decrypted:
            raise FileNotFoundError("Decryption failed: video or audio file not found.")

        cmd4 = (
            f'ffmpeg -i "{output_path}/video.mp4" -i "{output_path}/audio.m4a" '
            f'-c copy "{output_path}/{output_name}.mp4"'
        )
        print(f"Running command: {cmd4}")
        os.system(cmd4)

        if (output_path / "video.mp4").exists():
            (output_path / "video.mp4").unlink()
        if (output_path / "audio.m4a").exists():
            (output_path / "audio.m4a").unlink()

        filename = output_path / f"{output_name}.mp4"
        if not filename.exists():
            raise FileNotFoundError("Merged video file not found.")

        cmd5 = f'ffmpeg -i "{filename}" 2>&1 | grep "Duration"'
        duration_info = os.popen(cmd5).read()
        print(f"Duration info: {duration_info}")

        return str(filename)

    except Exception as e:
        print(f"Error during decryption and merging: {str(e)}")
        raise

async def run(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    print(f'[{cmd!r} exited with {proc.returncode}]')
    if proc.returncode == 1:
        return False
    if stdout:
        return f'[stdout]\n{stdout.decode()}'
    if stderr:
        return f'[stderr]\n{stderr.decode()}'

def old_download(url, file_name, chunk_size=1024 * 10 * 10):
    if os.path.exists(file_name):
        os.remove(file_name)
    r = requests.get(url, allow_redirects=True, stream=True)
    with open(file_name, 'wb') as fd:
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                fd.write(chunk)
    return file_name

def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if size < 1024.0 or unit == 'PB':
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f} {unit}"

def time_name():
    date = datetime.date.today()
    now  = datetime.datetime.now()
    current_time = now.strftime("%H%M%S")
    return f"{date} {current_time}.mp4"


failed_counter = 0  # global retry counter for download_video

async def download_video(url, cmd, name):
    download_cmd = (
        f'{cmd} -R 25 --fragment-retries 25 '
        f'--external-downloader aria2c '
        f'--downloader-args "aria2c:-x 16 -j 32"'
    )
    global failed_counter
    print(download_cmd)
    logging.info(download_cmd)
    k = subprocess.run(download_cmd, shell=True)
    if "visionias" in cmd and k.returncode != 0 and failed_counter <= 10:
        failed_counter += 1
        await asyncio.sleep(5)
        await download_video(url, cmd, name)
    failed_counter = 0
    try:
        if os.path.isfile(name):
            return name
        elif os.path.isfile(f"{name}.webm"):
            return f"{name}.webm"
        name = name.split(".")[0]
        if os.path.isfile(f"{name}.mkv"):
            return f"{name}.mkv"
        elif os.path.isfile(f"{name}.mp4"):
            return f"{name}.mp4"
        elif os.path.isfile(f"{name}.mp4.webm"):
            return f"{name}.mp4.webm"
        return name
    except FileNotFoundError:
        return os.path.isfile.splitext[0] + "." + "mp4"

async def send_doc(bot: Client, m: Message, cc, ka, cc1, prog, count, name, channel_id):
    reply = await bot.send_message(channel_id, f"Downloading pdf:\n<pre><code>{name}</code></pre>")
    time.sleep(1)
    start_time = time.time()
    await bot.send_document(chat_id=channel_id, document=ka, caption=cc1)
    count += 1
    await reply.delete(True)
    time.sleep(1)
    os.remove(ka)
    time.sleep(3)

def decrypt_file(file_path, key):
    if not os.path.exists(file_path):
        return False
    with open(file_path, "r+b") as f:
        num_bytes = min(28, os.path.getsize(file_path))
        with mmap.mmap(f.fileno(), length=num_bytes, access=mmap.ACCESS_WRITE) as mmapped_file:
            for i in range(num_bytes):
                mmapped_file[i] ^= ord(key[i]) if i < len(key) else i
    return True

async def download_and_decrypt_video(url, cmd, name, key):
    video_path = await download_video(url, cmd, name)
    if video_path:
        decrypted = decrypt_file(video_path, key)
        if decrypted:
            print(f"File {video_path} decrypted successfully.")
            return video_path
        else:
            print(f"Failed to decrypt {video_path}.")
            return None

MAX_FILE_SIZE_BYTES = 2000 * 1024 * 1024  # 2000 MB

async def split_video(filename):
    """Split a video into parts of ~1999 MB each using ffmpeg segment muxer."""
    base, ext = os.path.splitext(filename)
    pattern   = f"{base}_part%03d{ext}"

    file_size  = os.path.getsize(filename)
    part_size  = 1999 * 1024 * 1024
    num_parts  = ceil(file_size / part_size)

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    total_duration = float(result.stdout.strip() or 0)
    part_duration_secs = int(total_duration / num_parts) if num_parts > 1 else int(total_duration)

    cmd = (
        f'ffmpeg -i "{filename}" -c copy -map 0 '
        f'-segment_time {part_duration_secs} -f segment -reset_timestamps 1 '
        f'"{pattern}" -y'
    )
    subprocess.run(cmd, shell=True)

    dir_name = os.path.dirname(filename) or "."
    parts = sorted([
        f for f in os.listdir(dir_name)
        if os.path.basename(f).startswith(os.path.basename(base) + "_part")
        and f.endswith(ext)
    ])
    return [os.path.join(dir_name, p) for p in parts]


# ─── send_vid ─────────────────────────────────────────────────────────────────

async def send_vid(
    bot: Client,
    m: Message,
    cc,
    filename,
    thumb,
    name,
    prog,
    channel_id,
    topic_id=None,
    watermark_text: str = None,   # ← NEW: pass CR / credit string here
):
    """
    Send a video to a channel, optionally inside a forum topic.

    watermark_text: if provided, a random-timed text overlay is burned into
                    the video before upload.  Pass None to skip watermarking.
    """
    # ── Optional: burn random text watermark ─────────────────────────────────
    if watermark_text:
        base, ext = os.path.splitext(filename)
        wm_output = f"{base}_wm{ext or '.mp4'}"
        status_msg = await m.reply_text(
            f"🖊️ **Adding watermark...**\n<blockquote>{name}</blockquote>"
        )
        # Run blocking FFmpeg in executor so the event loop isn't blocked
        loop = asyncio.get_event_loop()
        watermarked = await loop.run_in_executor(
            None, add_random_text_overlay, filename, wm_output, watermark_text
        )
        await status_msg.delete()
        if watermarked != filename:
            # Overlay succeeded — remove the original, use watermarked copy
            try:
                os.remove(filename)
            except OSError:
                pass
            filename = watermarked
        else:
            print(f"[send_vid] Watermark failed/skipped for {name}, sending original")
    # ─────────────────────────────────────────────────────────────────────────

    subprocess.run(
        f'ffmpeg -i "{filename}" -ss 00:00:10 -vframes 1 "{filename}.jpg"',
        shell=True,
    )
    await prog.delete(True)

    thread_kwargs = {"message_thread_id": topic_id} if topic_id else {}

    reply1 = await bot.send_message(
        channel_id,
        f"**📩 Uploading Video 📩:-**\n<blockquote>**{name}**</blockquote>",
        **thread_kwargs,
    )
    reply = await m.reply_text(
        f"**Generate Thumbnail:**\n<blockquote>**{name}**</blockquote>"
    )

    try:
        if thumb == "/d":
            thumbnail = f"{filename}.jpg"
        else:
            thumbnail = thumb
    except Exception as e:
        await m.reply_text(str(e))

    dur        = int(duration(filename))
    start_time = time.time()
    file_size  = os.path.getsize(filename)

    try:
        if file_size > MAX_FILE_SIZE_BYTES:
            # ── Large file: split into parts ─────────────────────────────────
            split_msg = await m.reply_text(
                f"⚠️ File size is **{file_size // (1024*1024)} MB**, splitting into parts..."
            )
            parts = await split_video(filename)
            await split_msg.delete()
            if not parts:
                await m.reply_text("❌ Splitting failed, attempting to send original file...")
                parts = [filename]

            for idx, part_file in enumerate(parts, start=1):
                part_caption = f"{cc}\n\n📦 **Part {idx}/{len(parts)}**"
                part_dur     = int(duration(part_file))
                start_time   = time.time()
                try:
                    await bot.send_video(
                        channel_id, part_file,
                        caption=part_caption,
                        supports_streaming=True,
                        height=720, width=1280,
                        thumb=thumbnail,
                        duration=part_dur,
                        progress=progress_bar,
                        progress_args=(reply, start_time),
                        **thread_kwargs,
                    )
                except Exception:
                    await bot.send_document(
                        channel_id, part_file,
                        caption=part_caption,
                        progress=progress_bar,
                        progress_args=(reply, start_time),
                        **thread_kwargs,
                    )
                if part_file != filename and os.path.exists(part_file):
                    os.remove(part_file)

        else:
            # ── Normal send ───────────────────────────────────────────────────
            try:
                await bot.send_video(
                    channel_id, filename,
                    caption=cc,
                    supports_streaming=True,
                    height=720, width=1280,
                    thumb=thumbnail,
                    duration=dur,
                    progress=progress_bar,
                    progress_args=(reply, start_time),
                    **thread_kwargs,
                )
            except Exception:
                await bot.send_document(
                    channel_id, filename,
                    caption=cc,
                    progress=progress_bar,
                    progress_args=(reply, start_time),
                    **thread_kwargs,
                )

    finally:
        if os.path.exists(filename):
            os.remove(filename)
        await reply.delete(True)
        await reply1.delete(True)
        thumb_path = f"{filename}.jpg"
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
