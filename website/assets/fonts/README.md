# Website code font

Hack Nerd Font Mono is self-hosted for code, commands and technical labels.
Manrope remains the text face. Upstream: https://github.com/ryanoasis/nerd-fonts.

`Hack-SOURCE.json` records the pinned upstream release, source checksums,
FontTools version and checksums of the WOFF2 web files. `Hack-LICENSE.md` is the
unmodified license supplied with that upstream font.

The Regular and Bold files are text subsets: Latin, Greek, Cyrillic,
punctuation, arrows, mathematical symbols and box drawing. Unused private-use
icon glyphs are omitted; the site uses SVG for icons. Font family metadata and
copyright/license records are retained.

To regenerate, download the two TTF files at the exact URLs in `Hack-SOURCE.json`
and run `pyftsubset` from the recorded `fonttools[woff]` version on each:

```sh
pyftsubset HackNerdFontMono-Regular.ttf \
  --output-file=hack-nerd-mono-regular.woff2 --flavor=woff2 \
  --unicodes='U+0000-052F,U+1E00-1EFF,U+2000-206F,U+2190-22FF,U+2500-259F,U+FFFD' \
  --name-IDs='*' --name-legacy --name-languages='*'
```

Use the same command with the Bold input and output names, then update the web
checksums in the source metadata. No font tool is needed to build or serve the
website; the prepared web files are ordinary source assets.
