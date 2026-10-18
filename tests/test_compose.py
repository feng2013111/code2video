"""Unit tests for ffmpeg command construction."""
from pathlib import Path

from code2video.compose import build_ffmpeg_command, encoder_args


def test_encoder_args_nvenc():
    args = encoder_args("h264_nvenc", crf=20, preset="medium")
    assert "-rc" in args and "vbr" in args
    assert args[args.index("-cq") + 1] == "20"


def test_encoder_args_videotoolbox_quality_scale():
    args = encoder_args("h264_videotoolbox", crf=18, preset="medium")
    q = int(args[args.index("-q:v") + 1])
    assert 1 <= q <= 100
    # lower crf (better quality) -> higher q
    q_better = int(encoder_args("h264_videotoolbox", crf=10, preset="medium")
                   [encoder_args("h264_videotoolbox", crf=10, preset="medium").index("-q:v") + 1])
    assert q_better > q


def test_encoder_args_libx264():
    args = encoder_args("libx264", crf=18, preset="slow")
    assert args == ["-crf", "18", "-preset", "slow"]


def test_build_command_basics():
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg", frames_dir=Path("/tmp/frames"),
        output=Path("/tmp/out.mp4"), fps=30, encoder="libx264")
    assert cmd[0] == "ffmpeg"
    assert "-framerate" in cmd and cmd[cmd.index("-framerate") + 1] == "30"
    assert "-i" in cmd
    assert "frame_%05d.png" in cmd[cmd.index("-i") + 1]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "-pix_fmt" in cmd and "yuv420p" in cmd
    assert "-movflags" in cmd and "+faststart" in cmd
    # NOTE: compare via Path(), because WindowsPath('/tmp/x') == '\\tmp\\x'
    assert cmd[-1] == str(Path("/tmp/out.mp4"))
    # even-dimension safeguard is always present
    assert any("trunc(iw/2)" in part for part in cmd)


def test_build_command_nvenc():
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg", frames_dir=Path("/tmp/frames"),
        output=Path("/tmp/out.mp4"), fps=24, encoder="h264_nvenc", crf=23)
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert "-preset" in cmd and "p4" in cmd
