from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
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
            return ["-c:v", selected, "-preset",
                    nvenc_presets.get(settings.preset, "p4")]
        return ["-c:v", selected]
    codec_map = {"h264": "libx264", "h265": "libx265"}
    return ["-c:v", codec_map.get(codec, codec), "-preset", settings.preset]


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
        *encoder_args(settings), "-b:v", f"{settings.video_mbps:g}M",
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
    for n, (idx, a, b) in enumerate(pieces):
        trim = "" if a is None else f"trim=start={a:.6f}:end={b:.6f},"
        atrim = "" if a is None else f"atrim=start={a:.6f}:end={b:.6f},"
        filters.append(
            f"[{idx}:v]{trim}setpts=PTS-STARTPTS,"
            f"scale={enc.width}:{enc.height}:force_original_aspect_ratio=decrease,"
            f"pad={enc.width}:{enc.height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={enc.fps:g},format=yuv420p[v{n}]")
        filters.append(
            f"[{idx}:a]{atrim}asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{n}]")
        concat_labels.append(f"[v{n}][a{n}]")
    filters.append("".join(concat_labels) + f"concat=n={len(pieces)}:v=1:a=1[cv][ca]")
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

    audio_map = {"aac": "aac", "mp3": "libmp3lame"}
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{current_video}]", "-map", "[ca]",
        *encoder_args(enc),
        "-b:v", f"{enc.video_mbps:g}M", "-maxrate", f"{enc.video_mbps:g}M",
        "-bufsize", f"{enc.video_mbps * 2:g}M",
        "-c:a", audio_map.get(enc.audio_codec.lower(), enc.audio_codec),
        "-b:a", f"{enc.audio_kbps}k", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(job.output),
    ]
    return cmd, total


def render(job: Job, progress=None, cancel=None) -> None:
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


def job_debug(job: Job) -> str:
    cmd, _ = build_command(job)
    return json.dumps(cmd, ensure_ascii=False, indent=2)
