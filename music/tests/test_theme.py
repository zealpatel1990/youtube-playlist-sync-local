"""
The dashboard has to be legible in both colour schemes.

Bootstrap 5.3 splits its colour utilities in two, and the distinction is easy to
miss. `text-bg-light` / `bg-light` resolve to `--bs-light`, which is the *named
colour* light — #f8f9fa in both schemes. Against a dark page that reads as a
bright pill; against a white page it is very nearly invisible. The theme-aware
utilities (`bg-body-secondary`, `text-body-secondary`, `bg-body-tertiary`) go
through `--bs-secondary-bg` and friends, which Bootstrap re-maps under
`data-bs-theme`.

The templates were originally written and reviewed in dark mode, where the
broken variant looks correct, which is exactly why this is a test rather than a
note: nothing about rendering it in the theme you happen to use would catch it.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

TEMPLATE_DIR = Path(settings.BASE_DIR) / "music" / "templates"
STATIC_DIR = Path(settings.BASE_DIR) / "music" / "static" / "music"

#: Utilities that pin a colour regardless of the active theme, with the
#: theme-aware replacement to use instead.
THEME_FIXED = {
    "text-bg-light": "bg-body-secondary text-body-secondary",
    "text-bg-dark": "bg-body-secondary text-body-secondary",
    "bg-light": "bg-body-secondary",
    "bg-dark": "bg-body-secondary",
    "bg-white": "bg-body",
    "text-white": "text-body",
    "text-dark": "text-body",
}


class ThemeAwarenessTests(SimpleTestCase):
    def test_no_template_pins_a_colour_against_the_theme(self):
        offenders = []
        for path in sorted(TEMPLATE_DIR.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for bad, good in THEME_FIXED.items():
                # \b so bg-light does not also match bg-lighter, and
                # text-bg-light is not double-reported by the bg-light entry.
                for match in re.finditer(rf'class="[^"]*\b{re.escape(bad)}\b', text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(
                        f"{path.relative_to(TEMPLATE_DIR)}:{line} uses {bad!r}; "
                        f"use {good!r}"
                    )
        self.assertEqual(offenders, [], "\n".join([""] + offenders))

    def test_no_stylesheet_hardcodes_a_page_colour(self):
        """Custom CSS must use the Bootstrap variables, not literal colours.

        A hex value in app.css is frozen against the theme the same way
        `bg-light` is, and is harder to spot because it never appears in markup.
        """
        offenders = []
        for path in sorted(STATIC_DIR.glob("*.css")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("/*", "*", "//")):
                    continue
                if re.search(r":\s*#[0-9a-fA-F]{3,8}\b", stripped):
                    offenders.append(f"{path.name}:{number}  {stripped[:80]}")
        self.assertEqual(
            offenders, [],
            "hardcoded colours; use var(--bs-*) so the theme can re-map them:\n"
            + "\n".join(offenders),
        )


class ThemeRenderTests(SimpleTestCase):
    databases = {"default"}

    def test_idle_badge_is_theme_aware(self):
        """The reported bug: the Idle badge vanished on a light background."""
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Idle", body)

        # Find the badge markup around the Idle label and check its classes.
        index = body.find("Idle")
        window = body[max(0, index - 300):index]
        span = window.rfind("<span")
        classes = window[span:]
        self.assertIn("bg-body-secondary", classes)
        self.assertNotIn("text-bg-light", classes)

    def test_both_themes_are_defined_in_the_vendored_css(self):
        """The fix relies on Bootstrap re-mapping these under data-bs-theme."""
        css = (
            Path(settings.BASE_DIR) / "music" / "static" / "vendor" / "bootstrap"
            / "bootstrap.min.css"
        ).read_text(encoding="utf-8", errors="ignore")
        self.assertIn("[data-bs-theme=dark]", css)
        self.assertIn("--bs-secondary-bg", css)
