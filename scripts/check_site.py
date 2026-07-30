#!/usr/bin/env python3
"""Small, dependency-free validator for the TimeBackHQ static site."""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PAGES = [
    "index.html",
    "services.html",
    "how-it-works.html",
    "examples.html",
    "about.html",
    "intake.html",
    "privacy.html",
]
EXPECTED_NAV = [
    "Home",
    "Services",
    "How it works",
    "Sample assessment",
    "About",
    "Contact",
    "Start a conversation",
]
EXPECTED_FORM_ACTION = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSe090mYFQlNmUL5XOheWihXjWeWEZv7P9GbJZtfVtK3FVLFuw/formResponse"
)
APPROVED_VISIBLE_FIELDS = {
    "entry.836891586",   # Name
    "entry.214103589",  # Business or organization
    "entry.1245710023", # Email
    "entry.1811277665", # What prompted you to reach out?
    "entry.893089578",  # First workflow or time drain
    "entry.1584994266", # Optional note
}
KNOWN_BLOCKED_REQUIRED_FIELDS = {
    "entry.164870094",  # Business description
    "entry.1705796048", # Preferred help radio group
}
ANALYTICS_MARKERS = (
    "googletagmanager",
    "google-analytics",
    "gtag(",
    "plausible.io",
    "matomo",
    "mixpanel",
    "segment.com/analytics",
)
FORBIDDEN_FORM_PROMPT = re.compile(
    r"\b(password|passcode|credential|social security|ssn|credit card|bank account|"
    r"patient record|client record|government record|identification document)\b",
    re.I,
)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.metas: list[dict[str, str]] = []
        self.link_tags: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.headings: list[tuple[int, str]] = []
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._nav_depth = 0
        self._in_primary_nav = False
        self.primary_nav_text: list[str] = []
        self._anchor_depth = 0
        self._anchor_parts: list[str] = []
        self._anchor_in_primary = False
        self.forms: list[dict[str, str]] = []
        self.controls: list[dict[str, str]] = []
        self._label_depth = 0
        self._label_parts: list[str] = []
        self._legend_depth = 0
        self._legend_parts: list[str] = []
        self.form_prompt_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.metas.append(attr_map)
        elif tag == "link":
            self.link_tags.append(attr_map)
        elif tag == "a":
            self.links.append(attr_map)
            self._anchor_depth += 1
            self._anchor_parts = []
            self._anchor_in_primary = self._in_primary_nav
        elif tag == "img":
            self.images.append(attr_map)
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif tag == "nav":
            self._nav_depth += 1
            if attr_map.get("aria-label") == "Primary navigation":
                self._in_primary_nav = True
        elif tag == "form":
            self.forms.append(attr_map)
        elif tag in {"input", "textarea", "select"}:
            self.controls.append({"tag": tag, **attr_map})
        elif tag == "label":
            self._label_depth += 1
            self._label_parts = []
        elif tag == "legend":
            self._legend_depth += 1
            self._legend_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self._anchor_depth:
            if self._anchor_in_primary:
                text = " ".join("".join(self._anchor_parts).split())
                if text:
                    self.primary_nav_text.append(text)
            self._anchor_depth -= 1
            self._anchor_parts = []
            self._anchor_in_primary = False
        elif re.fullmatch(r"h[1-6]", tag) and self._heading_level == int(tag[1]):
            text = " ".join("".join(self._heading_parts).split())
            assert self._heading_level is not None
            level: int = self._heading_level
            self.headings.append((level, text))
            self._heading_level = None
            self._heading_parts = []
        elif tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
            if self._in_primary_nav and self._nav_depth == 0:
                self._in_primary_nav = False
        elif tag == "label" and self._label_depth:
            text = " ".join("".join(self._label_parts).split())
            if text:
                self.form_prompt_text.append(text)
            self._label_depth -= 1
            self._label_parts = []
        elif tag == "legend" and self._legend_depth:
            text = " ".join("".join(self._legend_parts).split())
            if text:
                self.form_prompt_text.append(text)
            self._legend_depth -= 1
            self._legend_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self._heading_level is not None:
            self._heading_parts.append(data)
        if self._anchor_depth:
            self._anchor_parts.append(data)
        if self._label_depth:
            self._label_parts.append(data)
        if self._legend_depth:
            self._legend_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def parse_page(path: Path) -> tuple[PageParser, str]:
    text = path.read_text(encoding="utf-8")
    parser = PageParser()
    parser.feed(text)
    return parser, text


def internal_target(page: Path, href: str) -> Path | None:
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    parsed = urlparse(href)
    if parsed.scheme or parsed.netloc:
        return None
    clean = unquote(parsed.path)
    if clean.startswith("/"):
        clean = clean[1:]
    if not clean:
        clean = "index.html"
    target = (ROOT / clean) if href.startswith("/") else (page.parent / clean)
    if target.is_dir():
        target = target / "index.html"
    return target.resolve()


