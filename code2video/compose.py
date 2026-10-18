"""Compose captured PNG frames into a video with ffmpeg.

Encoder selection is automatic and GPU-aware:
    NVIDIA -> h264_nvenc, Intel -> h264_qsv, AMD -> h264_amf,
    Apple  -> h264_videotoolbox, otherwise -> libx264 (CPU).

Because ``ffmpeg -encoders`` only lists codecs *compiled into* the binary,
every candidate is validated with a one-frame synthetic encode
(:func:`probe_encoder`) before it is trusted — a full build advertises
h264_nvenc even on machines without an NVIDIA GPU.

ffmpeg lookup: system ffmpeg first (hardware encoders are usually only in a
full build), then the ``imageio-ffmpeg`` pip package as a guaranteed fallback.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .detect import (EncoderInfo, GPUInfo, detect_ffmpeg,
                     encoder_candidates)

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


def _video_filters(encoder: str) -> str:
    """Filter chain for `encoder` (shared by the real render and the probe)."""
    vf = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]  # even dimensions, always
    if encoder == "h264_vaapi":
        vf += ["format=nv12|vaapi", "hwupload"]
    return ",".join(vf)


def build_ffmpeg_command(ffmpeg: str, frames_dir: Path, output: Path,
                         fps: int, encoder: str, crf: int = 18,
                         preset: str = "medium",
                         pattern: str = "frame_%05d.png") -> List[str]:
    """Build the full ffmpeg command (pure function, easy to test)."""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", str(fps),
        "-i", str(Path(frames_dir) / pattern),
        "-c:v", encoder,
    ]
    cmd.extend(encoder_args(encoder, crf, preset))
    cmd.extend([
        "-vf", _video_filters(encoder),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-r", str(fps),
        str(output),
    ])
    return cmd


def probe_encoder(ffmpeg: str, encoder: str, timeout: float = 30.0) -> bool:
    """Check that `encoder` can actually encode on this machine.

    ``ffmpeg -encoders`` lists compiled-in codecs, not usable ones: a full
    build advertises h264_nvenc without an NVIDIA GPU, h264_vaapi without a
    DRI node, and so on. A one-frame synthetic encode settles the question.
    """
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        # 256x256: small enough to be instant, large enough for NVENC's
        # minimum frame size (144x144) and every other hardware encoder.
        "-f", "lavfi", "-i", "color=c=black:s=256x256:r=10:d=0.2",
        "-frames:v", "1", "-c:v", encoder,
    ]
    cmd.extend(encoder_args(encoder, crf=28, preset="ultrafast"))
    cmd.extend(["-vf", _video_filters(encoder), "-f", "null", "-"])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False


def resolve_encoder(info: EncoderInfo, gpu: GPUInfo, forced: str = "auto",
                    crf: int = 18, log=print) -> str:
    """Pick an encoder that really works on this machine, hardware preferred.

    Candidates come from :func:`detect.encoder_candidates` (GPU/OS aware) and
    are validated with :func:`probe_encoder` until one succeeds. libx264 is
    the guaranteed last resort — if even that fails, ffmpeg itself is broken
    and :func:`compose` will surface the real error.
    """
    if forced and forced != "auto":
        if forced not in info.available:
            raise RuntimeError(
                f"encoder '{forced}' is not available in {info.ffmpeg} "
                f"(available: {', '.join(info.available) or 'none'})")
        return forced
    if info.chosen and info.chosen in info.available:
        return info.chosen  # already resolved in this process
    candidates = encoder_candidates(info, gpu) or ["libx264"]
    for enc in candidates:
        # libx264 is the trusted CPU fallback: never probe it. If even that
        # fails, ffmpeg itself is broken and compose() surfaces the real error.
        if enc == "libx264" or probe_encoder(info.ffmpeg, enc):
            if enc != "libx264":
                log(f"  encoder probe: {enc} works here")
            info.chosen = enc
            return enc
        log(f"  encoder probe: {enc} unusable here, trying next")
    info.chosen = "libx264"
    return "libx264"


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
    enc = resolve_encoder(ff, gpu, forced=encoder or "auto", crf=crf, log=log)
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
