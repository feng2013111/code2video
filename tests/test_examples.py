"""Contract tests: every shipped animation must implement the HTML contract."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ANIMATION_DIRS = [REPO / "templates", REPO / "examples"]


def _html_files():
    files = []
    for d in ANIMATION_DIRS:
        files.extend(sorted(d.glob("*.html")))
    assert files, "no animation HTML files found"
    return files


def _js_code(html_text: str) -> str:
    """Extract <script> bodies with comments stripped.

    Determinism rules apply to *code*, not to the documentation comments
    that explain the rules.
    """
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text,
                         flags=re.S | re.I)
    code = "\n".join(scripts)
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)  # block comments
    code = re.sub(r"//[^\n]*", " ", code)               # line comments
    return code


def test_animations_exist():
    assert len(_html_files()) >= 3  # template + 2 examples


def test_animations_implement_contract():
    for html in _html_files():
        text = html.read_text(encoding="utf-8")
        assert re.search(r"window\.__renderFrame\s*=\s*function", text), \
            f"{html.name} must define window.__renderFrame(index, t)"
        assert "window.VIDEO_SPEC" in text, \
            f"{html.name} should expose window.VIDEO_SPEC"


def test_animations_avoid_wall_clock_and_unseeded_random():
    """Determinism guard: no Date.now / performance.now / bare Math.random."""
    for html in _html_files():
        code = _js_code(html.read_text(encoding="utf-8"))
        for banned in ("Date.now", "performance.now", "new Date", "Math.random"):
            assert banned not in code, f"{html.name} uses banned {banned}"


def test_animations_are_self_contained():
    """No external network resources."""
    for html in _html_files():
        text = html.read_text(encoding="utf-8")
        assert "http://" not in text, f"{html.name} loads an external resource"
        assert "https://" not in text, f"{html.name} loads an external resource"
