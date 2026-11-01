"""Acceptance checks for a built docs site, including its agent-facing pages.

Run after ``python tools/docs/docs_site.py build`` with ``python -m unittest discover -s
scripts -p test_docs_site.py``. ``SANDLOOM_DOCS_SITE`` selects a different build.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE = Path(os.environ.get("SANDLOOM_DOCS_SITE", "build/site/docs"))
REPO = Path(__file__).resolve().parents[2]


class ArticleLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_article = False
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article":
            self.in_article = True
        target = dict(attrs).get("href")
        if (
            self.in_article
            and tag == "a"
            and target
            and not target.startswith(
                ("#", "http:", "https:", "mailto:", "javascript:")
            )
        ):
            self.links.append(target)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.in_article = False


class DocumentationSiteTests(unittest.TestCase):
    def test_documentation_articles_offer_a_next_internal_page(self) -> None:
        for page in SITE.rglob("index.html"):
            links = ArticleLinks()
            links.feed(page.read_text())
            with self.subTest(page=page):
                self.assertTrue(links.links, "article has no internal onward link")

    def test_stale_symbol_lint_scans_the_repository_docs(self) -> None:
        from tools.docs import docs_stale_symbols

        self.assertEqual(docs_stale_symbols.ROOT, REPO)
        selected = docs_stale_symbols.select(REPO, [])
        self.assertIn("AGENTS.md", selected)
        self.assertIn("docs/getting-started/tutorial.md", selected)

    def test_every_setting_has_a_source_description(self) -> None:
        from sandloom.core.config.settings import Settings

        missing = [
            field.alias or name
            for name, field in Settings.model_fields.items()
            if not (field.description or "").strip()
        ]
        self.assertEqual(missing, [])

    def test_published_content_excludes_private_repository_links(self) -> None:
        for pattern in ("*.html", "*.md", "llms*.txt"):
            for page in SITE.parent.rglob(pattern):
                with self.subTest(page=page):
                    self.assertNotIn(
                        "github.com/Maryarty/auto_job_applyer", page.read_text()
                    )

    def test_publication_metadata_matches_the_selected_build(self) -> None:
        page = (SITE.parent / "index.html").read_text()
        if os.environ.get("SANDLOOM_DOCS_SITE_PUBLIC") == "1":
            self.assertIn('rel="canonical" href="https://sandloom.dev/"', page)
            self.assertIn(
                'name="robots" content="index, follow, max-image-preview:large"', page
            )
        else:
            self.assertIn('name="robots" content="noindex, follow"', page)
        self.assertIn("Python backend framework for AI-driven development", page)

    def test_every_sdk_table_link_lands_on_its_own_heading(self) -> None:
        sdk = SITE / "reference/sdk"
        headings = {
            page.resolve(): {
                anchor: unescape(re.sub(r"<[^>]+>", "", title)).rstrip("¶").strip()
                for anchor, title in re.findall(
                    r'<h[1-6] id="([^"]+)"[^>]*>(.*?)</h[1-6]>',
                    page.read_text(),
                    flags=re.DOTALL,
                )
            }
            for page in sdk.rglob("index.html")
        }
        link = re.compile(
            r'<td><a href="([^"#]*)#([^"]+)"><code>([^<]+)</code></a></td>'
        )
        checked = 0
        for page in headings:
            for path, anchor, name in link.findall(page.read_text()):
                target = (page.parent / path / "index.html").resolve() if path else page
                with self.subTest(page=str(page), name=name):
                    self.assertEqual(headings[target].get(anchor), name)
                checked += 1
        self.assertGreater(checked, 300)

    def test_every_error_permalink_resolves_to_a_built_anchor(self) -> None:
        from sandloom.core.error_codes import CATALOGUE, documentation_url

        page = (SITE / "reference/error-codes/index.html").read_text()
        for entry in CATALOGUE:
            with self.subTest(code=entry.code):
                link = urlsplit(documentation_url(entry.code))
                self.assertEqual(link.netloc, "sandloom.dev")
                self.assertEqual(link.path, "/docs/reference/error-codes/")
                self.assertIn(f'id="{link.fragment}"', page)

    def test_tutorial_markdown_contains_the_real_code(self) -> None:
        tutorial = (SITE / "getting-started/tutorial/index.md").read_text()
        self.assertIn("class TasksItem(TenantScoped, Base):", tutorial)
        self.assertIn("async def create_item", tutorial)
        for page in SITE.rglob("*.md"):
            with self.subTest(page=page):
                self.assertNotIn("--8<--", page.read_text())

    def test_every_agent_index_link_has_a_markdown_page(self) -> None:
        index = (SITE / "llms.txt").read_text()
        links = re.findall(r"\]\(<?([^)>]+\.md)>?\)", index)
        self.assertTrue(links)
        canonical = urlsplit(
            tomllib.loads((REPO / "zensical.toml").read_text())["project"]["site_url"]
        )
        for link in links:
            with self.subTest(link=link):
                url = urlsplit(link)
                path = unquote(url.path)
                if url.netloc:
                    self.assertEqual(
                        (url.scheme, url.netloc), (canonical.scheme, canonical.netloc)
                    )
                if path.startswith("/"):
                    self.assertTrue(path.startswith(canonical.path))
                    path = path.removeprefix(canonical.path)
                self.assertTrue((SITE / path).is_file())

    def test_internal_research_is_absent_from_the_site_and_search(self) -> None:
        self.assertFalse((SITE / "design").exists())
        search = json.loads((SITE / "search.json").read_text())
        for item in search["items"]:
            self.assertFalse(item["location"].startswith("design/"), item["location"])

    def test_sdk_overview_describes_the_implemented_api(self) -> None:
        overview = (SITE / "reference/sdk/index.md").read_text()
        self.assertNotIn("Status at hand-off H0", overview)
        self.assertNotIn("NotImplementedError", overview)
        self.assertIn("sandloom docs", overview)

    def test_instant_navigation_has_its_sitemap(self) -> None:
        # Without it every link click reloads the page and rebuilds the search index.
        self.assertIn("<loc>", (SITE / "sitemap.xml").read_text())
        # The theme rewrites every [href] on navigation; an SVG href is read-only and stops it.
        for page in SITE.rglob("*.html"):
            self.assertNotRegex(page.read_text(), r"<(?:use|image)\b[^>]*\shref=", page)

    def test_pages_load_no_font_service(self) -> None:
        for page in SITE.parent.rglob("*.html"):
            html = page.read_text()
            self.assertNotIn("fonts.googleapis.com", html, page)
            self.assertNotIn("fonts.gstatic.com", html, page)

    def test_pages_load_no_diagram_library(self) -> None:
        self.assertEqual(
            [
                path
                for path in SITE.rglob("*.html")
                if 'class="mermaid"' in path.read_text()
            ],
            [],
        )

    def test_brand_kit_matches_the_rendered_site(self) -> None:
        import struct
        import zipfile
        from xml.etree import ElementTree

        site = SITE.parent
        brand = site / "assets/brand"
        canonical = ElementTree.parse(  # noqa: S314 - trusted built repository SVG
            SITE / "assets/sandloom-mark.svg"
        ).getroot()
        exported = ElementTree.parse(  # noqa: S314 - trusted built repository SVG
            brand / "sandloom-mark.svg"
        ).getroot()
        self.assertEqual([n.attrib for n in canonical], [n.attrib for n in exported])
        self.assertEqual(canonical[0].attrib["fill-rule"], "evenodd")
        self.assertEqual(
            (REPO / "admin/public/sandloom-mark.svg").read_bytes(),
            (REPO / "docs/assets/sandloom-mark.svg").read_bytes(),
        )
        for name in [
            "favicon.svg",
            "favicon-96.png",
            "favicon.ico",
            "apple-touch-icon.png",
        ]:
            self.assertEqual(
                (REPO / "admin/public" / name).read_bytes(),
                (brand / name).read_bytes(),
            )
        for page in [
            "index.html",
            "docs/index.html",
            "examples/index.html",
            "brand/index.html",
        ]:
            html = (site / page).read_text()
            self.assertIn('class="sl-brand-logo"', html)
            self.assertNotIn("woven S", html)
        self.assertFalse((site / "prototypes").exists())
        for size in [16, 20, 24, 32, 48, 64, 96]:
            png = (brand / f"favicon-{size}.png").read_bytes()
            self.assertEqual(struct.unpack(">II", png[16:24]), (size, size))
        preview = (brand / "github-social-preview.png").read_bytes()
        self.assertEqual(struct.unpack(">II", preview[16:24]), (1280, 640))
        self.assertLess(len(preview), 1_000_000)
        with zipfile.ZipFile(brand / "sandloom-brand-kit.zip") as archive:
            self.assertIsNone(archive.testzip())
            for name in [
                "sandloom-mark.svg",
                "sandloom-mark-micro.svg",
                "sandloom-lockup.svg",
                "sandloom-avatar.png",
                "favicon.ico",
                "LICENSE",
                "NOTICE",
                "Manrope-OFL.txt",
            ]:
                self.assertEqual(
                    archive.read("Sandloom/" + name), (brand / name).read_bytes()
                )
            self.assertEqual(
                archive.read("Sandloom/favicon.ico"),
                (site / "favicon.ico").read_bytes(),
            )

    def test_readme_code_stays_in_sync_with_the_executable_tutorial(self) -> None:
        import ast

        readme = (REPO / "README.md").read_text()
        example = re.search(
            r"<!-- readme:example:start -->\s*```python\n(.*?)```",
            readme,
            re.DOTALL,
        )
        self.assertIsNotNone(example)
        shown = ast.parse(example.group(1)).body
        tutorial = ast.parse(
            (REPO / "examples/tutorial/tasklist/modules/tasks/services.py").read_text()
        ).body
        self.assertTrue(any(isinstance(node, ast.AsyncFunctionDef) for node in shown))
        actual_nodes = {ast.dump(node) for node in tutorial}
        for node in shown:
            with self.subTest(node=ast.dump(node)):
                self.assertIn(ast.dump(node), actual_nodes)

    def test_readme_uses_a_published_app_and_its_doc_links_resolve(self) -> None:
        readme = (REPO / "README.md").read_text()
        quickstart = re.search(
            r"<!-- readme:quickstart:start -->\s*```bash\n(.*?)```",
            readme,
            re.DOTALL,
        )
        self.assertIsNotNone(quickstart)
        commands = quickstart.group(1)
        self.assertRegex(commands, r'uvx --from "sandloom==[^"]+" sandloom new')
        self.assertNotIn("--framework-source path", commands)
        self.assertNotIn("git clone", commands)
        self.assertIn("uv run sandloom test", readme)
        links = re.findall(r"\]\(([^)]+)\)", readme)
        for link in links:
            if "://" in link or link.startswith("#"):
                continue
            with self.subTest(link=link):
                self.assertTrue((REPO / link.split("#", 1)[0]).exists())


if __name__ == "__main__":
    unittest.main()


class BrokenLinkCheckTests(unittest.TestCase):
    """The post-build check that also covers snippets and the website pages."""

    def test_a_link_to_a_missing_page_or_asset_is_reported(self) -> None:
        import sys
        import tempfile

        sys.path.insert(0, str(REPO / "tools" / "docs"))
        from docs_site import broken_links

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "docs" / "guide").mkdir(parents=True)
            (site / "docs" / "guide" / "index.html").write_text("ok")
            (site / "assets").mkdir()
            (site / "assets" / "a.css").write_text("")
            (site / "index.html").write_text(
                '<a href="/docs/guide/">ok</a><a href="docs/guide/#x">ok</a>'
                '<link href="/assets/a.css"><a href="https://example.com/">ext</a>'
                '<a href="../docs/missing/">bad</a><img src="/assets/gone.png">'
            )
            self.assertEqual(
                broken_links(site),
                [
                    ("index.html", "../docs/missing/"),
                    ("index.html", "/assets/gone.png"),
                ],
            )
