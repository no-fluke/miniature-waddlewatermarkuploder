import os
import re
import time
import mmap
import json
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
import atexit
import signal
from math import ceil
from utils import progress_bar
from pyrogram import Client, filters
from pyrogram.types import Message
from io import BytesIO
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from base64 import b64decode
from typing import Optional

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

# ─── Parallel encode limit ────────────────────────────────────────────────────
# With 60-second fragments each encode uses only ~30-60 MB RAM (tiny clip).
# Python + Pyrogram idle ≈ 300 MB.
# 3 parallel x 60 MB = 180 MB + 300 MB = ~480 MB → safe on 1 GB dyno.
# WM_MAX_PARALLEL=2 → more conservative (~420 MB peak).
# WM_MAX_PARALLEL=4 → only on 2 GB+ dynos.
_MAX_PARALLEL_ENCODES = int(os.environ.get("WM_MAX_PARALLEL", "3"))

# ─── Global semaphore: cap concurrent watermark jobs across all bot users ─────
# Without this, two users uploading simultaneously bypass _MAX_PARALLEL_ENCODES.
_WM_SEMAPHORE = None  # type: Optional[asyncio.Semaphore]

def _get_wm_semaphore() -> asyncio.Semaphore:
    """Lazy-init so we always get the running loop's semaphore."""
    global _WM_SEMAPHORE
    if _WM_SEMAPHORE is None:
        _WM_SEMAPHORE = asyncio.Semaphore(1)
    return _WM_SEMAPHORE

# ─── Temp-dir registry: survive crashes and Heroku SIGTERM ───────────────────
_active_tmp_dirs: list[str] = []

def _cleanup_all_tmp():
    for d in list(_active_tmp_dirs):
        shutil.rmtree(d, ignore_errors=True)

def _sigterm_handler(*_):
    _cleanup_all_tmp()
    os._exit(0)

