from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


class MontageError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    video_bitrate: int
    audio_codec: str
    audio_bitrate: int
    has_audio: bool


@dataclass
class EncodeSettings:
    codec: str
    width: int
    height: int
    fps: float
    video_mbps: float
    audio_codec: str
    audio_kbps: int
    device: str = "cpu"
    preset: str = "fast"
    rate_mode: str = "bitrate"
    quality: int = 23


@dataclass
class Job:
    source: Path
    output: Path
    cut_start: float = 0
    cut_middle: tuple[float, float] | None = None
    cut_end: float = 0
    prepend: list[Path] = field(default_factory=list)
    append: list[Path] = field(default_factory=list)
    inserts: list[tuple[float, Path]] = field(default_factory=list)
    overlays: list[tuple[float, float, Path]] = field(default_factory=list)
    encode: EncodeSettings | None = None
    force_encode: bool = False


def app_file(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def ffmpeg_path() -> Path:
    bundled = app_file("ffmpeg.exe")
    if bundled.exists():
        return bundled
    try:
        import imageio_ffmpeg
        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        raise MontageError("FFmpeg bulunamadı. Uygulama eksik veya bozuk kurulmuş.") from exc


def ffprobe_path() -> Path | None:
    bundled = app_file("ffprobe.exe")
    if bundled.exists():
        return bundled
    system_probe = shutil.which("ffprobe")
    return Path(system_probe) if system_probe else None


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(args, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, creationflags=flags)


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    if not path.is_file():
        raise MontageError(f"Dosya bulunamadı:\n{path}")
    # Çıktı vermeden çağırmak FFmpeg'in yalnızca kapsayıcı/akış başlıklarını
    # okumasını sağlar; videonun tamamını çözümlemek uzun dosyalarda arayüzü kilitler.
    proc = _run([str(ffmpeg_path()), "-hide_banner", "-i", str(path)])
    text = proc.stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    video = re.search(
        r"Video:\s*([^\s,]+).*?(\d{2,5})x(\d{2,5}).*?([\d.]+)\s*fps", text)
    if not dur or not video:
        raise MontageError(f"Geçerli bir video okunamadı:\n{path.name}")
    duration = int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])
    fps = float(video[4])
    audio = re.search(r"Audio:\s*([^\s,]+).*?(\d+)\s*kb/s", text)
    bitrates = re.findall(r"(\d+)\s*kb/s", text)
    total_kbps = int(bitrates[0]) if bitrates else 0
    audio_kbps = int(audio[2]) if audio else 0
    return MediaInfo(
        path=path, duration=duration, width=int(video[2]), height=int(video[3]),
        fps=fps, video_codec=video[1], video_bitrate=max(0, total_kbps - audio_kbps) * 1000,
        audio_codec=audio[1] if audio else "aac", audio_bitrate=audio_kbps * 1000,
        has_audio=bool(audio),
    )


def parse_time(value: str) -> float:
    value = value.strip().replace(",", ".")
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d{1,3})?)", value)
    if not match:
        raise MontageError(f"Geçersiz süre: “{value}”. HH:MM:SS biçimini kullanın.")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def format_time(seconds: float) -> str:
    seconds = max(0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:05.2f}".replace(".00", "")


