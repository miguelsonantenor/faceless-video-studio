# Faceless Video Studio — Functional Specification (v1.1)

Nov 20, 2026 · @Miguelson Antenor

Faceless Video Studio (FVS) is a local app that turns raw clips into finished TikTok videos. Claude does the creative work as an agent through an MCP server: it looks at clips, writes scripts, plans the edit, checks the result and fixes it. The app does the voice, editing and rendering. This spec defines what the system must do; the companion Design Specification defines how.

## 1. Introduction

### 1.1 Purpose

This document specifies the functional and non-functional requirements of FVS v1. It follows the SRS outline of ISO/IEC/IEEE 29148:2018, which replaced IEEE 830. Every requirement has an ID so the design, the code and the tests can trace back to it (section 8).

### 1.2 Scope

In scope: scanning a local folder of clips; projects built from one or more clips; clip analysis; tools that let Claude see, hear (through transcripts and level charts) and change the work; scripts with simple markup; sentence-by-sentence voiceovers with ElevenLabs; an edit timed to the narration; one render per version; automatic quality checks; a local web app for review; and an export package ready to post.

Out of scope for v1: posting to TikTok automatically, cloud hosting, several users, an in-app chat (Claude Desktop is the chat), and languages that are not written with spaces between words.

### 1.3 Definitions

| Term | Meaning |
| --- | --- |
| Clip | One source video file in the input folder; never changed |
| Project | One video to publish: number, title, one or more clips, script, voiceover, edit, renders and status |
| Script | The voiceover text (with markup) plus title, hook text, post caption and hashtags |
| Sentence | One numbered line of the voiceover; the unit for voice generation, caching and timing |
| Cue | What sets a segment's length: a range of sentences or words, a number of seconds, or "until the end" |
| Edit Spec | A versioned JSON document that fully describes one edit; the only way Claude edits video |
| Template | Saved Edit Spec defaults (caption style, music, reframe, pacing) |
| Storyboard | One image plus a table showing each segment, its time range and the sentence spoken over it |
| Claude check | The state where Claude reviews a finished render before the creator sees it |
| Render | The single 1080×1920 file made from one Edit Spec version; the approved render is the file exported |
| QA report | The automatic checks run on every render |
| Activity feed | A plain-language log of each action Claude takes, shown live in the web app |
| MCP | Model Context Protocol, the standard Claude uses to call tools |
| Safe zone | The part of the 9:16 frame that TikTok's buttons and text do not cover |

### 1.4 References

- ISO/IEC/IEEE 29148:2018, requirements engineering (SRS outline)
- MCP specification 2025-11-25 and its Streamable HTTP security rules; MCP Apps extension
- Anthropic, *Writing effective tools for agents*
- ElevenLabs API: speech with timestamps, request stitching, pauses, errors, subscription
- FFmpeg filters documentation
- TikTok Community Guidelines (originality) and AI-generated content policy

Links are in section 9.4.

### 1.5 Revision history

| Version | Date | Change |
| --- | --- | --- |
| 1.0 | Nov 20, 2026 | First version |
| 1.1 | Nov 20, 2026 | After an editing-task review and a consistency review: projects separate from clips; narration-timed edits; tools for Claude to see and hear; script markup; sentence-level voice; one render per version; batch recovery; verification and traceability (section 8); security of the local MCP address |
| 1.1 | Nov 20, 2026 | Added inspiration sets (section 4.14, INS-1–4, `fvs_inspiration`), all P2 |

## 2. Overall description

### 2.1 Product perspective

FVS is a new, self-contained system that runs on the creator's computer. It has three faces:

- **MCP server**: the tools Claude calls. This is the main way work gets done.
- **Web app**: a local page for watching renders, approving, editing settings and watching Claude work.
- **Core engine**: shared by both. It owns the data, the job queue, FFmpeg, Whisper and the ElevenLabs connection.

Two rules guide the design:

- **Claude decides, the engine executes.** Claude writes scripts and fills in the Edit Spec. The engine never runs commands written by Claude.
- **Claude is an agent with no eyes or ears of its own.** Anything Claude needs to know or check must reach it through a tool: frames as images, the edit as a storyboard, sound as transcripts and level charts.

### 2.2 Who uses it

| Actor | Role |
| --- | --- |
| Creator (the user) | Drops clips in a folder, starts work in Claude Desktop, reviews and approves in the web app, posts to TikTok |
| Claude (via MCP) | Inspects clips, writes scripts, plans and fixes edits, checks every render before the creator sees it |
| ElevenLabs API | Produces voice audio with per-character timings |
| FFmpeg, ffprobe, Whisper | Analyse, render and check video and audio |
| TikTok | Receives the exported video, posted by the creator by hand |

### 2.3 Operating environment

- macOS 14+ (Apple Silicon preferred) or Windows 11. The first target OS is still open (section 9.2).
- FFmpeg 7 or newer on the PATH, built with libass and libx264.
- Claude Desktop with the FVS MCP server set up. Claude Code and other local MCP apps can connect through the local MCP address.
- An ElevenLabs account and API key.
- At least 20 GB free disk space.

### 2.4 Constraints

- **C-1** Everything runs locally. The only network calls are to ElevenLabs (and later, optionally, TikTok).
- **C-2** Claude Desktop handles tool results of about 1 MB at most, and long tool calls are reported to time out near 45 s. Every tool must return within 20 s; longer work runs as a background job.
- **C-3** The input folder is read-only to FVS.
- **C-4** Output must meet TikTok specs: 9:16, 1080×1920, H.264 and AAC.
- **C-5** Claude cannot hear audio; it reads text and sees images. Sound reaches Claude only as transcripts, numbers and charts.
- **C-6** Claude Desktop reads MCP resources only when the creator attaches them by hand, and does not support MCP elicitation (tool-initiated questions). Everything Claude needs is therefore a tool, and Claude asks the creator questions in chat.

