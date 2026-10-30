"""Build the unified Sandloom site or serve an existing build locally.

uv run --extra docs python tools/docs/docs_site.py build
uv run --extra docs python tools/docs/docs_site.py serve --dev-addr 127.0.0.1:8766

The serve command serves build/site; build explicitly after editing sources.
No deployment, network publication, tests or dependency installation is performed.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import html
import http.server
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / "build/site"
SITE_DESCRIPTION = (
    "Python backend framework for AI-driven development. Enforced module boundaries, "
    "pluggable infrastructure, and monolith-to-services deployment without rewriting "
    "business modules."
)


class PageMetadata(HTMLParser):
    """Use the rendered title, authored description or first documentation paragraph."""

    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.description = ""
        self.paragraph: list[str] = []
        self.in_title = False
        self.in_article = False
        self.in_paragraph = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta" and values.get("name") == "description":
            self.description = values.get("content") or ""
        elif tag == "article":
            self.in_article = True
        elif tag == "p" and self.in_article and not self.paragraph:
            self.in_paragraph = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "article":
            self.in_article = False
        elif tag == "p":
            self.in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        if self.in_paragraph:
            self.paragraph.append(data)


def site_origin(value: str) -> str:
    """The unified website is hosted at an origin, with documentation at /docs/."""
    url = urlsplit(value)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not url.hostname or url.username or url.password or url.query or url.fragment
        or url.path not in {"", "/"}
        or (url.scheme != "https" and not (local and url.scheme == "http"))
    ):
        raise ValueError("--site-url must be an HTTPS origin, without a path (HTTP allowed on loopback)")
    return value.rstrip("/")


def finalize_metadata(origin: str, *, public: bool) -> None:
    """Give every page one shared icon set and server-rendered sharing metadata."""
    origin = site_origin(origin)
    defaults = {
        SITE_DESCRIPTION,
        "A configurable-monolith framework for building SaaS — one codebase, monolith or microservices by configuration.",
        "Build SaaS with AI in isolated Python modules. Sandloom supplies identity, tenancy, billing and transaction mechanisms, from monolith to services.",
    }
    urls: list[str] = []
    for page in sorted(SITE.rglob("*.html")):
        source = page.read_text()
        metadata = PageMetadata()
        metadata.feed(source)
        title = "".join(metadata.title).strip() or "Sandloom"
        description = metadata.description
        if not description or description in defaults:
            description = " ".join("".join(metadata.paragraph).split()) or SITE_DESCRIPTION
        if len(description) > 185:
            description = description[:182].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
        relative = page.relative_to(SITE).as_posix()
        path = "/" + relative.removesuffix("index.html")
        not_found = relative in {"404.html", "docs/404.html"}
        card = "docs" if path.startswith("/docs/") else "examples" if path.startswith("/examples/") else "home"
        image_path = f"/assets/social/sandloom-{card}.png"
        image_version = hashlib.sha256((SITE / image_path.lstrip("/")).read_bytes()).hexdigest()[:12]
        image = f"{origin}{image_path}?v={image_version}"
        alt = {
            "home": "Sandloom. Build with AI. Keep control. A sand-colored caracal on graphite.",
            "docs": "Sandloom documentation. From your first module to services. A sand-colored caracal on graphite.",
            "examples": "Sandloom examples. Real apps. Clear starting points. A sand-colored caracal on graphite.",
        }[card]
        tags = []

        def meta(name: str, content: str, *, property: bool = False) -> None:
            key = "property" if property else "name"
            tags.append(f'<meta {key}="{name}" content="{html.escape(content, quote=True)}">')

        tags.extend([
            '<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">',
            '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml" sizes="any">',
            '<link rel="icon" href="/assets/favicon-96.png" type="image/png" sizes="96x96">',
            '<link rel="apple-touch-icon" href="/apple-touch-icon.png" sizes="180x180">',
        ])
        meta("description", description)
        meta("application-name", "Sandloom")
        meta("theme-color", "#111213")
        meta("color-scheme", "light dark")
        meta("robots", "index, follow, max-image-preview:large" if public and not not_found else "noindex, follow")
        for name, value in {
            "og:type": "website", "og:site_name": "Sandloom", "og:locale": "en_US",
            "og:title": title, "og:description": description, "og:image": image,
            "og:image:type": "image/png", "og:image:width": "1200", "og:image:height": "630", "og:image:alt": alt,
        }.items():
            meta(name, value, property=True)
        for name, value in {
            "twitter:card": "summary_large_image", "twitter:title": title,
            "twitter:description": description, "twitter:image": image, "twitter:image:alt": alt,
        }.items():
            meta(name, value)
        if not not_found:
            url = origin + path
            tags.append(f'<link rel="canonical" href="{html.escape(url, quote=True)}">')
            meta("og:url", url, property=True)
            urls.append(url)

        def remove_managed_tag(match: re.Match[str]) -> str:
            # Parse only head tags; keep Zensical's alternate Markdown, navigation and CSS links.
            class Attributes(HTMLParser):
                values: dict[str, str | None] = {}

                def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                    self.values = dict(attrs)

            tag = Attributes()
            tag.feed(match[0])
            name = tag.values.get("name") or tag.values.get("property") or ""
            rel = tag.values.get("rel") or ""
            managed = name in {"description", "application-name", "theme-color", "color-scheme", "robots"}
            if managed or name.startswith(("og:", "twitter:")) or rel in {"canonical", "icon", "shortcut icon", "apple-touch-icon"}:
                return ""
            return match[0]

        head, separator, body = source.partition("</head>")
        if not separator:
            raise ValueError(f"Missing document head: {page}")
        head = re.sub(r"<(?:meta|link)\b[^>]*>", remove_managed_tag, head, flags=re.IGNORECASE)
        page.write_text(head + "\n  " + "\n  ".join(tags) + "\n</head>" + body)
    if public:
        (SITE / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{xml_escape(url)}</loc></url>\n" for url in urls)
            + "</urlset>\n"
        )
        (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n")
    else:
        (SITE / "sitemap.xml").unlink(missing_ok=True)
        (SITE / "robots.txt").write_text("# Local preview; build with --site-url for publication.\nUser-agent: *\nDisallow: /\n")
    # The unified sitemap is authoritative; Zensical's relative /docs/ sitemap is obsolete.
    for filename in ("sitemap.xml", "sitemap.xml.gz"):
        (SITE / "docs" / filename).unlink(missing_ok=True)


class SiteScripts(HTMLParser):
    """Read the shared navigation and runtime from the generated docs homepage."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self.current: dict[str, str | None] | None = None
        self.body = ""
        self.navigation: list[str] = []
        self.nav_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.nav_depth or (not self.navigation and "data-sandloom-navigation" in dict(attrs)):
            self.nav_depth += 1
            self.navigation.append(self.get_starttag_text())
        if tag == "script":
            self.current = dict(attrs)
            self.body = ""

    def handle_data(self, data: str) -> None:
        if self.nav_depth:
            self.navigation.append(data)
        if self.current is not None:
            self.body += data

    def handle_entityref(self, name: str) -> None:
        if self.nav_depth:
            self.navigation.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.nav_depth:
            self.navigation.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if self.nav_depth:
            self.navigation.append(f"</{tag}>")
            self.nav_depth -= 1
        if tag == "script" and self.current is not None:
            self.scripts.append((self.current, self.body))
            self.current = None