def validate(job: Job) -> tuple[MediaInfo, float]:
    source = probe(job.source)
    if not job.output.parent.is_dir():
        raise MontageError(f"Çıktı klasörü bulunamadı:\n{job.output.parent}")
    if job.output.resolve() == job.source.resolve():
        raise MontageError("Çıktı dosyası ana videonun üzerine yazılamaz.")
    if job.output.suffix.lower() != ".mp4":
        raise MontageError("Çıktı dosyası .mp4 olmalıdır.")
    if job.cut_start < 0 or job.cut_end < 0:
        raise MontageError("Baştan/sondan kırpma süresi negatif olamaz.")
    end = source.duration - job.cut_end
    if job.cut_start >= end:
        raise MontageError("Kırpma videonun tamamını siliyor.")
    removed = 0.0
    if job.cut_middle:
        a, b = job.cut_middle
        if not (job.cut_start <= a < b <= end):
            raise MontageError("İç kırpma aralığı kalan video süresinin içinde olmalıdır.")
        removed = b - a
    base_duration = end - job.cut_start - removed
    kept_ranges = _source_ranges(job, source)
    for position, path in job.inserts:
        if not 0 <= position <= source.duration:
            raise MontageError(
                f"“{path.name}” ekleme zamanı kaynak video süresini aşıyor "
                f"({format_time(source.duration)}).")
        if not any(a <= position <= b for a, b in kept_ranges):
            raise MontageError(
                f"“{path.name}” için seçilen {format_time(position)} noktası "
                "kırpılarak silinen bir bölgenin içinde.")
    total = base_duration
    for path in [*job.prepend, *job.append]:
        total += probe(path).duration
    for _, path in job.inserts:
        total += probe(path).duration
    for start, duration, path in job.overlays:
        if not path.is_file():
            raise MontageError(f"Görsel bulunamadı:\n{path}")
        if not 0 <= start <= source.duration:
            raise MontageError(
                f"“{path.name}” görsel zamanı kaynak video süresini aşıyor.")
        if not any(a <= start <= b for a, b in kept_ranges):
            raise MontageError(
                f"“{path.name}” için seçilen {format_time(start)} noktası "
                "kırpılarak silinen bir bölgenin içinde.")
        mapped_start = _map_source_time(job, source, start)
        if duration <= 0 or mapped_start + duration > total:
            raise MontageError(f"“{path.name}” görsel süresi çıktı zaman çizgisini aşıyor.")
    if job.encode is None:
        raise MontageError("Encoding ayarları oluşturulamadı.")
    enc = job.encode
    if min(enc.width, enc.height, enc.fps, enc.video_mbps, enc.audio_kbps) <= 0:
        raise MontageError("Encoding değerleri sıfırdan büyük olmalıdır.")
    if enc.rate_mode not in ("bitrate", "quality"):
        raise MontageError("Geçersiz bitrate kontrol modu.")
    if not 0 <= enc.quality <= 51:
        raise MontageError("Kalite değeri 0 ile 51 arasında olmalıdır.")
    if enc.width % 2 or enc.height % 2:
        raise MontageError("Çözünürlük değerleri çift sayı olmalıdır.")
    return source, total


def estimated_bytes(duration: float, settings: EncodeSettings) -> int:
    return int(duration * (settings.video_mbps * 1_000_000 + settings.audio_kbps * 1000) / 8)


def encoder_args(settings: EncodeSettings) -> list[str]:
    codec = settings.codec.lower()
    if settings.device == "gpu":
        encoders = _run([str(ffmpeg_path()), "-hide_banner", "-encoders"]).stdout
        candidates = (
            ["h264_nvenc", "h264_qsv", "h264_amf"] if codec == "h264"
            else ["hevc_nvenc", "hevc_qsv", "hevc_amf"]
        )
        selected = next((name for name in candidates if name in encoders), None)
        if selected is None:
            raise MontageError(
                f"{codec.upper()} için desteklenen GPU encoder bulunamadı.")
        if selected.endswith("_nvenc"):
            nvenc_presets = {
                "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
                "faster": "p4", "fast": "p5", "medium": "p6", "slow": "p7",
            }
            args = ["-c:v", selected, "-preset",
                    nvenc_presets.get(settings.preset, "p4"), "-rc", "vbr"]
            if settings.rate_mode == "quality":
                args += ["-cq", str(settings.quality), "-b:v", "0"]
            return args
        if settings.rate_mode == "quality" and selected.endswith("_qsv"):
            return ["-c:v", selected, "-global_quality", str(settings.quality)]
        if settings.rate_mode == "quality" and selected.endswith("_amf"):
            return [
                "-c:v", selected, "-rc", "qvbr",
                "-qvbr_quality_level", str(settings.quality)]
        return ["-c:v", selected]
    codec_map = {"h264": "libx264", "h265": "libx265"}
    args = ["-c:v", codec_map.get(codec, codec), "-preset", settings.preset]
    if settings.rate_mode == "quality":
        args += ["-crf", str(settings.quality)]
    return args


