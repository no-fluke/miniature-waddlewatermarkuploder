import os
import re
import time
import mmap
import random
import tempfile
import shutil
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

# ─── Font Resolution ──────────────────────────────────────────────────────────

_WM_EDGE_MARGIN = 0.08

def _resolve_font() -> str:
    candidates = [
        "DejaVuSans.ttf",
        "/app/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans.ttf",
        "/app/.fonts/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""

_WM_FONT = _resolve_font()

# ─── Memory limit: how many FFmpeg encodes run at the same time ───────────────
# On Heroku 1 GB: 1 concurrent encode ≈ 180–250 MB → keep MAX_PARALLEL = 2
# to stay well under 1 GB even with Python + Pyrogram overhead (~300 MB idle).
# Raise to 3 only if you upgrade to a 2 GB dyno.
_MAX_PARALLEL_ENCODES = int(os.environ.get("WM_MAX_PARALLEL", "2"))


# ─── Core: Single-pass watermark ─────────────────────────────────────────────

def add_random_text_overlay(
    input_file: str,
    output_file: str,
    text: str,
    progress_callback=None,
    time_offset: float = 0.0,
    period_x: float = None,
    period_y: float = None,
    phase_x: float = None,
    phase_y: float = None,
) -> str:
    """
    Burns a continuously wandering text watermark into a video using FFmpeg.
    """
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
        vid_w    = int(lines[0])   if len(lines) > 0 else 1280
        vid_h    = int(lines[1])   if len(lines) > 1 else 720
        duration = float(lines[2]) if len(lines) > 2 else 0.0
    except Exception as e:
        print(f"[watermark] ffprobe failed: {e} — skipping overlay")
        return input_file

    if not _WM_FONT:
        print("[watermark] No font available — skipping overlay")
        return input_file
    font = _WM_FONT

    fontsize   = 28
    text_w_est = int(len(text) * fontsize * 0.60)
    text_h_est = int(fontsize * 1.2)

    margin_x   = int(vid_w * _WM_EDGE_MARGIN)
    margin_y   = int(vid_h * _WM_EDGE_MARGIN)
    safe_x_min = margin_x
    safe_x_max = max(margin_x + 1, vid_w - margin_x - text_w_est)
    safe_y_min = margin_y
    safe_y_max = max(margin_y + 1, vid_h - margin_y - text_h_est)

    range_x = (safe_x_max - safe_x_min) / 2
    range_y = (safe_y_max - safe_y_min) / 2
    cx      = safe_x_min + range_x
    cy      = safe_y_min + range_y

    if period_x is None:
        period_x = random.uniform(180, 240)
    if period_y is None:
        period_y = period_x * 1.4142135623730951
    if phase_x is None:
        phase_x = random.uniform(0, 6.2832)
    if phase_y is None:
        phase_y = random.uniform(0, 6.2832)

    print(
        f"[watermark] {vid_w}x{vid_h} fontsize={fontsize} "
        f"offset={time_offset:.1f}s px={period_x:.1f}s py={period_y:.1f}s"
    )

    safe_text = (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
    )

    fontfile_clause = (
        f":fontfile='{font.replace(chr(58), chr(92) + chr(58))}'" if font else ""
    )

    t_expr  = f"(t+{time_offset:.4f})"
    x_expr  = f"{cx:.1f}+{range_x:.1f}*sin(6.2832/{period_x:.4f}*{t_expr}+{phase_x:.4f})"
    y_expr  = f"{cy:.1f}+{range_y:.1f}*sin(6.2832/{period_y:.4f}*{t_expr}+{phase_y:.4f})"

    drawtext_filter = (
        f"drawtext="
        f"text='{safe_text}'"
        f"{fontfile_clause}"
        f":fontsize={fontsize}"
        f":fontcolor=white@0.55"
        f":shadowcolor=black@0.55"
        f":shadowx=2:shadowy=2"
        f":x={x_expr}"
        f":y={y_expr}"
    )

    filter_chain = f"{drawtext_filter},format=yuv420p"

    try:
        process = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-i", input_file,
                "-vf", filter_chain,
                "-c:v", "libx264",
                "-crf", "23",
                "-preset", "fast",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-progress", "pipe:1",
                output_file,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        last_pct = -1
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            line = line.strip()
            if line.startswith("out_time_ms=") and duration > 0:
                try:
                    out_ms = int(line.split("=")[1])
                    pct    = min(100, int((out_ms / 1000) / duration * 100))
                    if pct != last_pct and progress_callback:
                        progress_callback(pct)
                        last_pct = pct
                except Exception:
                    pass

        process.wait(timeout=3600)
        if process.returncode != 0:
            err = (process.stderr.read() or "")[-2000:]
            print(f"[watermark] FFmpeg error (code {process.returncode}):\n{err}")
            return input_file

        print(f"[watermark] Done → {output_file}")
        return output_file

    except Exception as ex:
        print(f"[watermark] Exception: {ex}")
        return input_file


# ─── Memory-safe chunked watermark ────────────────────────────────────────────

def add_watermark_parallel(
    input_file: str,
    output_file: str,
    text: str,
    chunk_duration: int = 600,      # ← 10 min chunks: fewer chunks = less temp disk
    progress_callback=None,
    workers: int = None,            # ignored now; use _MAX_PARALLEL_ENCODES env var
) -> str:
    """
    Memory-safe chunked watermarking pipeline:

      1. Split input into chunks with -c copy  (instant, no re-encode)
      2. Watermark chunks with a semaphore-limited ThreadPoolExecutor
         so at most _MAX_PARALLEL_ENCODES FFmpeg processes run at once.
         Default = 2 → safely under Heroku 1 GB RAM.
      3. Delete each raw chunk immediately after its watermarked version
         is produced, keeping temp disk usage low.
      4. Concat watermarked chunks with -c copy (instant, no re-encode)

    Set env var WM_MAX_PARALLEL=1 to go fully sequential (safest, slowest).
    Set WM_MAX_PARALLEL=3 only on a 2 GB+ dyno.
    """
    tmp_dir = tempfile.mkdtemp(prefix="wm_parallel_")
    print(f"[wm_parallel] Working in {tmp_dir} | max_parallel={_MAX_PARALLEL_ENCODES}")

    try:
        # ── Step 1: Probe total duration ──────────────────────────────────────
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             input_file],
            capture_output=True, text=True, timeout=30,
        )
        total_duration = float(probe.stdout.strip() or 0)
        if total_duration < 5:
            print("[wm_parallel] Video too short — single pass")
            return add_random_text_overlay(input_file, output_file, text, progress_callback)

        if progress_callback:
            progress_callback(2)

        # ── Step 2: Split at keyframes (-c copy, ultra-fast) ─────────────────
        chunk_pattern = os.path.join(tmp_dir, "chunk_%04d.mp4")
        split_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_file,
                "-c", "copy",
                "-map", "0",
                "-segment_time", str(chunk_duration),
                "-f", "segment",
                "-reset_timestamps", "1",
                chunk_pattern,
            ],
            capture_output=True, text=True,
        )
        if split_result.returncode != 0:
            raise RuntimeError(f"Split failed: {split_result.stderr[-1000:]}")

        chunks = sorted([
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if f.startswith("chunk_") and f.endswith(".mp4")
        ])
        if not chunks:
            raise RuntimeError("No chunks produced by split step")

        print(f"[wm_parallel] Split into {len(chunks)} chunks")
        if progress_callback:
            progress_callback(5)

        # ── Step 3: Shared wave params for seamless motion ────────────────────
        period_x = random.uniform(200, 500)
        period_y = period_x * 1.4142135623730951
        phase_x  = random.uniform(0, 6.2832)
        phase_y  = random.uniform(0, 6.2832)

        # Measure each chunk's actual duration to compute exact time offsets
        offsets      = []
        running_time = 0.0
        for chunk in chunks:
            offsets.append(running_time)
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 chunk],
                capture_output=True, text=True,
            )
            chunk_dur    = float(r.stdout.strip() or chunk_duration)
            running_time += chunk_dur

        # ── Step 4: Semaphore-limited threaded encode ─────────────────────────
        # ThreadPoolExecutor (not multiprocessing) — workers share the same
        # Python process so there's no per-process overhead.
        # The semaphore caps concurrent FFmpeg subprocesses regardless of
        # how many threads the executor spins up.
        sem         = concurrent.futures.ThreadPoolExecutor(
                          max_workers=_MAX_PARALLEL_ENCODES
                      )
        wm_chunks   = [None] * len(chunks)
        total        = len(chunks)
        completed    = [0]

        def encode_chunk(idx, chunk, offset):
            out_chunk = os.path.join(tmp_dir, f"wm_{idx:04d}.mp4")
            result = add_random_text_overlay(
                input_file=chunk,
                output_file=out_chunk,
                text=text,
                progress_callback=None,
                time_offset=offset,
                period_x=period_x,
                period_y=period_y,
                phase_x=phase_x,
                phase_y=phase_y,
            )
            # ── Free raw chunk disk space immediately after encode ────────────
            # This is critical: without this, raw + watermarked chunks
            # coexist and can fill /tmp on low-disk dynos.
            try:
                os.remove(chunk)
            except OSError:
                pass

            completed[0] += 1
            # Rough progress: 5% reserved for split, 90% for encoding, 5% for concat
            pct = 5 + int((completed[0] / total) * 85)
            if progress_callback:
                try:
                    progress_callback(pct)
                except Exception:
                    pass

            return idx, result

        futures = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_ENCODES
        ) as executor:
            for idx, (chunk, offset) in enumerate(zip(chunks, offsets)):
                futures.append(executor.submit(encode_chunk, idx, chunk, offset))

            for fut in concurrent.futures.as_completed(futures):
                idx, result = fut.result()
                wm_chunks[idx] = result

        if progress_callback:
            progress_callback(90)

        # ── Step 5: Concat with -c copy (no re-encode) ────────────────────────
        filelist_path = os.path.join(tmp_dir, "filelist.txt")
        with open(filelist_path, "w") as f:
            for wm_chunk in wm_chunks:
                f.write(f"file '{wm_chunk}'\n")

        concat_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", filelist_path,
                "-c", "copy",
                "-movflags", "+faststart",
                output_file,
            ],
            capture_output=True, text=True,
        )
        if concat_result.returncode != 0:
            raise RuntimeError(f"Concat failed: {concat_result.stderr[-1000:]}")

        if progress_callback:
            progress_callback(100)

        print(f"[wm_parallel] Done → {output_file}")
        return output_file

    except Exception as ex:
        print(f"[wm_parallel] Failed ({ex}) — falling back to single-pass")
        return add_random_text_overlay(input_file, output_file, text, progress_callback)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── send_vid integration ─────────────────────────────────────────────────────

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
    watermark_text: str = None,
):
    if watermark_text:
        base, ext = os.path.splitext(filename)
        wm_output = f"{base}_wm{ext or '.mp4'}"
        status_msg = await m.reply_text(
            f"🖊️ **Adding Watermark...**\n"
            f"<blockquote>{name}</blockquote>\n"
            f"⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%"
        )

        last_pct_sent = [-1]

        async def update_progress(pct):
            if pct == last_pct_sent[0]:
                return
            last_pct_sent[0] = pct
            filled = int(pct / 10)
            bar    = "🟩" * filled + "⬜" * (10 - filled)
            try:
                await status_msg.edit_text(
                    f"🖊️ **Adding Watermark...**\n"
                    f"<blockquote>{name}</blockquote>\n"
                    f"{bar} {pct}%"
                )
            except Exception:
                pass

        def sync_progress_callback(pct):
            asyncio.run_coroutine_threadsafe(update_progress(pct), loop)

        loop = asyncio.get_event_loop()

        watermarked = await loop.run_in_executor(
            None,
            add_watermark_parallel,
            filename, wm_output, watermark_text,
            600,                    # chunk_duration: 10 min (fewer chunks)
            sync_progress_callback,
            None,
        )

        await status_msg.edit_text(
            f"🖊️ **Watermark Done ✅**\n"
            f"<blockquote>{name}</blockquote>\n"
            f"🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩 100%"
        )
        await asyncio.sleep(1)
        await status_msg.delete()

        if watermarked != filename:
            try:
                os.remove(filename)
            except OSError:
                pass
            filename = watermarked
        else:
            print(f"[send_vid] Watermark failed/skipped for {name}, sending original")

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
        thumbnail = f"{filename}.jpg" if thumb == "/d" else thumb
    except Exception as e:
        await m.reply_text(str(e))

    dur        = int(duration(filename))
    start_time = time.time()
    file_size  = os.path.getsize(filename)

    try:
        if file_size > MAX_FILE_SIZE_BYTES:
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


