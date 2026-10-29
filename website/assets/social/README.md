# Brand and sharing artwork

The canonical caracal geometry is `docs/assets/sandloom-mark.svg`, with a separate optical drawing in `docs/assets/sandloom-mark-micro.svg`. The main drawing works from 24 CSS pixels; the optical drawing is for 16–23px.

`tools/docs/brand_assets.py` regenerates the outlined brand exports, shared logo/mark template partials, adaptive SVG favicon and the three 1200 × 630 social SVG cards. It preserves the negative-space fill rule. It uses the existing Manrope TTF recorded in `SOURCE.json`, verifies its SHA-256 and outlines the wordmark at weight 600 with optical spacing.

`tools/docs/render_brand_assets.cjs` prepares transparent PNGs, native raster favicons, ICO, the 180px touch icon, the square avatar, sharing PNGs, a 1280 × 640 GitHub preview and a deterministic ZIP. The normal site build only copies prepared assets; it needs no graphics tools and downloads no fonts.

## Regeneration

Use the existing upstream Manrope TTF identified by `SOURCE.json`. Verify its checksum before running the vector step.

```sh
uv run --no-project --with fonttools==4.68.0 python tools/docs/brand_assets.py --font /tmp/Manrope.ttf
NODE_PATH=/path/to/node_modules node tools/docs/render_brand_assets.cjs
```

The raster step requires Sharp 0.35.5 and Python 3 for the standard-library ZIP writer. Inspect the mark, full lockup, 16px favicon and social cards before rebuilding the unified site.

`website/assets/brand/` is the downloadable brand kit and its usage guide; `/brand/` presents it on the unified site. Its ZIP includes the asset files, Apache license/NOTICE and Manrope's SIL Open Font License. The outlined lettering needs no web font to display.

Metadata follows the [Open Graph protocol](https://ogp.me/), using PNG cards and `summary_large_image`. SVG/PNG/ICO/touch icons share the same identity. Normal builds version the sharing images and stylesheet URLs from file content hashes.