def site_runtime() -> dict[str, object]:
    """Reuse Zensical's resolved navigation, native search UI and index at /."""
    scripts = SiteScripts()
    scripts.feed((SITE / "docs/index.html").read_text())
    config = next(json.loads(body) for attrs, body in scripts.scripts if attrs.get("id") == "__config")
    config["base"] = "/docs/"
    config["features"] = ["search.highlight"]
    config["search"] = "/docs/" + config["search"].removeprefix("./")
    bundle = next(attrs["src"] for attrs, _ in scripts.scripts if "/bundle." in (attrs.get("src") or ""))
    bootstrap = next(body for _, body in scripts.scripts if body.startswith("__md_scope="))
    if not scripts.navigation or scripts.nav_depth:
        raise ValueError("The docs build did not provide a complete shared navigation tree")
    return {
        "search_config": config,
        "search_bundle": "/docs/" + bundle.removeprefix("./"),
        "search_bootstrap": bootstrap,
        "navigation_tree": "".join(scripts.navigation),
    }


def version_site_assets() -> None:
    """Invalidate shared asset caches after a new local or published build."""
    pattern = re.compile(r"((?:src|href)=[\"'])(/assets/[A-Za-z0-9_-]+\.(?:css|js))([\"'])")
    shared_urls = {
        f"/assets/{asset.name}"
        for asset in (REPO / "website/assets").iterdir()
        if asset.suffix in {".css", ".js"}
    }
    versions: dict[str, str] = {}

    def version(match: re.Match[str]) -> str:
        url = match[2]
        if url not in shared_urls:
            return match[0]
        if url not in versions:
            asset = (SITE / url.lstrip("/")).resolve()
            if not asset.is_relative_to(SITE.resolve()) or not asset.is_file():
                raise ValueError(f"Missing shared website asset: {url}")
            versions[url] = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
        return f"{match[1]}{url}?v={versions[url]}{match[3]}"

    for page in SITE.rglob("*.html"):
        original = page.read_text()
        updated = pattern.sub(version, original)
        if updated != original:
            page.write_text(updated)


