"""Environment detection for code2video.

Best-effort, read-only probing of everything the pipeline needs:

* GPU presence and vendor        (nvidia-smi / Win32_VideoController / system_profiler / lspci)
* a Chromium-family browser      (Chrome / Chromium / Edge / Brave, common install paths + PATH)
* ffmpeg and its H.264 encoders  (system ffmpeg preferred, imageio-ffmpeg as fallback)

Nothing is ever installed or downloaded here; callers turn the report into
launch flags / encoder choices.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# H.264 encoders we know how to drive, in generic preference order.
# The GPU/OS-specific preference is applied in choose_encoder().
KNOWN_ENCODERS = [
    "h264_nvenc",         # NVIDIA (Windows / Linux)
    "h264_qsv",           # Intel Quick Sync (Windows / Linux)
    "h264_amf",           # AMD Advanced Media Framework (Windows)
    "h264_videotoolbox",  # Apple
    "h264_vaapi",         # VAAPI (Linux, AMD / Intel)
    "libx264",            # CPU fallback, always acceptable
]

# Preferred encoders per GPU vendor. libx264 stays as the universal fallback.
_VENDOR_ENCODERS = {
    "nvidia": ["h264_nvenc"],
    "intel": ["h264_qsv"],
    "amd": ["h264_amf"],
    "apple": ["h264_videotoolbox"],
}

# Where Chromium-family browsers usually live, per OS.
_BROWSER_PATHS: Dict[str, List[tuple]] = {
    "Windows": [
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
        (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
        (os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"), "chrome"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "edge"),
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "edge"),
        (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", "brave"),
    ],
    "Darwin": [
        ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
        ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "edge"),
        ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "brave"),
        ("/Applications/Chromium.app/Contents/MacOS/Chromium", "chromium"),
    ],
    "Linux": [
        ("/usr/bin/google-chrome", "chrome"),
        ("/usr/bin/google-chrome-stable", "chrome"),
        ("/opt/google/chrome/chrome", "chrome"),
        ("/usr/bin/chromium", "chromium"),
        ("/usr/bin/chromium-browser", "chromium"),
        ("/snap/bin/chromium", "chromium"),
        ("/var/lib/flatpak/exports/bin/org.chromium.Chromium", "chromium"),
        ("/usr/bin/microsoft-edge", "edge"),
        ("/usr/bin/microsoft-edge-stable", "edge"),
    ],
}

# Command names to try on PATH (all platforms).
_BROWSER_COMMANDS = [
    "google-chrome", "google-chrome-stable", "google-chrome-beta",
    "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable", "msedge",
    "brave-browser",
]


@dataclass
class GPUInfo:
    available: bool = False
    vendor: Optional[str] = None          # nvidia | intel | amd | apple | unknown
    name: Optional[str] = None
    driver: Optional[str] = None
    method: Optional[str] = None          # how it was detected


@dataclass
class BrowserInfo:
    path: Optional[str] = None
    name: Optional[str] = None            # chrome | chromium | edge | brave
    version: Optional[str] = None


@dataclass
class EncoderInfo:
    ffmpeg: Optional[str] = None
    source: Optional[str] = None          # system | imageio-ffmpeg
    available: List[str] = field(default_factory=list)
    chosen: Optional[str] = None


@dataclass
class EnvReport:
    platform: str
    gpu: GPUInfo
    browser: BrowserInfo
    encoder: EncoderInfo

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _run(cmd: List[str], timeout: float = 15.0) -> str:
    """Run a command, return stdout ('' on any failure). Never raises.

    Output is decoded as UTF-8 with replacement: browser --version output is
    localized (e.g. GBK Chinese on Windows) and must not crash the reader.
    """
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return proc.stdout or ""
    except Exception:
        return ""


def _vendor_from_name(name: str) -> Optional[str]:
    low = name.lower()
    if any(k in low for k in ("nvidia", "geforce", "quadro", "tesla")):
        return "nvidia"
    if any(k in low for k in ("amd", "radeon")):
        return "amd"
    if "intel" in low:
        return "intel"
    if "apple" in low:
        return "apple"
    return None


# --------------------------------------------------------------------------
# GPU
# --------------------------------------------------------------------------

def detect_gpu() -> GPUInfo:
    """Detect a discrete/integrated GPU, best effort, cross-platform."""
    forced = os.environ.get("CODE2VIDEO_FORCE_GPU", "").strip().lower()
    if forced in ("0", "false", "no", "off"):
        return GPUInfo(available=False, method="env-override")
    if forced in ("1", "true", "yes", "on"):
        return GPUInfo(available=True, vendor="unknown",
                       name="forced via CODE2VIDEO_FORCE_GPU", method="env-override")

    # NVIDIA: most precise, includes driver version.
    nv = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if nv.strip():
        first = nv.strip().splitlines()[0]
        name, _, driver = first.partition(",")
        if name.strip():
            return GPUInfo(True, "nvidia", name.strip(),
                           driver.strip() or None, "nvidia-smi")

    system = platform.system()
    if system == "Windows":
        ps = _run(["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                  timeout=20)
        for line in ps.splitlines():
            line = line.strip()
            if not line:
                continue
            vendor = _vendor_from_name(line)
            if vendor:
                return GPUInfo(True, vendor, line, None, "win32_VideoController")
        if ps.strip():
            return GPUInfo(True, None, ps.strip().splitlines()[0].strip(), None,
                           "win32_VideoController")
        return GPUInfo()

    if system == "Darwin":
        out = _run(["system_profiler", "SPDisplaysDataType"])
        if out.strip():
            m = re.search(r"Chipset Model:\s*(.+)", out)
            name = m.group(1).strip() if m else out.strip().splitlines()[0]
            vendor = _vendor_from_name(out) or "apple"
            return GPUInfo(True, vendor, name, None, "system_profiler")
        return GPUInfo()

    # Linux / other: lspci is usually present on desktops.
    lspci = _run(["lspci"])
    for line in lspci.splitlines():
        if re.search(r"\b(VGA|3D|Display)\b", line):
            vendor = _vendor_from_name(line)
            name = line.split(":", 1)[-1].strip() if ":" in line else line.strip()
            if vendor or "controller" in line.lower():
                return GPUInfo(True, vendor, name, None, "lspci")
    return GPUInfo()


def chrome_render_flags(gpu: GPUInfo) -> List[str]:
    """Chrome flags that maximize the chance of GPU-accelerated compositing.

    When a GPU is present we ask ANGLE for the platform default (D3D11 on
    Windows, Metal on macOS, GL on Linux). Without a GPU we fall back to
    SwiftShader so rendering still works everywhere.
    """
    if not gpu.available:
        return ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    return ["--use-angle=gl", "--enable-gpu-rasterization", "--ignore-gpu-blocklist"]


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------

def _guess_browser_name(filename: str) -> str:
    low = filename.lower()
    if "msedge" in low or "edge" in low:
        return "edge"
    if "brave" in low:
        return "brave"
    if "chromium" in low:
        return "chromium"
    return "chrome"


def _browser_version(path: Path) -> Optional[str]:
    out = _run([str(path), "--version"], timeout=20)
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else None


def detect_browser() -> BrowserInfo:
    """Find a usable Chromium-family browser. Honors $CODE2VIDEO_BROWSER."""
    override = os.environ.get("CODE2VIDEO_BROWSER", "").strip()
    if override:
        candidate = Path(override)
        if candidate.exists():
            return BrowserInfo(str(candidate), _guess_browser_name(candidate.name),
                               _browser_version(candidate))
        found = shutil.which(override)
        if found:
            p = Path(found)
            return BrowserInfo(found, _guess_browser_name(p.name), _browser_version(p))
        return BrowserInfo()  # invalid override -> report nothing, caller errors out

    for path, name in _BROWSER_PATHS.get(platform.system(), []):
        if path and Path(path).exists():
            p = Path(path)
            return BrowserInfo(str(p), name, _browser_version(p))

    for name in _BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            p = Path(found)
            return BrowserInfo(found, _guess_browser_name(p.name), _browser_version(p))

    return BrowserInfo()


def browser_supports_new_headless(version: Optional[str]) -> bool:
    """--headless=new landed in Chrome/Edge 112."""
    if not version:
        return True  # assume modern
    try:
        return int(version.split(".")[0]) >= 112
    except (ValueError, IndexError):
        return True


# --------------------------------------------------------------------------
# ffmpeg + encoders
# --------------------------------------------------------------------------

def available_encoders(ffmpeg: str) -> List[str]:
    """Which of KNOWN_ENCODERS this ffmpeg build actually has."""
    out = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    present = {e for e in KNOWN_ENCODERS if re.search(rf"\b{re.escape(e)}\b", out)}
    return [e for e in KNOWN_ENCODERS if e in present]


def detect_ffmpeg() -> EncoderInfo:
    """System ffmpeg first (hardware encoders likely), imageio-ffmpeg fallback."""
    info = EncoderInfo()
    found = shutil.which("ffmpeg")
    if found:
        info.ffmpeg, info.source = found, "system"
    else:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore
            exe = get_ffmpeg_exe()
            if exe and Path(exe).exists():
                info.ffmpeg, info.source = exe, "imageio-ffmpeg"
        except Exception:
            pass
    if info.ffmpeg:
        info.available = available_encoders(info.ffmpeg)
    return info


def choose_encoder(encoder_info: EncoderInfo, gpu: GPUInfo,
                   forced: Optional[str] = None) -> Optional[str]:
    """Pick the best encoder: GPU/OS-specific hardware first, libx264 last."""
    if forced and forced != "auto":
        return forced if forced in encoder_info.available else None
    preferred = list(_VENDOR_ENCODERS.get(gpu.vendor or "", []))
    if platform.system() == "Linux" and gpu.vendor in ("amd", "intel"):
        # VAAPI is the reliable hardware path for AMD/Intel on Linux
        preferred = ["h264_vaapi"] + [e for e in preferred if e != "h264_vaapi"]
    order = preferred + [e for e in KNOWN_ENCODERS if e not in preferred]
    for enc in order:
        if enc in encoder_info.available:
            return enc
    return None


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def env_report() -> EnvReport:
    gpu = detect_gpu()
    browser = detect_browser()
    encoder = detect_ffmpeg()
    encoder.chosen = choose_encoder(encoder, gpu)
    return EnvReport(platform=platform.platform(), gpu=gpu,
                     browser=browser, encoder=encoder)


def describe_env(report: EnvReport) -> str:
    g = report.gpu
    b = report.browser
    e = report.encoder
    lines = [
        f"platform : {report.platform}",
        f"gpu      : {'available' if g.available else 'not detected'}"
        + (f" ({g.vendor}, {g.name}" + (f", driver {g.driver}" if g.driver else "") + ")"
           if g.available else "")
        + (f" [via {g.method}]" if g.method else ""),
        f"browser  : {b.path or 'NOT FOUND'}"
        + (f" ({b.name}" + (f" {b.version}" if b.version else "") + ")" if b.path else ""),
        f"ffmpeg   : {e.ffmpeg or 'NOT FOUND'}"
        + (f" [{e.source}]" if e.ffmpeg else ""),
        f"encoders : {', '.join(e.available) or 'none'}",
        f"chosen   : {e.chosen or 'none'}",
    ]
    if not b.path:
        lines.append("hint     : set CODE2VIDEO_BROWSER=/path/to/chrome")
    if not e.ffmpeg:
        lines.append("hint     : install ffmpeg, or: pip install imageio-ffmpeg")
    return "\n".join(lines)
