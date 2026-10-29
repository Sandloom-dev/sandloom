---
title: Website publishing
description: Build, review and manually publish the unified Sandloom website and documentation to GitHub Pages at sandloom.dev.
---

# Website publishing

The website and documentation are one static build: `https://sandloom.dev/`
and `https://sandloom.dev/docs/`. GitHub Pages serves both. Cloudflare manages
the domain's DNS; no separate `docs` DNS record is needed.

## Build and review

From the repository root:

```bash
uv sync --locked --all-extras
uv run python tools/docs/docs_site.py build
uv run python tools/docs/docs_site.py serve --dev-addr 127.0.0.1:8766
```

The local build uses loopback canonical URLs and `noindex`. To inspect the
publication build without publishing it:

```bash
uv run python tools/docs/docs_site.py build --site-url https://sandloom.dev
SANDLOOM_DOCS_SITE_PUBLIC=1 uv run python -m unittest discover -s tools/docs -p test_docs_site.py
```

The **Website (GitHub Pages)** workflow is manual. Its `deploy` input defaults
to **false**, so a normal dispatch only builds and retains a Pages artifact.
PR documentation checks also build without publishing.

## Configure the domain once

Do these steps in order, using the owner's GitHub account:

1. In the **Sandloom-dev organization → Settings → Pages**, add `sandloom.dev`
   as a verified domain. GitHub provides a TXT record and its exact value.
   Add that TXT record in **Cloudflare → sandloom.dev → DNS**, then verify it
   in GitHub. Keep the TXT record after verification. Never invent the value.
2. In **Sandloom-dev/sandloom → Settings → Pages**, choose **GitHub Actions**
   as the source and set the custom domain to **sandloom.dev** before pointing
   the domain at Pages. In the `github-pages` environment, limit deployments
   to `main`; add owner review when available.
3. In Cloudflare DNS, add these records with **DNS only** (gray cloud) and
   **TTL Auto**:

   | Type | Name | Value |
   | --- | --- | --- |
   | A | `@` | `185.199.108.153` |
   | A | `@` | `185.199.109.153` |
   | A | `@` | `185.199.110.153` |
   | A | `@` | `185.199.111.153` |
   | CNAME | `www` | `sandloom-dev.github.io` |

   Remove only conflicting apex/web records after checking their purpose.
   Keep existing mail MX/TXT records and GitHub's domain-verification TXT.
   Do not add a wildcard DNS record. The `www` target does not contain the
   repository name or a URL scheme.
4. Confirm GitHub's DNS check. Certificate provisioning and the **Enforce HTTPS**
   option may become available only after the first explicitly requested
   deployment. Keep DNS-only during this setup, then enable **Enforce HTTPS**
   as soon as GitHub's certificate is ready.

GitHub documents the required [apex and subdomain records](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)
and [organization domain verification](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages).

## Publish an approved revision

After review and merge, dispatch **Website (GitHub Pages)** from `main` with
`deploy: true`. Only its deployment job receives `pages: write` and an OIDC
token. It refuses a Pages host other than `sandloom.dev` or a nonempty repository
URL prefix. Initial Pages metadata may use HTTP before GitHub finishes issuing
the certificate; the build's canonical URLs remain HTTPS. The build artifact is deployed without rebuilding.
This workflow does not publish Python packages.

After deployment check `/`, `/docs/`, `/examples/`, search from the homepage,
a documentation error anchor, `/sitemap.xml`, `/robots.txt` and a missing URL.
The missing URL must render the site's 404 page. Verify both the apex and
`www` use HTTPS and the canonical domain is `sandloom.dev`.

For a website rollback, review a revert PR, merge it, then manually deploy the
resulting `main` revision. Source and deployment review remain visible.

## Related reading

- [Contributor workflow](index.md)
- [Package publishing](../guides/publishing.md)