def rate_control_args(settings: EncodeSettings) -> list[str]:
    if settings.rate_mode == "quality":
        return [
            "-maxrate", f"{settings.video_mbps:g}M",
            "-bufsize", f"{settings.video_mbps * 2:g}M"]
    return [
        "-b:v", f"{settings.video_mbps:g}M",
        "-maxrate", f"{settings.video_mbps + 2:g}M",
        "-bufsize", f"{settings.video_mbps * 2:g}M"]


def benchmark_encode(source: Path, settings: EncodeSettings) -> float:
    """Seçili ayarlarda saniye başına gereken gerçek işlem süresini ölç."""
    info = probe(source)
    sample_duration = min(8.0, info.duration)
    if sample_duration < 1:
        raise MontageError("PC gücü testi için video en az 1 saniye olmalıdır.")
    vf = (
        f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,"
        f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={settings.fps:g},format=yuv420p"
    )
    cmd = [
        str(ffmpeg_path()), "-hide_banner", "-loglevel", "error",
        "-ss", "0", "-t", f"{sample_duration:g}", "-i", str(source),
        "-map", "0:v:0", "-vf", vf,
        *encoder_args(settings), *rate_control_args(settings),
        "-an", "-f", "null", "-",
    ]
    started = time.perf_counter()
    proc = _run(cmd)
    elapsed = time.perf_counter() - started
    if proc.returncode:
        tail = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise MontageError("PC gücü ölçülemedi.\n\n" + tail)
    return max(0.01, elapsed / sample_duration)


def _map_source_time(job: Job, info: MediaInfo, source_time: float) -> float:
    """Kaynak videodaki zamanı birleşmiş çıktı zaman çizgisine dönüştür."""
    output_time = sum(probe(path).duration for path in job.prepend)
    for a, b in _source_ranges(job, info):
        if source_time >= b:
            output_time += b - a
        elif source_time >= a:
            output_time += source_time - a
            break
    for position, path in job.inserts:
        if position <= source_time:
            output_time += probe(path).duration
    return output_time


def _source_ranges(job: Job, info: MediaInfo) -> list[tuple[float, float]]:
    start, end = job.cut_start, info.duration - job.cut_end
    if not job.cut_middle:
        return [(start, end)]
    a, b = job.cut_middle
    return [(start, a), (b, end)]