### 2.5 Assumptions and dependencies

- **A-1** Clips are ones the creator owns or may use. FVS does not check rights.
- **A-2** ElevenLabs keeps these endpoints: speech with timestamps (`/v1/text-to-speech/{voice_id}/with-timestamps`, with `previous_request_ids` for stitching) and the user subscription (`/v1/user/subscription`).
- **A-3** Claude Desktop shows Claude the images that MCP tools return.
- **A-4** Claude Desktop supports MCP Apps (interactive views inside the chat). Used only for SEE-8, with the web app as fallback.

## 3. Use cases

Six use cases cover v1. UC-1 is the main one; the others reuse its steps.

### UC-1 Process a folder (batch)

1. The creator puts clips in the input folder and asks Claude to process them (or runs the `process_folder` prompt).
2. Claude calls `fvs_guide` to load the workflow, markup, schema, templates, voices and budget.
3. Claude calls `fvs_library_scan`, then `fvs_project_create` for each new clip (one project per clip by default).
4. For each project, Claude calls `fvs_clip_inspect` (details, scenes, speech in the clip, contact sheet), and `fvs_frames` for any moment it wants to look at closely. It skips unusable clips with `fvs_project_review`.
5. Claude writes the script, one sentence per line, and saves it with `fvs_script_save`. The tool returns the numbered sentences and whether the length fits.
6. Claude checks the cost with `fvs_voiceover_generate` in dry-run mode. If the batch is over the daily budget, Claude asks the creator in chat before going on.
7. Claude generates the voiceover. It gets back the timing of every sentence.
8. Claude calls `fvs_edit_plan`, which proposes which shot plays under each sentence. Claude checks it with `fvs_storyboard`, adjusts it, and saves it with `fvs_edit_save`.
9. Claude calls `fvs_render` and waits with `fvs_job_get`.
10. In **Claude check**, Claude reviews the render: `fvs_frames` on key moments (with the safe zone drawn), `fvs_listen` for the transcript and sound levels, and the QA report. It fixes problems, or marks the project ready.
11. The project appears **In review** in the web app.
12. The creator approves it in the web app or in chat. The engine writes the export package from the reviewed render, with no second render.

### UC-2 Revise one video in chat

1. The creator says, for example, "make #3 funnier, and the caption says 1000 instead of 1,000".
2. Claude reads the project with `fvs_project_get`.
3. For a caption fix, Claude adds a caption override in the Edit Spec. The voice is not regenerated.
4. For a script change, Claude saves a new script version. Only changed sentences are sent to ElevenLabs again.
5. Claude re-plans if needed, renders, checks, and marks the project ready.
6. Earlier versions stay available to compare or restore.

### UC-3 Review in the web app

1. The creator opens the review queue and plays a render.
2. The creator approves it, approves all that passed every check, or rejects it with a note such as "hook too slow".
3. A rejected project moves to **Changes requested**. Claude sees the note in `fvs_projects_list` and acts on it.

### UC-4 Build one video from several clips, or a series from one clip

1. The creator asks for a compilation of clips #4, #7 and #9, or a 3-part series from one long clip.
2. Claude creates the project(s) with `fvs_project_create`, naming the clips, series and part.
3. Claude cues segments from any of the project's clips to the sentences, as in UC-1.

### UC-5 Change a template and re-render

1. The creator or Claude edits a template (for example, a new caption font).
2. A dry run lists the projects that would change, with a diff. Exported projects are never changed.
3. Claude re-plans and re-renders the listed projects with one batch call.

### UC-6 Recover and resume

1. A step fails (ElevenLabs error, quota used up, FFmpeg error, failed QA check), or Claude Desktop closes mid-batch.
2. The failed job records a plain-language error with a fix. The project keeps its state and shows a failure flag. Other projects in the batch carry on.
3. Later, the creator runs `resume_batch` or asks Claude to continue. Claude lists unfinished projects by state and picks up where each stopped. No finished work is lost.

## 4. Functional requirements

There are 99 functional requirements in 15 groups. Priority uses MoSCoW: Must, Should, Could. Phase matches the roadmap in the Design Specification: P1 core pipeline, P2 templates and polish, P3 advanced editing, P4 quality and speed, P5 extras. No P1 requirement depends on a later one.

### 4.1 Library (LIB) and projects (PRJ)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| LIB-1 | Scan the input folder (optionally with subfolders) for .mp4, .mov, .m4v, .mkv and .webm files; register each new clip with an ID from a full-file hash, cached by path, size and modified time | Must | P1 |
| LIB-2 | Never write to, move or delete anything in the input folder | Must | P1 |
| LIB-3 | Detect clips that changed or disappeared, and flag the projects that use them | Should | P2 |
| LIB-4 | Optionally watch the folder and register new clips automatically | Could | P5 |
| PRJ-1 | A project is one video to publish, with a number, a title, a slug made from the title, one or more clips, and an optional series name and part number | Must | P1 |
| PRJ-2 | Create one project per new clip on request, or one project from several clips | Must | P1 |
| PRJ-3 | Several projects may use the same clip, each with its own time ranges | Must | P1 |
| PRJ-4 | Skip a project, and reopen a skipped or exported project | Must | P1 |
| PRJ-5 | Save every script, voiceover and Edit Spec as a new version that is never changed; restore an earlier version as the current one | Must | P1 |

