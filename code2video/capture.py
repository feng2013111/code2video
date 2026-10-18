"""Frame capture: drive a headless Chromium to render each frame.

The HTML contract (see templates/animation_template.html):

    window.VIDEO_SPEC = { fps, frames, width, height }   // optional metadata
    window.__renderFrame(index, t)                       // REQUIRED, must draw
                                                         // frame `index` (t = seconds)
                                                         // fully deterministically

GPU handling: we launch Chrome with GPU-accelerated compositing flags when a
GPU is detected, then *verify* by probing chrome://gpu. If the verdict is
"software only" while a GPU exists, we relaunch with an alternative ANGLE
backend before giving up and staying on software.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .cdp import CDPSession, CDPError
from .detect import (browser_supports_new_headless, chrome_render_flags,
                     detect_browser, detect_gpu)


@dataclass
class CaptureConfig:
    html: Path
    out_dir: Path
    fps: Optional[int] = None            # None -> VIDEO_SPEC.fps or 30
    frames: Optional[int] = None         # None -> VIDEO_SPEC.frames or 150
    width: Optional[int] = None          # None -> VIDEO_SPEC.width or 1280
    height: Optional[int] = None         # None -> VIDEO_SPEC.height or 720
    browser: Optional[str] = None        # None -> auto-detect (or $CODE2VIDEO_BROWSER)
    use_gpu: bool = True
    startup_timeout: float = 30.0
    frame_timeout: float = 60.0


@dataclass
class CaptureResult:
    frames_dir: Path
    frames: int
    fps: int
    width: int
    height: int
    browser: str
    renderer: dict                       # chrome://gpu probe verdict
    gpu_available: bool
    warnings: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


# --------------------------------------------------------------------------
# browser process handling
# --------------------------------------------------------------------------

def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _http_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _launch_browser(browser: str, port: int, width: int, height: int,
                    extra_flags: List[str], profile_dir: str,
                    headless_mode: str) -> subprocess.Popen:
    args = [
        browser,
        "--headless=new" if headless_mode == "new" else "--headless",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--window-size={width},{height}",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--force-color-profile=srgb",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-features=Translate,BackForwardCache,MediaRouter",
        "--mute-audio",
        f"--user-data-dir={profile_dir}",
        "about:blank",
    ]
    args.extend(extra_flags)
    if platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_devtools(proc: subprocess.Popen, port: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"browser exited immediately (code {proc.returncode}); "
                "is another Chrome instance locking the profile?")
        try:
            return _http_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"browser did not expose DevTools on port {port} "
                       f"within {timeout:.0f}s")


class _BrowserSession:
    """A launched browser plus an attached page session, ready for CDP calls."""

    def __init__(self, proc, cdp: CDPSession, session_id: str,
                 flags: List[str], headless_mode: str):
        self.proc = proc
        self.cdp = cdp
        self.session_id = session_id
        self.flags = flags
        self.headless_mode = headless_mode
        self.renderer: dict = {"probed": False}

    def close(self) -> None:
        try:
            self.cdp.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _open_session(browser: str, width: int, height: int, flags: List[str],
                  headless_mode: str, startup_timeout: float) -> _BrowserSession:
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="code2video-profile-")
    proc = _launch_browser(browser, port, width, height, flags, profile, headless_mode)
    try:
        info = _wait_devtools(proc, port, startup_timeout)
        cdp = CDPSession(info["webSocketDebuggerUrl"])
        try:
            target = cdp.call("Target.createTarget", {"url": "about:blank"})
            attached = cdp.call("Target.attachToTarget",
                                {"targetId": target["targetId"], "flatten": True})
            session_id = attached["sessionId"]
            cdp.call("Page.enable", session_id=session_id)
            cdp.call("Runtime.enable", session_id=session_id)
            return _BrowserSession(proc, cdp, session_id, flags, headless_mode)
        except Exception:
            cdp.close()
            raise
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        raise


# --------------------------------------------------------------------------
# GPU probe
# --------------------------------------------------------------------------

_WEBGL_PROBE_JS = """
(function () {
  try {
    var c = document.createElement('canvas');
    var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return { probed: false, reason: 'no webgl context' };
    var ext = gl.getExtension('WEBGL_debug_renderer_info');
    var renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
                       : gl.getParameter(gl.RENDERER);
    var vendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)
                     : gl.getParameter(gl.VENDOR);
    return {
      probed: true,
      vendor: String(vendor),
      renderer: String(renderer),
      verdict: /swiftshader|software|llvmpipe|basic render/i.test(String(renderer))
               ? 'software' : 'hardware'
    };
  } catch (e) {
    return { probed: false, reason: String(e) };
  }
})()
"""


def _probe_renderer(session: _BrowserSession) -> dict:
    """Ask the page for its WebGL renderer string.

    Works on any page (about:blank is fine) and reveals directly whether
    Chrome is compositing on the GPU or fell back to SwiftShader / llvmpipe.
    More reliable than navigating to chrome://gpu, which some Chrome builds
    refuse to render in headless mode.
    """
    try:
        result = session.cdp.evaluate(_WEBGL_PROBE_JS,
                                      session_id=session.session_id)
        return result if isinstance(result, dict) else {"probed": False}
    except Exception as exc:  # probing is best-effort, never fatal
        return {"probed": False, "reason": str(exc)}


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def render_frames(cfg: CaptureConfig, log=print) -> CaptureResult:
    """Render every frame of the animation to PNG files. Returns a result."""
    started = time.time()
    html = Path(cfg.html).resolve()
    if not html.exists():
        raise FileNotFoundError(f"HTML not found: {html}")

    browser = cfg.browser or detect_browser().path
    if not browser:
        raise RuntimeError(
            "No Chromium-family browser found. Install Chrome/Edge/Chromium or "
            "set CODE2VIDEO_BROWSER=/path/to/browser")

    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("frame_*.png"):
        stale.unlink()

    gpu = detect_gpu()
    warnings: List[str] = []

    # Candidate flag sets, best first.
    if cfg.use_gpu and gpu.available:
        flag_sets = [chrome_render_flags(gpu)]
        if platform.system() == "Windows":
            flag_sets.append(["--use-angle=d3d11", "--enable-gpu-rasterization",
                              "--ignore-gpu-blocklist"])
        flag_sets.append([])  # browser defaults
    else:
        flag_sets = [chrome_render_flags(gpu)]  # swiftshader

    headless_modes = ["old"] if not browser_supports_new_headless(
        detect_browser().version or "") else ["new"]

    def _try_open(modes):
        """Try each (headless mode, flag set) combo; keep the best session."""
        chosen: Optional[_BrowserSession] = None
        fallback: Optional[_BrowserSession] = None
        for mode in modes:
            for flags in flag_sets:
                try:
                    session = _open_session(browser, cfg.width or 1280,
                                            cfg.height or 720, flags, mode,
                                            cfg.startup_timeout)
                except Exception as exc:
                    warnings.append(
                        f"launch failed (headless={mode}, flags={flags}): {exc}")
                    continue
                session.renderer = _probe_renderer(session)
                if session.renderer.get("verdict") == "hardware":
                    if fallback:
                        fallback.close()
                    return session, None
                if fallback is None:
                    fallback = session
                else:
                    session.close()
            if fallback:
                break  # this headless mode works (just not hardware-accelerated)
        return chosen, fallback

    chosen, fallback = _try_open(headless_modes)
    if chosen is None and fallback is None and headless_modes == ["new"]:
        # --version probing can fail on odd builds; retry with legacy headless
        warnings.append("retrying with legacy --headless mode")
        chosen, fallback = _try_open(["old"])

    session = chosen or fallback
    if session is None:
        raise RuntimeError(
            f"Could not launch {browser} in headless mode. Warnings: {warnings}")
    if session.renderer.get("verdict") == "software" and gpu.available:
        warnings.append(
            "GPU detected but Chrome is using software rendering "
            "(SwiftShader); frames are correct but slower.")
    elif not session.renderer.get("probed") and gpu.available:
        warnings.append(
            "could not determine Chrome's renderer backend; "
            "GPU flags were applied on a best-effort basis.")

    try:
        session.cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": cfg.width or 1280, "height": cfg.height or 720,
             "deviceScaleFactor": 1, "mobile": False},
            session_id=session.session_id)

        session.cdp.call("Page.navigate", {"url": html.as_uri()},
                         session_id=session.session_id)

        ready = False
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if session.cdp.evaluate(
                    "typeof window.__renderFrame === 'function'",
                    session_id=session.session_id) is True:
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            raise RuntimeError(
                f"{html.name} does not define window.__renderFrame(index, t). "
                "See templates/animation_template.html for the contract.")

        spec_raw = session.cdp.evaluate(
            "window.VIDEO_SPEC ? JSON.stringify(window.VIDEO_SPEC) : '{}'",
            session_id=session.session_id) or "{}"
        try:
            spec = json.loads(spec_raw)
        except ValueError:
            spec = {}

        fps = cfg.fps or int(spec.get("fps") or 30)
        frames = cfg.frames or int(spec.get("frames") or 150)
        width = cfg.width or int(spec.get("width") or 1280)
        height = cfg.height or int(spec.get("height") or 720)
        if frames < 1:
            raise ValueError("frames must be >= 1")

        session.cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1,
             "mobile": False},
            session_id=session.session_id)

        log(f"  rendering {frames} frames @ {fps}fps ({width}x{height}) "
            f"via {Path(browser).name} [{session.renderer.get('verdict', 'unknown')}]")

        for i in range(frames):
            t = i / float(fps)
            session.cdp.call(
                "Runtime.evaluate",
                {"expression": f"window.__renderFrame({i}, {t:.6f})",
                 "returnByValue": True, "awaitPromise": True},
                session_id=session.session_id)
            shot = session.cdp.call(
                "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": False, "fromSurface": True},
                session_id=session.session_id, timeout=cfg.frame_timeout)
            (out_dir / f"frame_{i:05d}.png").write_bytes(
                base64.b64decode(shot["data"]))
            if (i + 1) % 10 == 0 or i + 1 == frames:
                log(f"  captured {i + 1}/{frames} frames")

        return CaptureResult(
            frames_dir=out_dir, frames=frames, fps=fps, width=width, height=height,
            browser=browser, renderer=session.renderer, gpu_available=gpu.available,
            warnings=warnings, elapsed_seconds=time.time() - started)
    finally:
        session.close()