# ─── All original helpers below (unchanged) ───────────────────────────────────

MAX_FILE_SIZE_BYTES = 2000 * 1024 * 1024

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
    output  = process.stdout.decode()
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
        'video/mp4':        'mp4',
        'video/x-matroska': 'mkv',
        'video/webm':       'webm',
        'video/quicktime':  'mov',
        'video/x-msvideo':  'avi',
        'application/pdf':  'pdf',
        'image/jpeg':       'jpg',
        'image/png':        'png',
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                content_type = resp.headers.get('Content-Type', '').split(';')[0].strip()
                ext = MIME_TO_EXT.get(content_type)
                if not ext:
                    from urllib.parse import urlparse
                    path    = urlparse(str(resp.url)).path
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
    info     = info.strip().split("\n")
    new_info = []
    temp     = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i = i.strip().split("|")[0].split(" ", 2)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.append((i[0], i[2]))
            except Exception:
                pass
    return new_info

def vid_info(info):
    info     = info.strip().split("\n")
    new_info = dict()
    temp     = []
    for i in info:
        i = str(i)
        if "[" not in i and '---' not in i:
            while "  " in i:
                i = i.replace("  ", " ")
            i = i.strip().split("|")[0].split(" ", 3)
            try:
                if "RESOLUTION" not in i[2] and i[2] not in temp and "audio" not in i[2]:
                    temp.append(i[2])
                    new_info.update({f'{i[2]}': f'{i[0]}'})
            except Exception:
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

        for f in ["video.mp4", "audio.m4a"]:
            p = output_path / f
            if p.exists():
                p.unlink()

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
    date         = datetime.date.today()
    now          = datetime.datetime.now()
    current_time = now.strftime("%H%M%S")
    return f"{date} {current_time}.mp4"

failed_counter = 0

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

async def split_video(filename):
    """Split a video into parts of ~1999 MB each using ffmpeg segment muxer."""
    base, ext  = os.path.splitext(filename)
    pattern    = f"{base}_part%03d{ext}"
    file_size  = os.path.getsize(filename)
    part_size  = 1999 * 1024 * 1024
    num_parts  = ceil(file_size / part_size)

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    total_duration     = float(result.stdout.strip() or 0)
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
