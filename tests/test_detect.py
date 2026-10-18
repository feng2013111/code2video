"""Unit tests for environment detection and encoder selection."""
import os
from pathlib import Path

import pytest

from code2video import detect


def test_choose_encoder_prefers_nvenc_for_nvidia():
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    enc = detect.EncoderInfo(available=["h264_nvenc", "h264_qsv", "libx264"])
    assert detect.choose_encoder(enc, gpu) == "h264_nvenc"


def test_choose_encoder_vendor_specific():
    gpu = detect.GPUInfo(available=True, vendor="intel")
    enc = detect.EncoderInfo(available=["h264_nvenc", "h264_qsv", "libx264"])
    assert detect.choose_encoder(enc, gpu) == "h264_qsv"

    gpu = detect.GPUInfo(available=True, vendor="apple")
    enc = detect.EncoderInfo(available=["h264_nvenc", "h264_videotoolbox", "libx264"])
    assert detect.choose_encoder(enc, gpu) == "h264_videotoolbox"


def test_choose_encoder_falls_back_to_libx264():
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    enc = detect.EncoderInfo(available=["libx264"])
    assert detect.choose_encoder(enc, gpu) == "libx264"


def test_choose_encoder_none_available():
    gpu = detect.GPUInfo(available=False)
    assert detect.choose_encoder(detect.EncoderInfo(available=[]), gpu) is None


def test_choose_encoder_forced_must_exist():
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    enc = detect.EncoderInfo(available=["h264_nvenc", "libx264"])
    assert detect.choose_encoder(enc, gpu, forced="libx264") == "libx264"
    assert detect.choose_encoder(enc, gpu, forced="h264_qsv") is None


def test_encoder_candidates_skips_vaapi_without_render_node(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Linux")
    monkeypatch.delenv("CODE2VIDEO_VAAPI_DEVICE", raising=False)
    monkeypatch.setattr(detect, "_dri_render_nodes", lambda: [])
    gpu = detect.GPUInfo(available=True, vendor="amd")
    enc = detect.EncoderInfo(available=["h264_vaapi", "libx264"])
    assert detect.encoder_candidates(enc, gpu) == ["libx264"]
    assert detect.choose_encoder(enc, gpu) == "libx264"


def test_encoder_candidates_uses_vaapi_with_render_node(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Linux")
    monkeypatch.delenv("CODE2VIDEO_VAAPI_DEVICE", raising=False)
    monkeypatch.setattr(detect, "_dri_render_nodes",
                        lambda: ["/dev/dri/renderD128"])
    gpu = detect.GPUInfo(available=True, vendor="amd")
    enc = detect.EncoderInfo(available=["h264_vaapi", "h264_qsv", "libx264"])
    assert detect.encoder_candidates(enc, gpu) == \
        ["h264_vaapi", "h264_qsv", "libx264"]


def test_encoder_candidates_vaapi_env_override(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Linux")
    monkeypatch.setenv("CODE2VIDEO_VAAPI_DEVICE", "/dev/dri/renderD129")
    monkeypatch.setattr(detect, "_dri_render_nodes", lambda: [])
    gpu = detect.GPUInfo(available=True, vendor="amd")
    enc = detect.EncoderInfo(available=["h264_vaapi", "libx264"])
    assert detect.encoder_candidates(enc, gpu)[0] == "h264_vaapi"


def test_encoder_candidates_vaapi_never_off_linux(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Windows")
    monkeypatch.delenv("CODE2VIDEO_VAAPI_DEVICE", raising=False)
    gpu = detect.GPUInfo(available=True, vendor="amd")
    enc = detect.EncoderInfo(available=["h264_vaapi", "h264_amf", "libx264"])
    # amd on Windows prefers h264_amf; vaapi must not be a candidate there
    assert detect.encoder_candidates(enc, gpu) == ["h264_amf", "libx264"]


def test_encoder_candidates_libx264_always_last(monkeypatch):
    monkeypatch.setattr(detect.platform, "system", lambda: "Linux")
    monkeypatch.delenv("CODE2VIDEO_VAAPI_DEVICE", raising=False)
    monkeypatch.setattr(detect, "_dri_render_nodes", lambda: [])
    gpu = detect.GPUInfo(available=True, vendor="nvidia")
    enc = detect.EncoderInfo(
        available=["libx264", "h264_qsv", "h264_vaapi", "h264_nvenc"])
    assert detect.encoder_candidates(enc, gpu) == \
        ["h264_nvenc", "h264_qsv", "libx264"]


def test_render_flags_gpu_vs_software():
    hw = detect.chrome_render_flags(detect.GPUInfo(available=True, vendor="nvidia"))
    assert "--enable-gpu-rasterization" in hw
    assert "--use-angle=gl" in hw

    sw = detect.chrome_render_flags(detect.GPUInfo(available=False))
    assert "--use-angle=swiftshader" in sw
    assert "--enable-unsafe-swiftshader" in sw
    assert "--enable-gpu-rasterization" not in sw


def test_detect_browser_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "fake-chrome"
    fake.write_text("not a real browser")
    monkeypatch.setenv("CODE2VIDEO_BROWSER", str(fake))
    info = detect.detect_browser()
    assert info.path == str(fake)
    assert info.name == "chrome"


def test_detect_browser_no_candidates(monkeypatch):
    monkeypatch.delenv("CODE2VIDEO_BROWSER", raising=False)
    monkeypatch.setattr(detect.platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(detect.shutil, "which", lambda name: None)
    assert detect.detect_browser().path is None


def test_force_gpu_env_override(monkeypatch):
    monkeypatch.setenv("CODE2VIDEO_FORCE_GPU", "0")
    assert detect.detect_gpu().available is False
    monkeypatch.setenv("CODE2VIDEO_FORCE_GPU", "1")
    gpu = detect.detect_gpu()
    assert gpu.available is True and gpu.method == "env-override"


def test_available_encoders_parses_ffmpeg_output(monkeypatch):
    sample = (
        " V....D libx264              libx264 H.264\n"
        " V....D h264_nvenc           NVIDIA NVENC\n"
        " V....D h264_qsv             Intel QSV\n"
    )
    monkeypatch.setattr(detect, "_run",
                        lambda cmd, timeout=15.0: sample)
    got = detect.available_encoders("ffmpeg")
    assert got == ["h264_nvenc", "h264_qsv", "libx264"]


def test_browser_supports_new_headless():
    assert detect.browser_supports_new_headless("130.0.0.0") is True
    assert detect.browser_supports_new_headless("111.0.0.0") is False
    assert detect.browser_supports_new_headless(None) is True


def test_env_report_is_json_serializable():
    import json
    report = detect.env_report()
    payload = json.loads(report.to_json())
    assert set(payload) == {"platform", "gpu", "browser", "encoder"}
    assert isinstance(payload["gpu"]["available"], bool)
