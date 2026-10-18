"""Command line interface.

    python -m code2video env
    python -m code2video render --html animation.html --out video.mp4 \
        [--fps 30] [--frames 180] [--size 1280x720] [--encoder auto] \
        [--crf 18] [--keep-frames] [--report report.json]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from . import __version__
from .capture import CaptureConfig, render_frames
from .compose import compose, probe_duration
from .detect import describe_env, env_report


def _parse_size(text: str) -> Tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError(f"expected WxH (e.g. 1280x720), got {text!r}")
    return int(m.group(1)), int(m.group(2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code2video",
        description="Render a frame-by-frame HTML animation to MP4 via "
                    "headless Chrome + ffmpeg (GPU auto-detected).")
    parser.add_argument("--version", action="version",
                        version=f"code2video {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("env", help="probe GPU / browser / ffmpeg / encoders")
    p_env.add_argument("--json", action="store_true", help="machine-readable output")

    p_render = sub.add_parser("render", help="render an HTML animation to video")
    p_render.add_argument("--html", required=True, help="self-contained HTML animation")
    p_render.add_argument("--out", required=True, help="output video path (.mp4)")
    p_render.add_argument("--fps", type=int, default=None,
                          help="override VIDEO_SPEC.fps")
    p_render.add_argument("--frames", type=int, default=None,
                          help="override VIDEO_SPEC.frames (use small values for previews)")
    p_render.add_argument("--size", type=_parse_size, default=None,
                          help="WxH, overrides VIDEO_SPEC.width/height")
    p_render.add_argument("--frames-dir", default=None,
                          help="where to keep PNG frames (default: <out>_frames/)")
    p_render.add_argument("--encoder", default="auto",
                          choices=["auto", "h264_nvenc", "h264_qsv", "h264_amf",
                                   "h264_videotoolbox", "h264_vaapi", "libx264"],
                          help="video encoder (default: auto = probe each "
                               "candidate with a test encode and keep the "
                               "best one that works)")
    p_render.add_argument("--crf", type=int, default=18,
                          help="quality, 0-51 x264 scale (default: 18)")
    p_render.add_argument("--preset", default="medium",
                          help="x264 preset: ultrafast..veryslow (default: medium)")
    p_render.add_argument("--browser", default=None,
                          help="path to a Chromium browser (default: auto-detect)")
    p_render.add_argument("--no-gpu", action="store_true",
                          help="force software rendering (SwiftShader)")
    p_render.add_argument("--keep-frames", action="store_true",
                          help="do not delete the PNG frames after composing")
    p_render.add_argument("--report", default=None,
                          help="write a JSON render report to this path")
    p_render.add_argument("--startup-timeout", type=float, default=30.0,
                          help="seconds to wait for the browser to start")
    return parser


def cmd_env(args: argparse.Namespace) -> int:
    report = env_report()
    if args.json:
        print(report.to_json())
    else:
        print(describe_env(report))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    t0 = time.time()
    html = Path(args.html)
    if not html.exists():
        print(f"error: HTML not found: {html}", file=sys.stderr)
        return 2
    out = Path(args.out)
    frames_dir = Path(args.frames_dir) if args.frames_dir \
        else out.with_name(out.stem + "_frames")

    cfg = CaptureConfig(
        html=html, out_dir=frames_dir, fps=args.fps, frames=args.frames,
        width=args.size[0] if args.size else None,
        height=args.size[1] if args.size else None,
        browser=args.browser, use_gpu=not args.no_gpu,
        startup_timeout=args.startup_timeout)

    print(f"[1/2] capturing frames from {html} ...")
    capture = render_frames(cfg)

    print(f"[2/2] composing video ...")
    result = compose(frames_dir, out, fps=capture.fps,
                     encoder=None if args.encoder == "auto" else args.encoder,
                     crf=args.crf, preset=args.preset)

    if not args.keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)

    duration = probe_duration(out)
    report = {
        "tool": "code2video", "version": __version__,
        "input_html": str(html.resolve()),
        "output": str(out.resolve()),
        "frames": capture.frames, "fps": capture.fps,
        "width": capture.width, "height": capture.height,
        "duration_seconds": duration if duration is not None else capture.frames / capture.fps,
        "encoder": result.encoder, "ffmpeg": result.ffmpeg,
        "ffmpeg_source": result.ffmpeg_source,
        "renderer": capture.renderer,
        "gpu_available": capture.gpu_available,
        "size_bytes": result.size_bytes,
        "elapsed_seconds": round(time.time() - t0, 2),
        "warnings": capture.warnings,
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    mb = result.size_bytes / 1024 / 1024
    print(f"done: {out} ({mb:.2f} MB, {report['duration_seconds']:.1f}s, "
          f"{result.encoder}, renderer={capture.renderer.get('verdict', 'unknown')})")
    for warning in capture.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "env":
        return cmd_env(args)
    if args.command == "render":
        return cmd_render(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