_LINK = re.compile(r'(?:href|src)="([^"#?]+)')


def broken_links(site: Path) -> list[tuple[str, str]]:
    """Internal links and assets of the built site that resolve to no file.

    The strict docs build does not check links inside included snippets (the
    changelog includes CHANGELOG.md) or on the hand-written website pages.
    """
    broken: list[tuple[str, str]] = []
    for page in sorted(site.rglob("*.html")):
        for target in _LINK.findall(page.read_text(errors="ignore")):
            if target.startswith(("http:", "https:", "mailto:", "data:", "//", "javascript:", "{")):
                continue
            path = (site / target.lstrip("/")) if target.startswith("/") else (page.parent / target)
            path = path.resolve()
            if path.is_dir():
                path = path / "index.html"
            if not path.exists():
                broken.append((page.relative_to(site).as_posix(), target))
    return broken


def build(*, public_url: str | None = None, preview_url: str = "http://127.0.0.1:8766") -> int:
    origin = site_origin(public_url or preview_url)
    synced = subprocess.run([sys.executable, str(REPO / "tools/packaging/sync_release.py")], cwd=REPO, check=False)
    if synced.returncode:
        return synced.returncode
    check = subprocess.run(
        [sys.executable, str(REPO / "tools/docs/docs_reference.py"), "--check"],
        cwd=REPO, check=False,
    )
    if check.returncode:
        return check.returncode
    result = subprocess.run([sys.executable, "-m", "zensical", "build", "--strict"], cwd=REPO, check=False)
    if result.returncode:
        return result.returncode
    website = REPO / "website"
    templates = Environment(
        loader=FileSystemLoader([website, website / "theme"]),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )
    context = site_runtime()
    for source in website.rglob("*"):
        if "theme" in source.relative_to(website).parts:
            continue
        if source.is_file():
            target = SITE / source.relative_to(REPO / "website")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".html":
                relative = source.relative_to(website)
                target.write_text(templates.get_template(relative.as_posix()).render(
                    is_docs=False,
                    page_section=relative.parts[0] if len(relative.parts) > 1 else "home",
                    **context,
                ))
            else:
                shutil.copy2(source, target)
    for filename in ("LICENSE", "NOTICE"):
        shutil.copy2(REPO / filename, SITE / filename)
    # Static hosts serve /404.html for any missing path, not only under /docs/.
    shutil.copy2(SITE / "docs" / "404.html", SITE / "404.html")
    broken = broken_links(SITE)
    if broken:
        for page, target in broken:
            print(f"{page}: broken link {target}", file=sys.stderr)
        return 1
    version_site_assets()
    finalize_metadata(origin, public=public_url is not None)
    # Root discovery for agents points to the native docs exports.
    (SITE / "llms.txt").write_text(
        "# Sandloom\n\n" + SITE_DESCRIPTION + "\n\nDocumentation: /docs/\n\n"
        "- [Example applications](/examples/)\n"
        "- [Documentation index](/docs/llms.txt)\n"
        "- [Full documentation](/docs/llms-full.txt)\n"
    )
    print(f"Unified site ready in {SITE}: / and /docs/")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "serve"))
    parser.add_argument("--dev-addr", default="127.0.0.1:8766")
    parser.add_argument("--site-url", type=site_origin, help="Public HTTPS origin; omit for a noindex local preview")
    args = parser.parse_args()
    if args.command == "build":
        return build(public_url=args.site_url, preview_url=f"http://{args.dev_addr}")
    if not (SITE / "index.html").is_file() or not (SITE / "docs/index.html").is_file():
        parser.error("No unified build exists. Run the explicit build command first.")
    host, port = args.dev_addr.rsplit(":", 1)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The local preview binds loopback only")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    with http.server.ThreadingHTTPServer((host, int(port)), handler) as server:
        print(f"Sandloom local preview: http://{args.dev_addr}", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
