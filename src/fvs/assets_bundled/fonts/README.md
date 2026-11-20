# Bundled fonts

Caption fonts that ship with FVS (NFR-X1: nothing is loaded from the internet at run
time). On start, FVS copies every `.ttf`/`.otf` here into `workspace/assets/fonts`, which
FFmpeg's `subtitles` filter uses as its `fontsdir`.

The built-in caption styles use:

| Style         | Family     | Weight | File                   |
|---------------|------------|--------|------------------------|
| `bold_yellow` | Montserrat | 900    | `Montserrat-Black.ttf` |
| `clean_white` | Inter      | 700    | `Inter-Bold.ttf`       |

Both are under the SIL Open Font License 1.1 (`OFL-Montserrat.txt`, `OFL-Inter.txt`).
They are static instances cut from the variable fonts in the Google Fonts repository
(`ofl/montserrat/Montserrat[wght].ttf` at wght 900, `ofl/inter/Inter[opsz,wght].ttf` at
wght 700 and opsz 32) with fontTools, named plainly "Montserrat" and "Inter" so libass
picks them by family. If a family's file is missing, captions fall back to a system font
(Arial Black or Arial) and the render result says so.