def _split_ranges(ranges: list[tuple[float, float]], points: list[float]) -> list[tuple[float, float]]:
    """Kaynak zaman noktalarında korunan ana video parçalarını böl."""
    result: list[tuple[float, float]] = []
    for src_a, src_b in ranges:
        cuts = sorted({p for p in points if src_a < p < src_b})
        bounds = [src_a, *cuts, src_b]
        result.extend((a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 1e-6)
    return result


def stream_signature(path: Path) -> tuple:
    probe_exe = ffprobe_path()
    if probe_exe is None:
        raise MontageError("Kesin akış doğrulaması için ffprobe bulunamadı.")
    proc = _run([
        str(probe_exe), "-v", "error", "-show_data_hash", "sha256",
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,width,height,pix_fmt,"
        "field_order,r_frame_rate,time_base,color_range,color_space,"
        "color_transfer,color_primaries,sample_fmt,sample_rate,channels,"
        "channel_layout,extradata_hash",
        "-of", "json", str(path),
    ])
    if proc.returncode:
        raise MontageError(f"Geçerli bir video okunamadı:\n{path.name}")
    try:
        streams = json.loads(proc.stdout)["streams"]
    except (KeyError, TypeError, ValueError) as exc:
        raise MontageError(f"Akış bilgileri okunamadı:\n{path.name}") from exc
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MontageError(f"Geçerli bir video okunamadı:\n{path.name}")
    video_keys = (
        "codec_name", "profile", "width", "height", "pix_fmt", "field_order",
        "r_frame_rate", "time_base", "color_range", "color_space",
        "color_transfer", "color_primaries", "extradata_hash",
    )
    audio_keys = (
        "codec_name", "profile", "sample_fmt", "sample_rate", "channels",
        "channel_layout", "time_base", "extradata_hash",
    )
    return (
        tuple(video.get(key) for key in video_keys),
        tuple(audio.get(key) for key in audio_keys) if audio else None,
    )


def frame_at(
        path: Path, timestamp: float, media_info: MediaInfo | None = None
        ) -> tuple[bool, bool]:
    probe_exe = ffprobe_path()
    if probe_exe is None:
        return False, False
    info = media_info or probe(path)
    tolerance = max(0.001, 0.5 / max(info.fps, 1.0))
    window_start = max(0.0, timestamp - 2.0)
    window_end = min(info.duration, timestamp + 2.0)
    try:
        proc = _run([
            str(probe_exe), "-v", "error", "-select_streams", "v:0",
            "-read_intervals", f"{window_start:.6f}%{window_end:.6f}",
            "-show_entries", "frame=best_effort_timestamp_time,key_frame",
            "-of", "csv=p=0", str(path),
        ])
    except OSError:
        return False, False
    if proc.returncode:
        return False, False
    for line in proc.stdout.splitlines():
        match = re.match(r"\s*([01]),([\d.]+)", line)
        if not match:
            continue
        key_frame = match[1] == "1"
        frame_time = float(match[2])
        if abs(frame_time - timestamp) <= tolerance:
            return True, key_frame
    return False, False


def is_keyframe(path: Path, timestamp: float) -> bool:
    return frame_at(path, timestamp)[1]


def _run_checked(args: list[str], message: str) -> None:
    proc = _run(args)
    if proc.returncode:
        tail = "\n".join(proc.stderr.strip().splitlines()[-10:])
        raise MontageError(message + "\n\n" + tail)


def _normalize_clip(
        source: Path, output: Path, target: MediaInfo, target_signature: tuple) -> None:
    codec = "libx265" if target.video_codec.lower() in ("hevc", "h265") else "libx264"
    video_mbps = max(1.0, target.video_bitrate / 1_000_000) if target.video_bitrate else 8.0
    args = [
        str(ffmpeg_path()), "-hide_banner", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf",
        f"scale={target.width}:{target.height}:force_original_aspect_ratio=decrease,"
        f"pad={target.width}:{target.height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={target.fps:g},format=yuv420p",
        "-c:v", codec, "-preset", "fast", "-b:v", f"{video_mbps:g}M",
        "-maxrate", f"{video_mbps + 2:g}M",
        "-bufsize", f"{video_mbps * 2:g}M",
    ]
    if target.has_audio:
        audio_signature = target_signature[1] or ()
        sample_rate = audio_signature[3] if len(audio_signature) > 3 else 48000
        layout = audio_signature[5] if len(audio_signature) > 5 else "stereo"
        channels = 1 if "mono" in layout else 2
        audio_encoder = (
            "libmp3lame" if target.audio_codec.lower() in ("mp3", "mp3float")
            else "aac")
        args += [
            "-c:a", audio_encoder, "-b:a",
            f"{max(96, target.audio_bitrate // 1000) if target.audio_bitrate else 192}k",
            "-ar", str(sample_rate), "-ac", str(channels),
        ]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(output)]
    _run_checked(args, f"“{source.name}” ana videoya uyarlanamadı.")


def _copy_segment(source: Path, output: Path, start: float, end: float) -> None:
    args = [str(ffmpeg_path()), "-hide_banner", "-y"]
    if start > 0:
        args += ["-ss", f"{start:.6f}"]
    args += ["-i", str(source)]
    if end > start:
        args += ["-t", f"{end - start:.6f}"]
    args += ["-map", "0", "-c", "copy", str(output)]
    _run_checked(args, f"“{source.name}” kayıpsız kesilemedi.")


def _validate_smart_output(
        output: Path, expected_duration: float, joins: list[float],
        source_signature: tuple, fps: float) -> bool:
    probe_exe = ffprobe_path()
    if probe_exe is None:
        return False
    try:
        probe(output)
        if stream_signature(output) != source_signature:
            return False
    except MontageError:
        return False
    duration_proc = _run([
        str(probe_exe), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration", "-of",
        "default=noprint_wrappers=1:nokey=1", str(output),
    ])
    try:
        video_duration = float(duration_proc.stdout.strip())
    except ValueError:
        return False
    tolerance = 0.5 / max(fps, 1.0) + 0.001
    if abs(video_duration - expected_duration) > tolerance:
        return False
    for join_time in joins:
        proc = _run([
            str(ffmpeg_path()), "-v", "error",
            "-ss", f"{max(0.0, join_time - 0.25):.6f}",
            "-t", "0.5", "-i", str(output), "-map", "0:v:0",
            "-map", "0:a:0?", "-f", "null", "-",
        ])
        if proc.returncode or proc.stderr.strip():
            return False
    return True


