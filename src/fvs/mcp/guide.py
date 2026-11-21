"""What ``fvs_guide`` returns besides live data: the workflow and the script markup (SEE-1).

Kept as plain text so Claude reads it the way a person would read a short manual. The
same workflows back the four prompts (FS 5.4).
"""

from __future__ import annotations

WORKFLOW = """\
You turn the creator's clips into finished TikTok videos (9:16, 1080x1920, about 20-60 s) \
with a voiceover, captions and sound. FVS does the editing; you make the decisions and \
check the result. You cannot watch or hear video yourself: look with fvs_frames and \
fvs_storyboard, listen with fvs_listen.

One video (a "project"), about 14 calls:
1. fvs_library_scan, then fvs_project_create (one project per clip by default; several \
clips for a compilation; series and part for a multi-part series).
2. fvs_clip_inspect each clip (details, scenes, speech, contact sheet); fvs_frames for \
any moment you want to see closely. If the guide names an inspiration set (the creator's \
example videos), call fvs_inspiration and match its length, pace, hooks and look.
3. Write the voiceover, one sentence per line (markup below), and save it with \
fvs_script_save. It returns numbered sentences and whether the length fits.
4. fvs_voiceover_generate with dry_run true to see the cost. COSTS MONEY when not a dry \
run. If it says BUDGET_EXCEEDED, ask the creator in chat; only after they agree, call \
again with allow_over_budget true.
5. fvs_voiceover_generate. Every sentence gets start and end times.
6. fvs_edit_plan proposes which shot plays under each sentence. Look at it with \
fvs_storyboard (pass the proposed spec as a draft), adjust, then fvs_edit_save.
7. fvs_render, then fvs_job_get until it is done (it waits up to 15 s per call).
8. Claude check: the render result has a QA report (8 checks). Look at key moments with \
fvs_frames (source render, safe_zone true) and listen with fvs_listen (target render). \
Fix what is wrong (a new spec or script version) and render again.
9. When it is right, the project goes to the creator's review queue. The creator \
approves or rejects with a note; a rejection shows up in fvs_projects_list.

Revisions: read the project with fvs_project_get. A caption typo is a caption override \
in the Edit Spec (captions.overrides), not a new voiceover. A script change makes a new \
script version; only changed sentences are sent to ElevenLabs again.

Rules:
- Times in the Edit Spec are cues to sentences and words, not seconds: the voiceover \
sets the timeline. Use clip time ranges ("in") only to pick the part of a clip.
- Never invent clip numbers, voices, templates or assets: use the ones listed here and \
in fvs_library_scan.
- Every error tells you what went wrong and how to fix it; follow the fix.
- Long work (renders, long voiceovers, uncached clip analysis) returns a job ID; wait \
with fvs_job_get.
"""

MARKUP = """\
One sentence per line. A line with several sentences is split at . ! ? and the ellipsis.
- [pause 0.6]   silence after the sentence, 0.1-3 s (ends the sentence there).
- {caption|spoken}   show one text, say another, e.g. {1,000|one thousand}.
- *word*   emphasis: shown in the caption style's emphasis colour.
The hook (up to 8 words) is shown on screen at the start; the post caption and hashtags \
go into the export, not the video.

Example:
This volcano makes its own lightning.
Ash rubs together and builds a *huge* electric charge. [pause 0.4]
It can reach {100,000|a hundred thousand} volts.
"""

PROMPTS = {
    "process_folder": {
        "description": "Make videos from all new clips in the input folder (UC-1).",
        "arguments": [
            {"name": "template", "description": "Template to use, e.g. storytime_dark",
             "required": False},
            {"name": "how_many", "description": "How many clips to process (default all)",
             "required": False}],
        "text": "Process the new clips in my FVS input folder{how_many_text}. Start with "
                "fvs_guide, then scan the library and make one video per new clip"
                "{template_text}. Check the cost with a dry run before making voiceovers "
                "and ask me if it goes over today's budget. Look at each render before "
                "putting it in my review queue.",
    },
    "revise_video": {
        "description": "Change one video (UC-2).",
        "arguments": [
            {"name": "project", "description": "Project number, e.g. 3", "required": True},
            {"name": "request", "description": "What to change", "required": True}],
        "text": "Revise FVS project #{project}: {request}. Read it with fvs_project_get "
                "first. Fix caption typos with caption overrides rather than a new "
                "voiceover. Render, check the result, and put it back in my review queue.",
    },
    "review_queue": {
        "description": "Act on the notes on rejected videos.",
        "arguments": [],
        "text": "Look at my FVS projects with changes requested (fvs_projects_list), read "
                "my notes, fix each one, render, check, and put them back in the review "
                "queue.",
    },
    "resume_batch": {
        "description": "Continue unfinished videos after a stop or an error (UC-6).",
        "arguments": [],
        "text": "Continue my unfinished FVS videos. List projects by state with "
                "fvs_projects_list, look at any that failed and why, and pick up each one "
                "where it stopped. Finished work stays as it is.",
    },
}


def prompt_text(name: str, arguments: dict[str, str]) -> str:
    p = PROMPTS[name]
    args = {a["name"]: (arguments.get(a["name"]) or "").strip() for a in p["arguments"]}
    if name == "process_folder":
        args["how_many_text"] = f" (the first {args['how_many']})" if args["how_many"] \
            else ""
        args["template_text"] = f" with the {args['template']} template" \
            if args["template"] else ""
    return p["text"].format(**args)
