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
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE = Path(os.environ.get("SANDLOOM_DOCS_SITE", "build/site/docs"))
REPO = Path(__file__).resolve().parents[2]


class DocumentationSiteTests(unittest.TestCase):
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