def _smart_timeline(job: Job, info: MediaInfo) -> tuple[list[tuple[Path, float, float]], list[Path]]:
    inserts = sorted(job.inserts, key=lambda item: item[0])
    ranges = _split_ranges(_source_ranges(job, info), [position for position, _ in inserts])
    timeline: list[tuple[Path, float, float]] = []
    additions: list[Path] = []
    for path in job.prepend:
        duration = probe(path).duration
        timeline.append((path, 0.0, duration))
        additions.append(path)
    by_time: dict[float, list[Path]] = {}
    for position, path in inserts:
        by_time.setdefault(position, []).append(path)
    emitted: set[float] = set()
    first = ranges[0][0]
    for path in by_time.get(first, []):
        duration = probe(path).duration
        timeline.append((path, 0.0, duration))
        additions.append(path)
    if first in by_time:
        emitted.add(first)
    for start, end in ranges:
        if start in by_time and start not in emitted:
            for path in by_time[start]:
                duration = probe(path).duration
                timeline.append((path, 0.0, duration))
                additions.append(path)
            emitted.add(start)
        timeline.append((job.source, start, end))
        if end in by_time and end not in emitted:
            for path in by_time[end]:
                duration = probe(path).duration
                timeline.append((path, 0.0, duration))
                additions.append(path)
            emitted.add(end)
    for path in job.append:
        duration = probe(path).duration
        timeline.append((path, 0.0, duration))
        additions.append(path)
    return timeline, additions


def can_smart_render(job: Job, info: MediaInfo) -> bool:
    if job.force_encode or job.overlays:
        return False
    timeline, _ = _smart_timeline(job, info)
    frame_cache: dict[tuple[Path, float], tuple[bool, bool]] = {}
    info_cache: dict[Path, MediaInfo] = {job.source: info}
    for path, start, end in timeline:
        if path not in info_cache:
            info_cache[path] = probe(path)
        path_info = info_cache[path]
        start_key = (path, start)
        if start_key not in frame_cache:
            frame_cache[start_key] = frame_at(path, start, path_info)
        start_result = frame_cache[start_key]
        if not start_result[1]:
            return False
        if end < path_info.duration - (0.5 / max(path_info.fps, 1.0)):
            end_key = (path, end)
            if end_key not in frame_cache:
                frame_cache[end_key] = frame_at(path, end, path_info)
            end_result = frame_cache[end_key]
            if not end_result[0]:
                return False
    return True