### 4.2 Clip analysis (ANA)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| ANA-1 | Read duration, resolution, frame rate, rotation, codecs, bitrate and whether there is sound | Must | P1 |
| ANA-2 | Detect scene changes with timestamps | Must | P1 |
| ANA-3 | Detect black and frozen stretches | Must | P1 |
| ANA-4 | Transcribe speech in the original audio with word timings (local Whisper) | Must | P1 |
| ANA-5 | Measure the loudness of the original audio | Should | P1 |
| ANA-6 | Cache all analysis by clip hash | Must | P1 |
| ANA-7 | Run analysis as a background job when it would take longer than the tool time limit | Must | P1 |
| ANA-8 | Detect letterboxing (black bars) so the crop can remove it | Should | P2 |
| ANA-9 | Detect silences in the original audio | Should | P3 |
| ANA-10 | Track a subject to guide the 9:16 crop | Could | P5 |

### 4.3 Claude's eyes and ears (SEE)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| SEE-1 | One guide call returns the workflow, script markup, Edit Spec schema, templates, caption styles, voices, budget and ElevenLabs quota left | Must | P1 |
| SEE-2 | Return frames at exact times, or N frames spread over a time range, from a clip, a render, or the current or draft Edit Spec (drawn as stills with captions and overlays, without a full render) | Must | P1 |
| SEE-3 | Optionally draw the TikTok safe zone on returned frames | Should | P1 |
| SEE-4 | Return a storyboard of an Edit Spec or render: one tile per segment with its time range, clip, source time and the sentence spoken over it, as one image plus a table | Must | P1 |
| SEE-5 | "Listen" to a voiceover or render: speech-recognition transcript with times, word differences from the script, a loudness-over-time chart per track (voice, music, original), silences and clipping | Must | P1 |
| SEE-6 | Return a project's full state in one call: script, sentence and word timings, Edit Spec, resolved timeline, renders, QA, review notes and history | Must | P1 |
| SEE-7 | Keep every result under the size and time limits (section 5.2) | Must | P1 |
| SEE-8 | Show the creator a render or storyboard inside Claude Desktop (MCP Apps), with a web app link as fallback | Should | P2 |

### 4.4 Script (SCR)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| SCR-1 | Store per version: title, voiceover text, hook (8 words or fewer), post caption, hashtags, language, notes and author (Claude or creator) | Must | P1 |
| SCR-2 | Split the voiceover into numbered sentences: one per line, or at sentence-ending punctuation | Must | P1 |
| SCR-3 | Support markup: `[pause 0.8]` (0.1–3 s of silence), `{1,000\|one thousand}` (caption text \| spoken text) and `*word*` (caption emphasis) | Must | P1 |
| SCR-4 | Estimate the spoken length and say whether it fits the target length | Must | P1 |
| SCR-5 | v1 supports languages written with spaces between words; scripts in other scripts are refused with a clear error | Must | P1 |
| SCR-6 | Store a channel style guide (tone, audience, call-to-action rules, banned words), return it with the guide, and check scripts against it | Should | P2 |
| SCR-7 | Support `[voice: alias]` lines for two-voice scripts | Could | P3 |

### 4.5 Voiceover (VO)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| VO-1 | Generate the voiceover sentence by sentence with ElevenLabs' with-timestamps endpoint; work out word and sentence timings for the whole voiceover | Must | P1 |
| VO-2 | Keep the voice consistent across sentences with request stitching, where the model supports it | Should | P1 |
| VO-3 | Insert `[pause]` markup as exact silence and shift all later timings | Must | P1 |
| VO-4 | Cache each sentence by its spoken text, voice, model and settings, so unchanged sentences are never paid for again | Must | P1 |
| VO-5 | Support named voice profiles (voice ID, model, stability, similarity, style, speed, language), created in Settings, one of them the default | Must | P1 |
| VO-6 | Enforce a daily character budget; a dry run reports characters to be billed, budget left today and ElevenLabs characters left this period with the reset date | Must | P1 |
| VO-7 | Turn ElevenLabs errors into clear ones: bad key, quota used up, text too long, too busy (retry with backoff, at most 2 requests at once), invalid input | Must | P1 |
| VO-8 | Apply pronunciation dictionaries | Could | P4 |

### 4.6 Edit (EDT)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| EDT-1 | Propose a full Edit Spec from a template, the clip analysis and the sentence timings, choosing a shot for each sentence | Must | P1 |
| EDT-2 | Build segments from any of the project's clips; each segment's length is set by a cue: sentences, a word range, a number of seconds, or until the end | Must | P1 |
| EDT-3 | Validate a spec against its schema and rules, return every error with its path and a fix, and in a dry run return the resolved timeline | Must | P1 |
| EDT-4 | Accept a full spec or a patch, and version each save by content hash | Must | P1 |
| EDT-5 | When a clip is too short for its cue, report it and offer: slow down (no slower than 0.8×), hold the last frame, loop, or use another clip | Must | P1 |
| EDT-6 | Reframe to 9:16 by center crop, blurred-background fill, fit with bars or manual; with focus x, focus y and zoom | Must | P1 |
| EDT-7 | Add slow zoom or pan per segment | Should | P1 |
| EDT-8 | Transitions: cut and fade in P1; named crossfade styles later | Should | P1 |
| EDT-9 | Zoom to a point, freeze frame, and speed change per segment | Should | P3 |
| EDT-10 | Blur a region of the source (for example burned-in subtitles the creator may remove) | Could | P3 |
| EDT-11 | A spec stores its template name and version plus its own changes; changing a template re-plans only projects not yet exported, and a dry run shows the differences | Should | P2 |
| EDT-12 | Seamless loop: the end leads back into the start, with no tail | Could | P3 |

### 4.7 Captions and text (CAP)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| CAP-1 | Build captions from the voiceover's word timings, showing the caption text from the markup and overrides | Must | P1 |
| CAP-2 | Caption styles: word-by-word highlight, 1–3 word chunks or full line; font, size, colors, outline, shadow, all caps and position | Must | P1 |
| CAP-3 | Keep captions inside the safe zone and wrap lines automatically | Must | P1 |
| CAP-4 | Show the hook text during the first seconds | Must | P1 |
| CAP-5 | Highlight emphasis words in a second color | Should | P1 |
| CAP-6 | Override the caption text of any word without changing the voice | Must | P1 |
| CAP-7 | Export an .srt caption file | Must | P1 |

