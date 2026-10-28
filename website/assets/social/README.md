# Sharing artwork

`sandloom-{home,docs,examples}.png` are the 1200 × 630 sharing images. They reuse
the Sandloom mark, graphite/sand palette and Manrope type. The SVG sources have
outlined lettering; the PNGs require no browser, web font or JavaScript to render.
`tools/docs/brand_assets.py` owns the copy and vector composition. The existing
mark in `docs/assets/sandloom-mark.svg` remains the source of the symbol's paths.

`Manrope-OFL.txt` retains the upstream copyright and SIL Open Font License;
`SOURCE.json` records its source URL and checksum.

These are prepared source assets. A normal website build simply copies them;
it does not download fonts, install packages or generate graphics.

## Regeneration (maintenance only)

1. Download the official Manrope variable TTF using the source recorded in
   `SOURCE.json`. Verify its SHA-256 before using it. The Git blob URL in that
   file identifies the exact original bytes if the upstream branch changes.
2. Generate the outlined SVGs and favicon with FontTools 4.68.0:

   ```sh
   uv run --no-project --with fonttools==4.68.0 python tools/docs/brand_assets.py --font /tmp/Manrope.ttf
   ```

3. In a maintenance Node environment with Sharp 0.35.5 available, rasterize:

   ```js
   const sharp = require("sharp");
   (async () => {
     for (const kind of ["home", "docs", "examples"]) {
       const base = `website/assets/social/sandloom-${kind}`;
       await sharp(`${base}.svg`).png().toFile(`${base}.png`);
     }
     for (const [name, size] of [
       ["website/assets/favicon-96.png", 96],
       ["website/apple-touch-icon.png", 180],
       ["/tmp/sandloom-icon.png", 256],
     ]) {
       await sharp("website/assets/favicon.svg", { density: 384 })
         .resize(size, size).png().toFile(name);
     }
   })();
   ```

4. Write the ICO fallback with Pillow:

   ```python
   from PIL import Image
   Image.open("/tmp/sandloom-icon.png").save(
       "website/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)]
   )
   ```

Inspect all three PNGs and the small icon before rebuilding. Keep important
content inside the inset area so modest crops retain the mark and headlines.
The metadata builder versions image URLs from their PNG content hashes.

Metadata follows the [Open Graph protocol](https://ogp.me/). The site emits
`summary_large_image` for X and uses PNG rather than SVG for sharing images.
Favicons include SVG, a 96px PNG, ICO fallbacks and a 180px Apple touch icon;
see [Google's favicon guidance](https://developers.google.com/search/docs/appearance/favicon-in-search).