def smart_render(job: Job, progress=None, cancel=None) -> None:
    info, _ = validate(job)
    if not can_smart_render(job, info):
        _render_full(job, progress, cancel)
        return
    has_edits = bool(
        job.cut_start or job.cut_end or job.cut_middle or job.prepend
        or job.append or job.inserts)
    if not has_edits:
        if cancel and cancel():
            raise MontageError("İşlem kullanıcı tarafından iptal edildi.")
        shutil.copy2(job.source, job.output)
        if progress:
            progress(1.0)
        return

    timeline, additions = _smart_timeline(job, info)
    source_signature = stream_signature(job.source)
    piece_durations = [end - start for _, start, end in timeline]
    expected_duration = sum(piece_durations)
    joins: list[float] = []
    running_duration = 0.0
    for duration in piece_durations[:-1]:
        running_duration += duration
        joins.append(running_duration)
    token = uuid.uuid4().hex
    temp_dir = job.output.parent
    temporary_files: list[Path] = []
    try:
        normalized: dict[Path, Path] = {}
        for number, path in enumerate(dict.fromkeys(additions)):
            if stream_signature(path) == source_signature:
                normalized[path] = path
            else:
                converted = temp_dir / f".media_editor_{token}_normalized_{number}.mp4"
                temporary_files.append(converted)
                _normalize_clip(path, converted, info, source_signature)
                if stream_signature(converted) != source_signature:
                    _render_full(job, progress, cancel)
                    return
                normalized[path] = converted
        pieces: list[Path] = []
        for index, (path, start, end) in enumerate(timeline):
            if cancel and cancel():
                raise MontageError("İşlem kullanıcı tarafından iptal edildi.")
            actual = normalized.get(path, path)
            piece = temp_dir / f".media_editor_{token}_piece_{index:04d}.mp4"
            temporary_files.append(piece)
            _copy_segment(actual, piece, start if actual == path else 0.0,
                          end if actual == path else probe(actual).duration)
            pieces.append(piece)
            if progress:
                progress(min(0.85, (index + 1) / max(1, len(timeline)) * 0.85))
        concat_file = temp_dir / f".media_editor_{token}_concat.txt"
        temporary_files.append(concat_file)
        concat_file.write_text(
            "\n".join("file '" + str(path).replace("'", "'\\''") + "'" for path in pieces),
            encoding="utf-8")
        _run_checked([
            str(ffmpeg_path()), "-hide_banner", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file), "-map", "0", "-c", "copy",
            "-movflags", "+faststart", str(job.output),
        ], "Videolar kayıpsız birleştirilemedi.")
    finally:
        for temporary_file in temporary_files:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass
    if not job.output.is_file() or job.output.stat().st_size == 0:
        _render_full(job, progress, cancel)
        return
    if not _validate_smart_output(
            job.output, expected_duration, joins, source_signature, info.fps):
        try:
            job.output.unlink()
        except OSError:
            pass
        _render_full(job, progress, cancel)
        return
    if progress:
        progress(1.0)


def build_command(job: Job) -> tuple[list[str], float]:
    info, total = validate(job)
    enc = job.encode
    assert enc is not None
    inserts = sorted(job.inserts, key=lambda item: item[0])
    ranges = _split_ranges(_source_ranges(job, info), [p for p, _ in inserts])
    inputs: list[Path] = [job.source]
    pieces: list[tuple[int, float | None, float | None]] = []

    for path in job.prepend:
        inputs.append(path)
        pieces.append((len(inputs) - 1, None, None))

    insert_at: dict[float, list[Path]] = {}
    for position, path in inserts:
        insert_at.setdefault(position, []).append(path)
    emitted_insert_positions: set[float] = set()
    first_source_time = ranges[0][0]
    if first_source_time in insert_at:
        for path in insert_at[first_source_time]:
            inputs.append(path)
            pieces.append((len(inputs) - 1, None, None))
        emitted_insert_positions.add(first_source_time)
    for a, b in ranges:
        if a in insert_at and a not in emitted_insert_positions:
            for path in insert_at[a]:
                inputs.append(path)
                pieces.append((len(inputs) - 1, None, None))
            emitted_insert_positions.add(a)
        pieces.append((0, a, b))
        for position in list(insert_at):
            if position not in emitted_insert_positions and abs(position - b) < 0.01:
                for path in insert_at[position]:
                    inputs.append(path)
                    pieces.append((len(inputs) - 1, None, None))
                emitted_insert_positions.add(position)

    for path in job.append:
        inputs.append(path)
        pieces.append((len(inputs) - 1, None, None))

    image_indices: list[int] = []
    for _, _, path in job.overlays:
        inputs.append(path)
        image_indices.append(len(inputs) - 1)

    cmd = [str(ffmpeg_path()), "-hide_banner", "-y"]
    for index, path in enumerate(inputs):
        cmd += ["-i", str(path)]

    filters: list[str] = []
    concat_labels: list[str] = []
    input_infos = {
        index: probe(path) for index, path in enumerate(inputs)
        if index not in image_indices
    }
    any_audio = any(item.has_audio for item in input_infos.values())
    for n, (idx, a, b) in enumerate(pieces):
        trim = "" if a is None else f"trim=start={a:.6f}:end={b:.6f},"
        atrim = "" if a is None else f"atrim=start={a:.6f}:end={b:.6f},"
        filters.append(
            f"[{idx}:v]{trim}setpts=PTS-STARTPTS,"
            f"scale={enc.width}:{enc.height}:force_original_aspect_ratio=decrease,"
            f"pad={enc.width}:{enc.height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={enc.fps:g},format=yuv420p[v{n}]")
        if any_audio:
            if input_infos[idx].has_audio:
                filters.append(
                    f"[{idx}:a]{atrim}asetpts=PTS-STARTPTS,aresample=48000,"
                    f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
            else:
                piece_duration = (
                    b - a if a is not None and b is not None
                    else input_infos[idx].duration)
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={piece_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[a{n}]")
            concat_labels.append(f"[v{n}][a{n}]")
        else:
            concat_labels.append(f"[v{n}]")
    filters.append(
        "".join(concat_labels)
        + f"concat=n={len(pieces)}:v=1:a={1 if any_audio else 0}"
        + ("[cv][ca]" if any_audio else "[cv]"))
    current_video = "cv"
    mapped_overlays = [
        (_map_source_time(job, info, start), duration, path)
        for start, duration, path in job.overlays
    ]
    for n, ((start, duration, _), idx) in enumerate(zip(mapped_overlays, image_indices)):
        out = f"ov{n}"
        filters.append(
            f"[{idx}:v]scale={enc.width}:{enc.height}:force_original_aspect_ratio=decrease,"
            f"pad={enc.width}:{enc.height}:(ow-iw)/2:(oh-ih)/2[img{n}];"
            f"[{current_video}][img{n}]overlay=0:0:"
            f"enable='between(t,{start:.6f},{start + duration:.6f})'[{out}]")
        current_video = out

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{current_video}]",
        *encoder_args(enc),
        *rate_control_args(enc),
    ]
    if any_audio:
        audio_map = {"aac": "aac", "mp3": "libmp3lame"}
        cmd += [
            "-map", "[ca]",
            "-c:a", audio_map.get(enc.audio_codec.lower(), enc.audio_codec),
            "-b:a", f"{enc.audio_kbps}k",
        ]
    cmd += [
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
        str(job.output),
    ]
    return cmd, total


