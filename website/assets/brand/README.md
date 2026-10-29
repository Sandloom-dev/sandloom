# Sandloom brand assets

The caracal is Sandloom's primary mark. Use the provided mark and wordmark together to identify the project.

## Choose the right file

- `sandloom-mark.svg`: the main mark, at 24 CSS pixels and above.
- `sandloom-mark-micro.svg`: optical drawing for 16–23 CSS pixels.
- `sandloom-lockup.svg`: the mark and outlined wordmark, with their intended proportions and spacing.
- Ink, sand and white variants: fixed colours for documents and graphics. Transparent PNG marks are 1024px; PNG lockups are 1600px wide.
- `sandloom-avatar.png`: square graphite/sand avatar, 1024px.
- Native favicons: 16, 20, 24, 32, 48, 64 and 96px, plus SVG and ICO. Use the 16px and 20px optical files at those sizes.
- `github-social-preview.png`: 1280 × 640 card for repository link previews.

## Spacing and colour

Keep at least 10 master units of clear space around the visible logo (one eighth of the nominal 80-unit mark box). The surrounding layout supplies this spacing. Favicon and app-tile versions use their own optical insets.

Graphite: `#111213`. Sand on dark backgrounds: `#d8b892`. Ink on light backgrounds: `#262825`. White is available for a single-colour reverse.

Keep the proportions and facial geometry. Use the supplied optical drawing on small surfaces. The primary artwork needs no gradients, glow, shadows or decorative outline.

The wordmark uses outlined Manrope 600 with adjusted spacing; it needs no installed font. Ordinary website body text continues to use Manrope.

## Sources and regeneration

The canonical geometry is `docs/assets/sandloom-mark.svg` and `docs/assets/sandloom-mark-micro.svg`. `tools/docs/brand_assets.py` builds the vector exports and sharing cards; `tools/docs/render_brand_assets.cjs` builds raster files and the download archive. A normal website build copies these prepared files and needs no graphics tools.

Original Sandloom artwork: Vasyl Cherniavskyi, Apache-2.0. See LICENSE and NOTICE. Manrope-derived lettering retains its upstream SIL Open Font License in Manrope-OFL.txt.
