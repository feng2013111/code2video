"""Compose captured PNG frames into a video with ffmpeg.

Encoder selection is automatic and GPU-aware:
    NVIDIA -> h264_nvenc, Intel -> h264_qsv, AMD -> h264_amf,
    Apple  -> h264_videotoolbox, otherwise -> libx264 (CPU).

ffmpeg lookup: system ffmpeg first (hardware encoders are usually only in a
full build), then the ``imageio-ffmpeg`` pip package as a guaranteed fallback.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .detect import GPUInfo, choose_encoder, detect_ffmpeg

FFMPEG_INSTALL_HINT = """\
ffmpeg was not found. Install it with one of:
  Windows : winget install Gyan.FFmpeg   (or: choco install ffmpeg)
  macOS   : brew install ffmpeg
  Debian  : sudo apt install ffmpeg
  ...or as a pure-Python fallback: pip install imageio-ffmpeg
A system ffmpeg is preferred because it usually ships hardware encoders
(h264_nvenc / h264_qsv / h264_amf / h264_videotoolbox).
"""


@dataclass
class ComposeResult:
    output: Path
    encoder: str
    ffmpeg: str
    ffmpeg_source: str
    size_bytes: int
    command: List[str] = field(default_factory=list)


def vaapi_device() -> str:
    """DRI render node for VAAPI (override with $CODE2VIDEO_VAAPI_DEVICE)."""
    return os.environ.get("CODE2VIDEO_VAAPI_DEVICE", "/dev/dri/renderD128")


def encoder_args(encoder: str, crf: int, preset: str,
                 vaapi_dev: Optional[str] = None) -> List[str]:
    """Per-encoder quality flags. `crf` is the 0-51 x264-style quality scale."""
    if encoder == "h264_nvenc":
        return ["-preset", "p4", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if encoder == "h264_qsv":
        return ["-preset", preset, "-global_quality", str(crf)]
    if encoder == "h264_amf":
        return ["-quality", "quality", "-rc", "cqp",
                "-qp_i", str(crf), "-qp_p", str(crf)]
    if encoder == "h264_videotoolbox":
        # videotoolbox uses a 1-100 "quality" scale (higher = better).
        q = max(1, min(100, int(round((51 - crf) * 100 / 51))))
        return ["-q:v", str(q)]
    if encoder == "h264_vaapi":
        # VAAPI needs the hwupload filter chain (added in build_ffmpeg_command)
        return ["-vaapi_device", vaapi_dev or vaapi_device(), "-qp", str(crf)]
    # libx264 (and any unknown CPU encoder)
    return ["-crf", str(crf), "-preset", preset]


def build_ffmpeg_command(ffmpeg: str, frames_dir: Path, output: Path,
                         fps: int, encoder: str, crf: int = 18,
                         preset: str = "medium",
                         pattern: str = "frame_%05d.png") -> List[str]:
    """Build the full ffmpeg command (pure function, easy to test)."""
    # even-dimension safeguard; VAAPI additionally needs format+nv12|vaapi+hwupload
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if encoder == "h264_vaapi":
        vf += ",format=nv12|vaapi,hwupload"
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", str(fps),
        "-i", str(Path(frames_dir) / pattern),
        "-c:v", encoder,
    ]
    cmd.extend(encoder_args(encoder, crf, preset))
    cmd.extend([
        "-vf", vf,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-r", str(fps),
        str(output),
    ])
    return cmd


def compose(frames_dir: Path, output: Path, fps: int,
            encoder: Optional[str] = None, crf: int = 18, preset: str = "medium",
            gpu: Optional[GPUInfo] = None, pattern: str = "frame_%05d.png",
            log=print) -> ComposeResult:
    """Run ffmpeg over the frame sequence and write the final video."""
    ff = detect_ffmpeg()
    if not ff.ffmpeg:
        raise RuntimeError(FFMPEG_INSTALL_HINT)
    if gpu is None:
        from .detect import detect_gpu
        gpu = detect_gpu()
    enc = encoder or choose_encoder(ff, gpu) or "libx264"
    if enc not in ff.available:
        raise RuntimeError(
            f"encoder '{enc}' is not available in {ff.ffmpeg} "
            f"(available: {', '.join(ff.available) or 'none'})")

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(ff.ffmpeg, Path(frames_dir), output,
                               fps, enc, crf, preset, pattern)
    log(f"  composing with {enc} ({ff.source}) -> {output}")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2000:]
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}):\n{tail}")
    if not output.exists():
        raise RuntimeError("ffmpeg reported success but no output file exists")
    return ComposeResult(output=output, encoder=enc, ffmpeg=ff.ffmpeg,
                         ffmpeg_source=ff.source or "unknown",
                         size_bytes=output.stat().st_size, command=cmd)


def probe_duration(video: Path) -> Optional[float]:
    """Best-effort duration probe via ffprobe (seconds)."""
    ffmpeg = detect_ffmpeg().ffmpeg
    if not ffmpeg:
        return None
    ffprobe = str(Path(ffmpeg).with_name("ffprobe"))
    if not Path(ffprobe).exists():
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            universal_newlines=True).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None