def _render_full(job: Job, progress=None, cancel=None) -> None:
    cmd, total = build_command(job)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=flags)
    assert proc.stdout is not None
    error_lines: deque[str] = deque(maxlen=20)

    def drain_errors() -> None:
        if proc.stderr:
            for error_line in proc.stderr:
                error_lines.append(error_line.rstrip())

    error_reader = threading.Thread(target=drain_errors, daemon=True)
    error_reader.start()
    try:
        for line in proc.stdout:
            if cancel and cancel():
                proc.terminate()
                raise MontageError("İşlem kullanıcı tarafından iptal edildi.")
            if line.startswith("out_time_ms=") and progress:
                raw_time = line.split("=", 1)[1].strip()
                if raw_time.isdigit():
                    seconds = int(raw_time) / 1_000_000
                    progress(min(0.99, seconds / max(total, 0.001)))
        code = proc.wait()
        error_reader.join(timeout=2)
        if code:
            tail = "\n".join(error_lines)
            raise MontageError("FFmpeg işlemi tamamlayamadı.\n\n" + tail)
        if not job.output.is_file() or job.output.stat().st_size == 0:
            raise MontageError("İşlem bitti ancak geçerli çıktı dosyası oluşmadı.")
        if progress:
            progress(1.0)
    except Exception:
        if proc.poll() is None:
            proc.terminate()
        if job.output.exists():
            try:
                job.output.unlink()
            except OSError:
                pass
        raise


def render(job: Job, progress=None, cancel=None) -> None:
    validate(job)
    try:
        smart_render(job, progress, cancel)
    except MontageError as smart_error:
        if job.output.exists():
            try:
                job.output.unlink()
            except OSError:
                pass
        if "iptal edildi" in str(smart_error).lower() or (cancel and cancel()):
            raise
        _render_full(job, progress, cancel)


def job_debug(job: Job) -> str:
    cmd, _ = build_command(job)
    return json.dumps(cmd, ensure_ascii=False, indent=2)