### 4.8 Audio (AUD)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| AUD-1 | Mix the voiceover, the original sound (mute, lower or keep) and music | Must | P1 |
| AUD-2 | Lower the original sound and music automatically while the voice speaks | Must | P1 |
| AUD-3 | Normalise the final mix to −14 LUFS with peaks at most −1 dBTP | Must | P1 |
| AUD-4 | Use a music file with a chosen start point, looping and fades | Must | P1 |
| AUD-5 | Provide a music library with mood tags and recorded licenses | Should | P2 |
| AUD-6 | Let the original sound play at full level during a chosen segment | Should | P3 |
| AUD-7 | Add sound effects at cue points | Could | P3 |

### 4.9 Overlays and branding (OVL)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| OVL-1 | Choose the cover frame | Must | P1 |
| OVL-2 | Text overlays cued to sentences, words or seconds, in preset styles such as a numbered list | Should | P2 |
| OVL-3 | Logo or watermark image with position and opacity | Should | P2 |
| OVL-4 | Cover text | Should | P2 |
| OVL-5 | Image overlays from the project's own assets, such as a screenshot card | Should | P2 |
| OVL-6 | Progress bar and end card | Could | P5 |

### 4.10 Rendering (RND) and export (EXP)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| RND-1 | Turn an Edit Spec into FFmpeg commands inside the engine; never accept FFmpeg arguments from Claude | Must | P1 |
| RND-2 | Make one render per spec version: 1080×1920, 30 fps, H.264 High, yuv420p, AAC 48 kHz, fast-start. The reviewed render is the file that is exported | Must | P1 |
| RND-3 | Run renders as background jobs with live progress, and let them be cancelled | Must | P1 |
| RND-4 | Log the exact command, the end of FFmpeg's error output and the time taken | Must | P1 |
| RND-5 | Reuse an existing render when the spec hash and inputs are the same | Should | P1 |
| RND-6 | Use hardware encoding when available, with libx264 as fallback | Should | P4 |
| EXP-1 | On approval, write a package folder `YYYY-MM-DD_NN_slug` holding video.mp4, caption.txt (post caption and hashtags), cover.jpg, captions.srt and project.json | Must | P1 |
| EXP-2 | Remind the creator, in the package and on the review screen, when the post may need TikTok's AI-generated label | Should | P2 |
| EXP-3 | Send the video to the creator's TikTok inbox as a draft | Could | P5 |

### 4.11 Quality checks (QA)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| QA-1 | After every render, run 8 checks: format; duration and tail (video ends 0.3–1.0 s after the last word); loudness; true peak; voice complete (render transcript matches at least 95% of script words); captions in the safe zone; no black or frozen stretch over 0.5 s; no missing caption glyphs | Must | P1 |
| QA-2 | Give each check pass, warn or fail, with the measured value, the limit and a fix that names the spec field to change | Must | P1 |
| QA-3 | Store a contact sheet of every render, shown to Claude and the creator | Must | P1 |
| QA-4 | Marking ready or approving with a failed check needs an override note, which is recorded | Must | P1 |

### 4.12 Batch and recovery (BAT)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| BAT-1 | Run steps (voiceover, plan, render) for many projects as one job, with a result per project; one failure does not stop the others | Must | P1 |
| BAT-2 | A batch stays within the budget unless the creator agreed in chat to go over; that agreement is recorded | Must | P1 |
| BAT-3 | List unfinished projects by state so work can resume; provide a `resume_batch` prompt | Must | P1 |
| BAT-4 | Jobs keep running when Claude Desktop closes and survive an app restart | Must | P1 |

### 4.13 Web app (REV)

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| REV-1 | Dashboard: projects by state, running jobs, today's budget and ElevenLabs quota, a live feed of what Claude is doing in plain words, and a needs-attention list | Must | P1 |
| REV-2 | Project page: player, storyboard (which shot plays under which sentence), the frames Claude checked, QA results, script and spec versions, and job logs | Must | P1 |
| REV-3 | Review queue: approve, reject with a note, approve anyway with a note, and approve all that passed every check | Must | P1 |
| REV-4 | Settings: paste the API key (shown only as its last 4 characters, with Replace key and Save and test); Connect Claude (Claude Desktop config with add and copy buttons, local MCP address and token); folders; voice profiles; defaults; budget; safe-zone margins; renders at once; system check | Must | P1 |
| REV-5 | Live updates without reloading the page | Must | P1 |
| REV-6 | Let the creator edit the script or caption words in the web app, saved as a new version by the creator | Should | P2 |
| REV-7 | Compare two versions side by side | Should | P2 |
| REV-8 | Template editor with a live preview frame | Could | P5 |

### 4.14 Inspiration (INS)

The creator shows Claude what they want with example videos instead of describing it. FVS measures what it can (pacing, speech, sound) and gives Claude pictures for the rest (framing, colour, caption look). Inspiration videos are references only: their footage never appears in a render.

| ID | Requirement | Priority | Phase |
| --- | --- | --- | --- |
| INS-1 | Keep named inspiration sets (for example one per channel), each a read-only folder of example videos the creator likes, with an optional note per video on what to take from it; inspiration videos are analysed but never used as footage | Should | P2 |
| INS-2 | Build a style profile from a set: video length, shot length and cuts per second, time to the first word, hook length, words per minute, sentence length, how videos end (call to action), loudness, and a look sheet of frames from each video; give the median and range across the set, and the value for each video | Should | P2 |
| INS-3 | Return the chosen set's profile and look sheet with the guide, and use its values as targets for script length and fit, planner pacing and shot length | Should | P2 |
| INS-4 | After a render, compare it with the profile and report differences as notes (for example average shot 2.6 s against 1.8 s in the examples); never a failed check | Could | P2 |

