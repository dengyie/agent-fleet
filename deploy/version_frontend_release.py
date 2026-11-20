"""Version local static asset references in a packaged frontend release."""
from __future__ import annotations

import re
import sys
from pathlib import Path


ASSET_SUFFIXES = r"(?:js|css|png|ico|svg|webp|woff2?|ttf|otf)"
LOCAL_ASSET = re.compile(
    rf"(?P<url>(?:\.{{1,2}}/|/)[^\s\"'()<>?#]+\.{ASSET_SUFFIXES})(?P<suffix>[?#][^\s\"'()<>]*)?",
    re.IGNORECASE,
)
JS_REFERENCE = re.compile(
    r"(?P<prefix>\bfrom\s*[\"']|\bimport\s*\(\s*[\"']|\bimport\s*[\"'])"
    r"(?P<url>(?:\.{1,2}/|/)[^\"']+)"
    r"(?P<quote>[\"'])"
)
HTML_REFERENCE = re.compile(
    r"(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>[\"'])"
    r"(?P<url>(?:\.{1,2}/|/)[^\"']+)(?P=quote)",
    re.IGNORECASE,
)
CSS_REFERENCE = re.compile(
    r"(?P<prefix>\burl\(\s*)(?P<quote>[\"']?)"
    r"(?P<url>(?:\.{1,2}/|/)[^\s\"')]+)(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)


def _version_url(value: str, version: str) -> str:
    match = LOCAL_ASSET.fullmatch(value)
    if not match:
        return value
    url = match.group("url")
    suffix = match.group("suffix") or ""
    query, fragment = (suffix.split("#", 1) + [""])[:2]
    separator = "&" if "?" in query else "?"
    return f"{url}{query}{separator}v={version}" + (f"#{fragment}" if fragment else "")


def _rewrite(text: str, pattern: re.Pattern[str], version: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        rewritten = _version_url(url, version)
        return match.group(0).replace(url, rewritten, 1)

    return pattern.sub(replace, text)


def version_release(root: Path, version: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise ValueError("invalid release version")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".js", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".html":
            text = _rewrite(text, HTML_REFERENCE, version)
        elif path.suffix.lower() == ".js":
            text = _rewrite(text, JS_REFERENCE, version)
        else:
            text = _rewrite(text, CSS_REFERENCE, version)
        path.write_text(text, encoding="utf-8", newline="")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: version_frontend_release.py <release_root> <version>")
    version_release(Path(sys.argv[1]), sys.argv[2])
