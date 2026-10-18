"""Unit tests for ffmpeg command construction."""
from pathlib import Path

import pytest

from code2video import compose, detect
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


def test_build_command_vaapi_filter_chain():
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg", frames_dir=Path("/tmp/frames"),
        output=Path("/tmp/out.mp4"), fps=24, encoder="h264_vaapi")
    vf = cmd[cmd.index("-vf") + 1]
    assert "format=nv12|vaapi" in vf and "hwupload" in vf
    assert "trunc(iw/2)" in vf


def test_probe_encoder_builds_synthetic_command(monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(compose.subprocess, "run", fake_run)
    assert compose.probe_encoder("ffmpeg", "libx264") is True
    assert len(calls) == 1
    cmd = calls[0]
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "lavfi"
    assert "color=c=black:s=256x256" in cmd[cmd.index("-i") + 1]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[-2:] == ["-f", "null"] or "null" in cmd


def test_probe_encoder_failure_returns_false(monkeypatch):
    class FakeProc:
        returncode = 1
        stderr = "Cannot load nvcuda.dll"

    monkeypatch.setattr(compose.subprocess, "run",
                        lambda cmd, **kwargs: FakeProc())
    assert compose.probe_encoder("ffmpeg", "h264_nvenc") is False


def test_probe_encoder_timeout_returns_false(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise TimeoutError("ffmpeg hung")

    monkeypatch.setattr(compose.subprocess, "run", fake_run)
    assert compose.probe_encoder("ffmpeg", "h264_nvenc") is False


def test_resolve_encoder_probes_until_one_works(monkeypatch):
    info = detect.EncoderInfo(
        ffmpeg="ffmpeg",
        available=["h264_nvenc", "h264_qsv", "libx264"])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    tried = []

    def fake_probe(ffmpeg, encoder, timeout=30.0):
        tried.append(encoder)
        return False  # no hardware encoder really works here

    monkeypatch.setattr(compose, "probe_encoder", fake_probe)
    enc = compose.resolve_encoder(info, gpu, log=lambda *a: None)
    assert enc == "libx264"
    # hardware candidates are probed; libx264 is trusted without probing
    assert tried == ["h264_nvenc", "h264_qsv"]


def test_resolve_encoder_prefers_working_hardware(monkeypatch):
    info = detect.EncoderInfo(ffmpeg="ffmpeg",
                              available=["h264_nvenc", "libx264"])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    monkeypatch.setattr(compose, "probe_encoder",
                        lambda ffmpeg, encoder, timeout=30.0: True)
    assert compose.resolve_encoder(info, gpu, log=lambda *a: None) == "h264_nvenc"


def test_resolve_encoder_caches_choice(monkeypatch):
    info = detect.EncoderInfo(ffmpeg="ffmpeg",
                              available=["h264_nvenc", "libx264"])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    calls = []

    def fake_probe(ffmpeg, encoder, timeout=30.0):
        calls.append(encoder)
        return True

    monkeypatch.setattr(compose, "probe_encoder", fake_probe)
    assert compose.resolve_encoder(info, gpu, log=lambda *a: None) == "h264_nvenc"
    assert compose.resolve_encoder(info, gpu, log=lambda *a: None) == "h264_nvenc"
    assert calls == ["h264_nvenc"]  # second call reuses the cached choice


def test_resolve_encoder_forced_skips_probing(monkeypatch):
    info = detect.EncoderInfo(ffmpeg="ffmpeg",
                              available=["h264_nvenc", "libx264"])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")

    def fail_probe(*args, **kwargs):
        pytest.fail("forced encoder must not be probed")

    monkeypatch.setattr(compose, "probe_encoder", fail_probe)
    assert compose.resolve_encoder(info, gpu, forced="libx264") == "libx264"


def test_resolve_encoder_forced_missing_raises():
    info = detect.EncoderInfo(ffmpeg="ffmpeg", available=["libx264"])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    with pytest.raises(RuntimeError, match="not available"):
        compose.resolve_encoder(info, gpu, forced="h264_nvenc")


def test_resolve_encoder_nothing_available_falls_back_to_libx264(monkeypatch):
    info = detect.EncoderInfo(ffmpeg="ffmpeg", available=[])
    gpu = detect.GPUInfo(available=False)

    def fail_probe(*args, **kwargs):
        pytest.fail("no candidates means no probing")

    monkeypatch.setattr(compose, "probe_encoder", fail_probe)
    assert compose.resolve_encoder(info, gpu, log=lambda *a: None) == "libx264"