## 5. MCP interface (what Claude can do)

Claude gets 25 tools and 4 prompts, and no resources. Every step of UC-1 to UC-6 is a tool call, so Claude never needs a shell, a browser or the ElevenLabs connector. The set follows Anthropic's guidance for agent tools: tools that each do a whole job, a shared `fvs_` prefix, results Claude can act on, and errors that say how to fix them.

### 5.1 What Claude cannot do, and what replaces it

| Limit | Replaced by |
| --- | --- |
| Cannot hear sound | `fvs_listen`: transcripts, word differences from the script, loudness charts as images, silence and clipping lists |
| Cannot watch video | `fvs_frames` (stills at exact times), `fvs_storyboard` (the whole edit as one picture), contact sheets |
| Cannot read MCP resources on its own in Claude Desktop | `fvs_guide` and `fvs_project_get` return everything as tool results |
| Cannot be asked a question by a tool (no elicitation in Claude Desktop) | Claude asks the creator in chat; tools take `allow_over_budget` once the creator agrees |
| Tool calls time out on long work | Jobs: start a render or batch, then wait with `fvs_job_get` |

### 5.2 Tools

Tools that take a clip or project accept its number (`3`) or its ID. Tools marked $ can spend ElevenLabs characters.

| Tool | What it does | Main inputs | Returns | Covers | Phase |
| --- | --- | --- | --- | --- | --- |
| `fvs_guide` | Loads everything Claude needs to start | optional sections | Workflow, script markup, Edit Spec schema, templates, caption styles, voices, budget, quota left, settings summary; from P2 the style guide and inspiration profile | SEE-1, SCR-6, INS-3 | P1 |
| `fvs_library_scan` | Finds and registers clips | folder (default from settings), subfolders | Clips: number, title, length, resolution, has sound, projects using it | LIB-1–2 (LIB-3 in P2) | P1 |
| `fvs_projects_list` | Lists projects and where each stands | state, failed only, needs attention, cursor, `response_format` | Number, title, state, failure flag, stale parts, review notes | BAT-3, REV-3 | P1 |
| `fvs_project_create` | Creates a project | title, clips, series, part, template | Project number and slug | PRJ-1–3 | P1 |
| `fvs_project_get` | Reads a project's full state | project, parts | Script, sentence and word timings, spec, resolved timeline, renders, QA, notes, history | SEE-6 | P1 |
| `fvs_clip_inspect` | Analyses a clip | clip, extras (scenes, transcript, loudness, black and frozen) | Details, data asked for, contact sheet; or a job ID while analysing | ANA-1–7 | P1 |
| `fvs_frames` | Takes "screenshots" | source (clip, render or spec), clip or project, times or range and count, safe zone, captions | Images with their times | SEE-2–3 | P1 |
| `fvs_storyboard` | Shows the whole edit as one picture | project, spec version or draft, or render | Storyboard image and table | SEE-4 | P1 |
| `fvs_listen` | Claude's ears | project or clip, target (voiceover, render, clip audio) | Transcript with times, differences from script, loudness chart, silences, clipping, loudness values | SEE-5 | P1 |
| `fvs_script_save` | Saves a script version | project, title, voiceover with markup, hook, post caption, hashtags, language, notes | Version, numbered sentences, estimated length, fit, warnings | SCR-1–6 | P1 |
| `fvs_voiceover_generate` $ | Makes the voiceover | project, voice, `dry_run`, `allow_over_budget` | Sentence timings, length, fit, characters billed and reused, budget and quota left; or a job ID | VO-1–7 | P1 |
| `fvs_edit_plan` | Proposes an Edit Spec (not saved) | project, template, changes | Proposed spec, notes (for example "clip #7 is 1.2 s short for sentence 4") | EDT-1, EDT-5 | P1 |
| `fvs_edit_save` | Validates and saves an Edit Spec | project, full spec or patch, `dry_run` | Version, hash, resolved timeline, all errors and warnings | EDT-2–12, CAP, AUD, OVL | P1 |
| `fvs_render` | Starts renders | projects | Job IDs | RND-1–5 | P1 |
| `fvs_batch_run` $ | Runs saved steps for many projects as one job | projects, steps (voiceover, plan, render), template, `allow_over_budget` | Batch job ID | BAT-1–2 | P1 |
| `fvs_job_get` | Checks jobs, waiting up to 15 s for a change | job IDs or "active", wait seconds | State, progress, time left, result, error with fix | RND-3, BAT-4 | P1 |
| `fvs_job_cancel` | Stops a job | job ID | Final state | RND-3 | P1 |
| `fvs_project_review` | Moves projects through review | projects (one or many), decision (ready, approve, reject, skip, reopen), note, `override_qa` | New states; for approve, the export package path | PRJ-4, QA-4, REV-3, EXP-1 | P1 |
| `fvs_project_restore` | Makes an earlier version current | project, script or spec version | New current version | PRJ-5 | P1 |
| `fvs_assets_list` | Lists music, fonts, logos, images and sound effects | kind, tag, cursor | ID, name, length, tags, license | AUD-4–5, OVL-3, OVL-5 | P1 |
| `fvs_asset_import` | Adds an asset from an allowed folder | path, kind, name, tags, license | Asset ID | AUD-4, OVL-5, NFR-L1 | P1 |
| `fvs_settings` | Reads or changes settings (never the API key) | settings to change, or none | Current settings | REV-4 | P1 |
| `fvs_template_save` | Creates or changes a template | name, defaults, `dry_run` | Template version, projects affected and their differences | EDT-11 | P2 |
| `fvs_show` | Shows the creator a render or storyboard inside Claude Desktop | project, view (player or storyboard) | MCP App view, plus a web app link | SEE-8 | P2 |
| `fvs_inspiration` | Reads or refreshes an inspiration set's style profile | set (default from settings), refresh, notes per video | Profile (median, range, per video), look sheet; or a job ID while analysing | INS-1–3 | P2 |