def main() -> int:
    argp = argparse.ArgumentParser()
    argp.add_argument(
        "--allow-known-intake-blocker",
        action="store_true",
        help="Report, but do not fail on the two live required fields pending Dave approval.",
    )
    args = argp.parse_args()

    errors: list[str] = []
    blockers: list[str] = []
    notes: list[str] = []
    parsed: dict[str, tuple[PageParser, str]] = {}

    for name in PUBLIC_PAGES:
        path = ROOT / name
        if not path.exists():
            errors.append(f"{name}: public page is missing")
            continue
        parser, text = parse_page(path)
        parsed[name] = (parser, text)

        if not parser.title:
            errors.append(f"{name}: missing <title>")
        descriptions = [
            m.get("content", "").strip()
            for m in parser.metas
            if m.get("name", "").lower() == "description"
        ]
        if not any(descriptions):
            errors.append(f"{name}: missing meta description")

        h1s = [text for level, text in parser.headings if level == 1]
        if len(h1s) != 1:
            errors.append(f"{name}: expected one H1, found {len(h1s)}")
        previous = 0
        for level, heading in parser.headings:
            if previous and level > previous + 1:
                errors.append(
                    f"{name}: heading skip H{previous} to H{level} before {heading!r}"
                )
            previous = level

        icons = [
            link.get("href", "")
            for link in parser.link_tags
            if "icon" in link.get("rel", "").lower().split()
        ]
        if not icons:
            errors.append(f"{name}: missing favicon link")
        for href in icons:
            target = internal_target(path, href)
            if target is not None and not target.exists():
                errors.append(f"{name}: favicon target missing: {href}")

        if parser.primary_nav_text != EXPECTED_NAV:
            errors.append(
                f"{name}: primary nav mismatch: {parser.primary_nav_text!r}"
            )

        for image in parser.images:
            src = image.get("src", "<missing src>")
            if not image.get("alt", "").strip():
                errors.append(f"{name}: image missing alt text: {src}")
            if not image.get("width", "").strip() or not image.get("height", "").strip():
                errors.append(f"{name}: image missing explicit width/height: {src}")
            target = internal_target(path, src)
            if target is not None and not target.exists():
                errors.append(f"{name}: image target missing: {src}")

        for link in parser.links:
            href = link.get("href", "")
            if "preview-" in href:
                errors.append(f"{name}: public link points to stale preview: {href}")
            target = internal_target(path, href)
            if target is not None and not target.exists():
                errors.append(f"{name}: broken internal link: {href}")

        if "http://timebackhq.com" in text.lower():
            errors.append(f"{name}: insecure TimeBackHQ URL")
        head = text.split("</head>", 1)[0]
        if re.search(r"<a\b", head, re.I):
            errors.append(f"{name}: anchor element found inside <head>")
        ids = re.findall(r'\bid=["\']([^"\']+)["\']', text, re.I)
        duplicate_ids = sorted({value for value in ids if ids.count(value) > 1})
        if duplicate_ids:
            errors.append(
                f"{name}: duplicate HTML IDs: " + ", ".join(duplicate_ids)
            )
        lower = text.lower()
        for marker in ANALYTICS_MARKERS:
            if marker in lower:
                errors.append(f"{name}: analytics marker found: {marker}")

        for prompt in parser.form_prompt_text:
            if FORBIDDEN_FORM_PROMPT.search(prompt):
                errors.append(f"{name}: forbidden sensitive-data prompt: {prompt!r}")

    intake = parsed.get("intake.html")
    if intake:
        parser, _ = intake
        if len(parser.forms) != 1:
            errors.append(f"intake.html: expected one form, found {len(parser.forms)}")
        else:
            action = parser.forms[0].get("action", "")
            if action != EXPECTED_FORM_ACTION:
                errors.append(f"intake.html: unexpected form action: {action}")
        visible_names = {
            c.get("name", "")
            for c in parser.controls
            if c.get("type", "").lower() != "hidden" and c.get("name", "")
        }
        missing = APPROVED_VISIBLE_FIELDS - visible_names
        if missing:
            errors.append(
                "intake.html: approved visible field IDs missing: "
                + ", ".join(sorted(missing))
            )
        blocked_visible = KNOWN_BLOCKED_REQUIRED_FIELDS & visible_names
        if blocked_visible:
            blockers.append(
                "intake.html: live Google Form still requires removed fields: "
                + ", ".join(sorted(blocked_visible))
            )

    if blockers and not args.allow_known_intake_blocker:
        errors.extend(blockers)
    elif blockers:
        notes.extend(f"KNOWN BLOCKER: {item}" for item in blockers)

    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for item in errors:
            print(f"- {item}")
        if notes:
            for item in notes:
                print(f"- {item}")
        return 1

    print(f"PASS: checked {len(PUBLIC_PAGES)} public pages")
    for item in notes:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
