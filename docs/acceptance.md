# Phase 1 acceptance: the two manual runs

Everything else in AC-2 to AC-14 is automated (`uv run pytest -o addopts=""`; the
traceability test fails if a P1 requirement or an automated AC has no test). These two need
Claude Desktop, the real core and a real ElevenLabs key, so they spend voice characters.

Before both: `fvs doctor` is all green or warnings only, `fvs whisper-download` has run,
Settings → Connect Claude → **Add to Claude Desktop**, then restart Claude Desktop.

## AC-1: a folder of 10 clips, end to end

1. Put 10 clips in the input folder: your own, or the 7 test clips (`tests/fixtures/media/`)
   plus 3 of your own.
2. In Claude Desktop, pick the **process_folder** prompt from FVS and send it.
3. Pass: 10 projects reach **In review** with no editing by you; the Review queue shows 10
   cards; every tool result in the activity feed came back within 20 s (AC-11).

## AC-15: agent evaluation (pass: at least 13 of 15)

Run each task in a new chat after AC-1, so projects #1–#10 exist and are in review. Mark a
task done only if Claude finishes it **without help** and the check holds. Save each
transcript; for each failure note which tool description or error fix misled Claude.

| # | Ask Claude | Check |
|---|---|---|
| 1 | "Put the drone shot under sentence 3 of #3 and zoom in at 0:07." | Storyboard: sentence 3 uses that clip; the segment at 0:07 has a zoom; new render in review |
| 2 | "Make #3 funnier." | New script version with a lighter tone; voiceover, plan and render redone; in review |
| 3 | "The caption on #4 says 1000 instead of 1,000. Fix it." | Caption override only: 0 new voice characters; new render shows "1,000" |
| 4 | "Build a 3-part series from clip #2." | 3 projects, series set, parts 1–3, different time ranges of clip #2 |
| 5 | "The hook on #5 is too slow. Start on the most dramatic shot." | First segment changed; hook within 8 words; in review |
| 6 | "Look at #6 and tell me whether the captions are readable." | Claude uses `fvs_frames` or `fvs_storyboard` with the safe zone and answers from what it saw |
| 7 | "Does the voice on #7 match the script?" | Claude uses `fvs_listen` and reports missing or extra words, if any |
| 8 | "Shorten #8 to under 30 seconds." | New script version; estimate and final length under 30 s |
| 9 | "Use the clean light template for #9 and re-render." | Spec uses `clean_light`; render in review |
| 10 | "Mute the original sound on #10." | `audio.original.mode` is `mute`; no voice characters spent |
| 11 | "I rejected #3 with a note. Deal with it." | Claude reads the note with `fvs_projects_list` and acts on it |
| 12 | "Approve everything that passed every check." | Only projects whose quality checks all passed are approved and exported; the others stay in review |
| 13 | "Go back to the previous script of #2." | `fvs_project_restore`; voiceover reused if it exists; in review again after the render |
| 14 | "Add the music track I imported, quietly, from 0:12." | Music asset in the spec with `start_at` 12 and a low gain; ducked under the voice |
| 15 | "How many voice characters do I have left today, and will a batch of the remaining clips fit?" | Answer matches Settings (budget left) and the sum of `fvs_voiceover_generate` dry runs |

Record the result in `BUILD_PLAN.md` under WP9 (tasks passed, the changes made to tool
descriptions and error fixes, and a second run if the first fell below 13).