### 5.3 Rules every tool must follow

| ID | Rule |
| --- | --- |
| MCP-1 | Everything Claude needs is a tool. Nothing depends on MCP resources, elicitation or audio content |
| MCP-2 | Declare input and output schemas and return structured data plus a one-line summary. Tools that return images (`fvs_frames`, `fvs_storyboard`, `fvs_listen`, `fvs_clip_inspect`) return the images plus their data as JSON text |
| MCP-3 | Return within 20 s. Longer work becomes a job; `fvs_job_get` waits at most 15 s |
| MCP-4 | Keep each result under 1 MB: single images 512 px or smaller, sheets and storyboards 1024 px or smaller (JPEG or WebP); lists paged at 20 with a cursor; `response_format` (concise or detailed) on list, get and inspect tools |
| MCP-5 | On error, set the error flag and return what went wrong, why, how to fix it, and a correct example |
| MCP-6 | Be safe to call twice: the same inputs never bill ElevenLabs twice or start a duplicate job |
| MCP-7 | Mark read-only tools as read-only, and say in the description which tools cost money |
| MCP-8 | Spend over the daily budget only with `allow_over_budget`, which Claude sets after the creator agrees in chat; the agreement is recorded in the activity feed |
| MCP-9 | Never accept paths outside the configured folders, or FFmpeg arguments |
| MCP-10 | Offer two transports: stdio for Claude Desktop, and Streamable HTTP at `127.0.0.1:8787/mcp` for other local MCP apps. The HTTP address requires the session token, checks the Host and Origin headers (rejecting bad ones with 403) and accepts only JSON |
| MCP-11 | Every tool call adds one plain-language line to the activity feed |

### 5.4 Prompts (slash commands in Claude Desktop)

Prompts are shortcuts for the creator. Claude does not depend on them: `fvs_guide` returns the same workflows.

| Prompt | Arguments | What it starts |
| --- | --- | --- |
| `process_folder` | template, how many clips | UC-1 for all new clips |
| `revise_video` | project number, request | UC-2 |
| `review_queue` | none | Reads rejection notes and fixes those projects |
| `resume_batch` | none | UC-6: continues unfinished projects |

## 6. Non-functional requirements

Each requirement has a measurable target. Times assume a 60-second video on an Apple Silicon Mac or a mid-range Windows PC. All are Must and P1 unless the table says otherwise.

| ID | Area | Requirement | Target |
| --- | --- | --- | --- |
| NFR-P1 | Performance | Render time | ≤ 60 s without hardware encoding; ≤ 20 s with it (P4) |
| NFR-P2 | Performance | Still frames from a spec (`fvs_frames`, source spec) | ≤ 3 s for up to 6 frames |
| NFR-P3 | Performance | Storyboard | ≤ 8 s |
| NFR-P4 | Performance | Clip analysis (details, frames, scenes, black and frozen; transcription not included) | ≤ 10 s per minute of video; a job when longer than 15 s |
| NFR-P5 | Performance | Tool response time | Read tools ≤ 2 s (95th percentile); every tool ≤ 20 s |
| NFR-P6 | Performance | Batch of 20 clips, start to In review | ≤ 30 min |
| NFR-R1 | Reliability | Restart safety | Interrupted jobs are queued again once or fail cleanly; outputs are written to a temp file and renamed when complete |
| NFR-R2 | Reliability | Repeatable renders | Same spec hash and inputs give the same output |
| NFR-R3 | Reliability | External failures | ElevenLabs errors never lose a script; busy and server errors retried up to 3 times with backoff |
| NFR-S1 | Security | Network exposure | Core, web app and `/mcp` listen on 127.0.0.1 only; every request needs the session token; the Host and Origin headers are checked and bad ones refused with 403 |
| NFR-S2 | Security | Secrets | The API key is pasted in Settings and saved in `~/.fvs/config.toml`, readable only by the user; the core never sends it back (the web app shows only its last 4 characters); never in tool results or logs |
| NFR-S3 | Security | File access | Only the configured folders, plus Claude Desktop's config file when the creator clicks Add to Claude Desktop; path traversal and links leading outside are refused |
| NFR-S4 | Security | Command safety | FFmpeg started with an argument list, never through a shell; all text escaped for subtitle and filter syntax |
| NFR-C1 | Cost | Voice spending | Daily character budget (default 20,000) with a ledger of every call |
| NFR-C2 | Cost | No double charges | Unchanged sentences served from cache |
| NFR-U1 | Usability | Creator effort | A batch of 10 clips needs at most 3 creator messages: start, agree to cost if asked, approve all that passed |
| NFR-U2 | Usability | Messages | Every error shown to Claude or the creator says what to do next |
| NFR-A1 | Agent fitness | Claude completes realistic tasks | At least 13 of 15 evaluation tasks done without help (AC-15) |
| NFR-M1 | Maintainability | Schema changes | Edit Spec carries a schema version; old specs are migrated automatically |
| NFR-M2 | Maintainability | Extending | A new edit feature means one new schema field and one compiler stage change |
| NFR-O1 | Observability | Logs | Structured logs; each job keeps its commands, timings and errors; spending visible in the web app |
| NFR-X1 | Portability | Operating systems | macOS and Windows from one codebase; fonts bundled with the app, nothing loaded from the internet |
| NFR-L1 | Licensing | Assets | Each font, music track, image and sound effect records its license; assets without one cannot be used in a render |

## 7. Acceptance criteria

