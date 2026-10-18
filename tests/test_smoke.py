"""Optional end-to-end smoke test.

Renders 3 frames of the template animation and composes a tiny MP4.
Skipped by default because it needs a real browser + ffmpeg:

    CODE2VIDEO_SMOKE=1 pytest tests/test_smoke.py -s
"""
import os
import shutil
from pathlib import Path

import pytest

from code2video.capture import CaptureConfig, render_frames
from code2video.compose import compose

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "templates" / "animation_template.html"

pytestmark = pytest.mark.skipif(
    not os.environ.get("CODE2VIDEO_SMOKE"),
    reason="set CODE2VIDEO_SMOKE=1 to run the end-to-end smoke test")


def test_smoke_render_three_frames(tmp_path):
    frames_dir = tmp_path / "frames"
    cfg = CaptureConfig(html=TEMPLATE, out_dir=frames_dir,
                        fps=30, frames=3, width=640, height=360)
    result = render_frames(cfg, log=lambda *a, **k: None)
    assert result.frames == 3
    pngs = sorted(frames_dir.glob("frame_*.png"))
    assert len(pngs) == 3
    for png in pngs:
        assert png.stat().st_size > 1000, "screenshot looks empty"

    out = tmp_path / "smoke.mp4"
    composed = compose(frames_dir, out, fps=result.fps,
                       encoder="libx264", crf=30)
    assert out.exists() and composed.size_bytes > 0
    assert composed.encoder in ("libx264", "h264_nvenc", "h264_qsv",
                                "h264_amf", "h264_videotoolbox", "h264_vaapi")
    shutil.rmtree(frames_dir, ignore_errors=True)
