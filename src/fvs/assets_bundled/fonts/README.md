# Bundled fonts

Caption fonts that ship with FVS (NFR-X1: nothing is loaded from the internet at run
time). On start, FVS copies every `.ttf`/`.otf` here into `workspace/assets/fonts`, which
FFmpeg's `subtitles` filter uses as its `fontsdir`.

The built-in caption styles use:

| Style         | Family     | Weight |
|---------------|------------|--------|
| `bold_yellow` | Montserrat | 900    |
| `clean_white` | Inter      | 700    |

Both are under the SIL Open Font License 1.1. Until a family's files are here, captions
fall back to a system font (Arial Black or Arial) and the render result says so.