P1 is accepted when all 15 checks below pass on a fixed test set: a landscape clip, a portrait clip, a clip with no sound, a 4K clip, a variable-frame-rate phone clip, an 8-second clip and a 3-minute clip.

| ID | Criterion | How it is checked | Traces to |
| --- | --- | --- | --- |
| AC-1 | `process_folder` turns a folder of 10 clips, including the 7 test clips, into 10 projects In review, with no manual editing | End-to-end run in Claude Desktop | UC-1, PRJ-2, BAT-1 |
| AC-2 | Every exported file is 1080×1920, 30 fps, H.264 High, yuv420p, AAC 48 kHz, with fast-start, and its package holds the chosen cover frame | ffprobe on every output; cover image present | RND-2, OVL-1 |
| AC-3 | Integrated loudness is −14 ± 1 LUFS and true peak is at most −1 dBTP | FFmpeg ebur128 measurement | AUD-3, QA-1 |
| AC-4 | Caption words appear within 100 ms of the spoken word, and every caption sits inside the safe zone | 20 sampled words against the timings; caption boxes against the safe-zone rectangle | CAP-1, CAP-3 |
| AC-5 | Original sound and music drop by at least 10 dB while the voice speaks | Loudness in voice and non-voice windows | AUD-2 |
| AC-6 | A segment cued to a sentence starts within one frame of that sentence | 20 sampled cues against the resolved timeline and the render | EDT-2, VO-1 |
| AC-7 | The video ends 0.3–1.0 s after the last word, and the render transcript matches at least 95% of the script words | QA report on every output | QA-1 |
| AC-8 | An unchanged script bills 0 characters; changing one sentence bills only that sentence; an over-budget call without `allow_over_budget` is refused | Cost ledger | VO-4, VO-6, MCP-8 |
| AC-9 | Paths outside the folders and FFmpeg flags are refused; the input folder is byte-for-byte unchanged; `/mcp` without the token returns 401 and a foreign Origin returns 403; the API key never appears in a tool result, API response or log | Negative tests, checksums, log search | LIB-2, MCP-9, MCP-10, NFR-S1, NFR-S2 |
| AC-10 | Killing the app mid-render leaves no broken file and the job is queued again or fails cleanly; closing Claude Desktop mid-batch does not stop the batch; `resume_batch` finishes the rest | Fault-injection tests | NFR-R1, BAT-3, BAT-4 |
| AC-11 | Every tool result in the AC-1 run is under 1 MB and returned within 20 s | Tool-call log | MCP-3, MCP-4 |
| AC-12 | A revision creates new versions and an old one can be restored; a caption override changes the captions without new voice characters | Manual test and cost ledger | PRJ-5, CAP-6 |
| AC-13 | Frames come from the requested times within one frame; a spec still matches the render at the same time; the storyboard lists every segment with its sentence; `fvs_listen` flags a sentence removed on purpose | Automated comparisons | SEE-2, SEE-4, SEE-5 |
| AC-14 | One project is built from 3 clips, and 3 projects are built from one clip with different time ranges | Manual test | PRJ-1–3, UC-4 |
| AC-15 | Claude completes at least 13 of 15 realistic tasks without help (for example, "put the drone shot under sentence 3 and zoom in at 0:07") | Scripted evaluation; transcripts reviewed to improve tool descriptions | NFR-A1 |

P2 to P5 requirements are checked at their own phase gates (Design Specification, section 11).

## 8. Verification and traceability

Each requirement group is verified by one main method: Test (automated), Demonstration (shown working), Inspection (review of code or output) or Analysis (measurement). The matrix links each group to its design, tools and checks.

| Group | Method | Design (Design Specification) | Tools | Checked by |
| --- | --- | --- | --- | --- |
| LIB, PRJ | Test | Library, data model (6), lifecycle (7) | `fvs_library_scan`, `fvs_project_create`, `fvs_project_review`, `fvs_project_restore` | AC-9, AC-12, AC-14 |
| ANA | Test | Analyzer | `fvs_clip_inspect` | AC-1, media tests |
| SEE | Test | Perception service | `fvs_guide`, `fvs_frames`, `fvs_storyboard`, `fvs_listen`, `fvs_project_get`, `fvs_show` | AC-11, AC-13 |
| SCR | Test | Script service | `fvs_script_save` | AC-12, unit tests |
| VO | Test | Voice service | `fvs_voiceover_generate`, `fvs_batch_run` | AC-6, AC-8 |
| EDT | Test | Planner, Validator, Compiler | `fvs_edit_plan`, `fvs_edit_save`, `fvs_template_save` | AC-6, AC-14, compiler tests |
| CAP | Test | Compiler, captions stage | `fvs_edit_save` | AC-4, AC-12 |
| AUD | Analysis | Compiler, audio stage | `fvs_edit_save`, `fvs_asset_import` | AC-3, AC-5 |
| OVL | Test | Compiler, overlay stage | `fvs_edit_save`, `fvs_asset_import` | OVL-1 by AC-2 at the P1 gate; the rest by media tests at the P2 gate |
| RND, EXP | Test | Renderer, Exporter | `fvs_render`, `fvs_project_review` | AC-2, AC-10 |
| QA | Test | QA service | `fvs_job_get`, `fvs_project_get` | AC-3, AC-7 |
| BAT | Demonstration | Job queue | `fvs_batch_run`, `fvs_job_get`, `fvs_projects_list` | AC-1, AC-10 |
| REV | Demonstration | Web app | none | One walkthrough per screen at each gate |
| INS | Test | Analyzer, Planner, QA service | `fvs_inspiration`, `fvs_guide`, `fvs_edit_plan` | Media tests at the P2 gate |
| MCP rules | Test | MCP adapter | all | AC-9, AC-11, contract tests |
| NFR | Analysis | Sections 10 and 11 of the design | all | Measured in the media and contract tests |