atexit.register(_cleanup_all_tmp)
signal.signal(signal.SIGTERM, _sigterm_handler)


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
    Uses ffprobe JSON output for reliable dimension/duration parsing.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-show_entries", "format=duration",
                "-of", "json",
                input_file,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data     = json.loads(probe.stdout)
        streams  = data.get("streams", [{}])
        fmt      = data.get("format", {})
        vid_w    = int(streams[0].get("width",    1280))
        vid_h    = int(streams[0].get("height",   720))
        duration = float(fmt.get("duration",      0.0))
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
                "-crf", "20",
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
    chunk_duration: int = 60,       # 60-second fragments: small RAM footprint + smooth progress
    progress_callback=None,
    workers: int = None,            # ignored; always sequential on 1 GB dyno
) -> str:
    """
    Fragment-based watermarking pipeline optimised for Heroku 1 GB:

      1. Probe total duration.
      2. Split into 60-second fragments with -c copy (no re-encode, instant).
         Each fragment is ~10-50 MB on disk — tiny RAM footprint when encoding.
      3. Encode fragments ONE AT A TIME sequentially:
           - FFmpeg reads fragment from disk (not RAM)
           - Watermarked fragment written to disk
           - Raw fragment deleted immediately → only 2 small files exist at once
           - Progress callback fires after each fragment → smooth bar movement
      4. Concat all watermarked fragments with -c copy (no re-encode).
      5. Temp dir cleaned up in finally block AND by atexit/SIGTERM handler.

    Progress breakdown:
      2%        → probe done
      3–5%      → split done
      5–92%     → per-fragment encode (moves smoothly with every fragment)
      92–100%   → concat + faststart
    """
    tmp_dir = tempfile.mkdtemp(prefix="wm_frag_")
    _active_tmp_dirs.append(tmp_dir)
    print(f"[wm_frag] Working in {tmp_dir} | fragment_duration={chunk_duration}s")

    try:
        # ── Step 1: Probe total duration ──────────────────────────────────────
        probe = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "json",
             input_file],
            capture_output=True, text=True, timeout=30,
        )
        probe_data     = json.loads(probe.stdout)
        total_duration = float(probe_data.get("format", {}).get("duration", 0))

        if total_duration < 5:
            print("[wm_frag] Video too short — single pass")
            return add_random_text_overlay(input_file, output_file, text, progress_callback)

        if progress_callback:
            progress_callback(2)

        # ── Step 2: Split into 60-second fragments (-c copy, no re-encode) ────
        frag_pattern = os.path.join(tmp_dir, "frag_%04d.mp4")
        split_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_file,
                "-c", "copy",
                "-map", "0",
                "-segment_time", str(chunk_duration),
                "-f", "segment",
                "-reset_timestamps", "1",
                frag_pattern,
            ],
            capture_output=True, text=True,
        )
        if split_result.returncode != 0:
            raise RuntimeError(f"Split failed: {split_result.stderr[-1000:]}")

        fragments = sorted([
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if f.startswith("frag_") and f.endswith(".mp4")
        ])
        if not fragments:
            raise RuntimeError("No fragments produced by split step")

        total_frags = len(fragments)
        print(f"[wm_frag] Split into {total_frags} fragments (~{chunk_duration}s each)")

        if progress_callback:
            progress_callback(5)

        # ── Step 3: Compute time offsets + fragment durations in one pass ─────
        # Done up front so the encode loop has no ffprobe overhead per fragment.
        offsets      = []
        running_time = 0.0
        for frag in fragments:
            offsets.append(running_time)
            r = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "json",
                 frag],
                capture_output=True, text=True,
            )
            frag_data = json.loads(r.stdout)
            frag_dur  = float(frag_data.get("format", {}).get("duration", chunk_duration))
            running_time += frag_dur

        # ── Step 4: Shared wave params — seamless motion across all fragments ─
        period_x = random.uniform(200, 500)
        period_y = period_x * 1.4142135623730951
        phase_x  = random.uniform(0, 6.2832)
        phase_y  = random.uniform(0, 6.2832)

        # ── Step 5: Parallel fragment encode ──────────────────────────────────
        # _MAX_PARALLEL_ENCODES controls concurrency (default 3 for 60s fragments):
        #   - 60s fragment encode uses ~30-60 MB RAM each
        #   - 3 parallel x 60 MB = 180 MB + 300 MB Python = ~480 MB — safe on 1 GB
        #   - WM_MAX_PARALLEL=2 for more conservative; =4 only on 2 GB+ dynos
        #
        # Thread-safe progress: a Lock ensures parallel threads never race on
        # the completed counter, so the bar always moves forward correctly.
        import threading
        completed_count = [0]
        progress_lock   = threading.Lock()
        wm_fragments    = [None] * total_frags

        def encode_fragment(idx, frag, offset):
            out_frag = os.path.join(tmp_dir, f"wm_{idx:04d}.mp4")

            result = add_random_text_overlay(
                input_file=frag,
                output_file=out_frag,
                text=text,
                progress_callback=None,
                time_offset=offset,
                period_x=period_x,
                period_y=period_y,
                phase_x=phase_x,
                phase_y=phase_y,
            )

            # Delete raw fragment only if watermarked output confirmed on disk
            if result != frag and os.path.exists(out_frag):
                try:
                    os.remove(frag)
                except OSError:
                    pass

            # Thread-safe counter + progress update
            with progress_lock:
                completed_count[0] += 1
                done = completed_count[0]

            pct = 5 + int((done / total_frags) * 87)
            if progress_callback:
                try:
                    progress_callback(pct)
                except Exception:
                    pass

            print(f"[wm_frag] Fragment {done}/{total_frags} done ({pct}%)")
            return idx, result

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_PARALLEL_ENCODES
        ) as executor:
            futures = {
                executor.submit(encode_fragment, idx, frag, offset): idx
                for idx, (frag, offset) in enumerate(zip(fragments, offsets))
            }
            for fut in concurrent.futures.as_completed(futures):
                idx, result = fut.result()
                wm_fragments[idx] = result

        # ── Step 6: Concat all watermarked fragments with -c copy ─────────────
        if progress_callback:
            progress_callback(92)

        filelist_path = os.path.join(tmp_dir, "filelist.txt")
        with open(filelist_path, "w") as f:
            for wm_frag in wm_fragments:
                f.write(f"file '{wm_frag}'\n")

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

        print(f"[wm_frag] Done → {output_file}")
        return output_file

    except Exception as ex:
        print(f"[wm_frag] Failed ({ex}) — falling back to single-pass")
        return add_random_text_overlay(input_file, output_file, text, progress_callback)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            _active_tmp_dirs.remove(tmp_dir)
        except ValueError:
            pass


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

        # ── Global semaphore: only 1 watermark job runs at a time bot-wide ───
        # This prevents two simultaneous uploads from spawning two FFmpeg
        # processes in parallel, defeating _MAX_PARALLEL_ENCODES entirely.
        async with _get_wm_semaphore():
            watermarked = await loop.run_in_executor(
                None,
                add_watermark_parallel,
                filename, wm_output, watermark_text,
                60,                     # fragment_duration: 60s fragments → smooth progress + tiny RAM
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
    """Stream download to disk — never buffers the full file in RAM."""
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
                # ── Stream in 256 KB chunks — never loads full file into RAM ──
                async with aiofiles.open(ka, mode='wb') as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        if chunk:
                            await f.write(chunk)
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