## 9. Risks, open questions and sources

### 9.1 Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| TikTok keeps unoriginal content with no new or creative changes out of the For You feed, and repeated cases can affect the whole account | Low reach | An original script per video, real editing, narration-timed shots, footage the creator owns or has licensed |
| TikTok requires a label on realistic AI-generated content | Content flagged | EXP-2 reminder |
| Sentence-by-sentence voice can sound joined up wrongly between sentences | Lower quality | Request stitching (VO-2); not available on Eleven v3, so the default voice profile uses a model that supports it |
| ElevenLabs spending runs over | Cost | Daily budget, dry runs, sentence cache, quota shown from the subscription endpoint |
| Claude Desktop limits: results around 1 MB, calls reported to time out near 45 s | Failed calls | 20 s tool limit, jobs, small images (MCP-3, MCP-4) |
| MCP clients differ: Claude Desktop reads resources only when attached, has no elicitation, and MCP Apps rendering has open bug reports | Claude misses information; the in-chat view fails | Tools only (MCP-1); questions in chat; web app link as fallback (SEE-8) |
| A local HTTP MCP address can be reached by web pages through DNS rebinding | Tools called by a malicious page | Token, Host and Origin checks (MCP-10, NFR-S1) |
| MCP Tasks is experimental and moving to an extension | Rework | Our own job tools |
| FFmpeg builds differ (libass, hardware encoders) | Renders fail | Start-up self-check naming what is missing |
| Fonts, music or images without commercial rights | Takedowns | License recorded per asset; unlicensed assets refused (NFR-L1) |

### 9.2 Open questions

- [ ] Mac or Windows first? This decides the hardware encoder and how the app is packaged.
- [ ] Target length per video (15, 30 or 60 s)?
- [ ] Daily ElevenLabs character budget and plan?
- [ ] Is TikTok draft upload (EXP-3) wanted? It needs a TikTok developer app, and the API allows at most 5 pending uploads per 24 hours.
- [x] Languages for v1: languages written with spaces between words (SCR-5). Decided in version 1.1.

### 9.3 Decisions taken in version 1.1

- One render per version, reviewed and exported as is (no separate preview).
- Narration-timed edits and projects built from several clips are in P1; two-voice scripts and sound effects are in P3.
- Spending over budget is allowed by a flag after the creator agrees in chat, instead of confirmation tokens.
- Learning each voice's speed was dropped: the real length replaces the estimate after the first voiceover.

### 9.4 Sources

- [ISO/IEC/IEEE 29148:2018 — Requirements engineering](https://www.iso.org/standard/72089.html)
- [MCP 2025-11-25 changelog](https://modelcontextprotocol.io/specification/2025-11-25/changelog.md), [Tasks (experimental)](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks.md), [SEP-2663: Tasks moved to an extension](https://modelcontextprotocol.io/seps/2663-tasks-extension)
- [MCP TypeScript SDK advisory: DNS rebinding on localhost HTTP servers](https://github.com/modelcontextprotocol/typescript-sdk/security/advisories/GHSA-w48q-cv73-mx4w), [CSA research note on MCP transport trust](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-transport-trust-gap-20261109-csa-style/), [MCP Python SDK transport security](https://py.sdk.modelcontextprotocol.io/v2/api/mcp/server/transport_security/)
- [MCP Apps overview](https://modelcontextprotocol.io/docs/extensions/apps), [MCP Apps announcement](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/), [MCP Apps rendering bug report](https://github.com/modelcontextprotocol/ext-apps/issues/671)
- [MCP resources support research (Claude Desktop: manual attach only)](https://glama.ai/mcp/servers/@DollhouseMCP/DollhouseMCP/blob/62c9b46e851b3753b28f2b916e6379c465abb9d3/docs/development/MCP_RESOURCES_SUPPORT_RESEARCH_2025-10-16.md), [Elicitation request for Claude Desktop](https://claudeissues.com/issue/41347-feature-mcp-elicitation-support-in-claude-desktop-app)
- [Claude and audio files](https://iamtypist.dev/blog/can-claude-transcribe-audio), [Client limits for MCP image results](https://pvliesdonk.github.io/image-generation-mcp/guides/client-compatibility) and [tool timeouts](https://pvliesdonk.github.io/image-generation-mcp/tools/)
- [Anthropic — Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- ElevenLabs: [Create speech with timing](https://elevenlabs.io/docs/api-reference/text-to-speech-with-timestamps), [Request stitching](https://elevenlabs.io/docs/eleven-api/guides/how-to/text-to-speech/request-stitching), [How to add pauses](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/how-can-i-add-pauses), [API errors 400 and 401](https://elevenlabs.io/docs/help-center/technical/api-error-code-400-or-401), [Error messages (429)](https://elevenlabs.io/docs/product/troubleshooting/error-messages), [Get user subscription](https://elevenlabs.io/docs/api-reference/user/subscription/get)
- [FFmpeg filters documentation](https://ffmpeg.org/ffmpeg-filters.html), [sidechaincompress](https://ayosec.github.io/ffmpeg-filters-docs/8.0/Filters/Audio/sidechaincompress.html)
- [TikTok — Integrity and authenticity guidelines](https://www.tiktok.com/community-guidelines/integrity-authenticity/), [Accounts and features](https://www.tiktok.com/community-guidelines/en/accounts-features), [About AI-generated content](https://support.tiktok.com/en/using-tiktok/creating-videos/ai-generated-content)
- [TikTok Content Posting API — Upload](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video)
- [Vertical video safe zones 2026](https://adaptlypost.com/blog/social-media-safe-zones-2026-complete-guide)
- Prior art: [mcp-video](https://pypi.org/project/mcp-video/1.3.0/), an open-source FFmpeg MCP server
