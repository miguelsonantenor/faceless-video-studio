"""Templates and caption styles (DS 6.1; EDT-11 editing arrives in P2).

A template is a partial Edit Spec (``spec``) plus hints for the planner (``plan``). P1
ships two read-only templates and the named caption styles they use; templates are
referred to as ``name`` (latest version) or ``name@version``.
"""

from __future__ import annotations

import json
from typing import Any

from fvs.errors import ErrorCode, FvsError
from fvs.store import Database
from fvs.timeutil import now_iso

BUILT_IN_TEMPLATES: dict[str, dict[str, Any]] = {
    "storytime_dark": {
        "description": "Fast story narration: blurred fill, slow zooms, bold yellow "
                       "word-by-word captions, original sound ducked under the voice.",
        "spec": {
            "reframe": {"mode": "blur_fill"},
            "audio": {"original": {"mode": "duck", "duck_db": -14}},
            "captions": {"style": "bold_yellow", "mode": "word_highlight", "max_words": 3,
                         "position": "lower_third"},
            "hook": {"seconds": 2.5},
            "end": {"tail_seconds": 0.6},
        },
        "plan": {"max_shot_seconds": 3.5, "motion": ["zoom_in", "none", "zoom_out", "none"],
                 "transition": "cut"},
    },
    "clean_light": {
        "description": "Calm explainer: centre crop, gentle pans, white two-word chunks, "
                       "soft fades between shots.",
        "spec": {
            "reframe": {"mode": "center_crop"},
            "audio": {"original": {"mode": "duck", "duck_db": -18}},
            "captions": {"style": "clean_white", "mode": "chunks", "max_words": 2,
                         "position": "center"},
            "hook": {"seconds": 2.0},
            "end": {"tail_seconds": 0.8},
        },
        "plan": {"max_shot_seconds": 5.0, "motion": ["pan_right", "none", "pan_left", "none"],
                 "transition": {"type": "fade", "seconds": 0.3}},
    },
}

BUILT_IN_TEMPLATES["perfume_review"] = {
    "description": "Hand-held fragrance review, like the creator's own videos: one "
                   "continuous shot of the bottle, a red hook banner at the top for 5 s, "
                   "no running captions, the voice over the clip's muted sound.",
    "spec": {
        "reframe": {"mode": "center_crop"},
        "audio": {"original": {"mode": "mute"}},
        "captions": {"style": "red_banner", "mode": "off", "max_words": 3,
                     "position": "upper_third"},
        "hook": {"seconds": 5.0},
        "end": {"tail_seconds": 0.8},
    },
    "plan": {"max_shot_seconds": 6.0, "motion": ["none"], "transition": "cut"},
}

# Named caption styles (CAP-2). Fonts are bundled with the app (NFR-X1, WP4).
CAPTION_STYLES: dict[str, dict[str, Any]] = {
    "bold_yellow": {"font": "Montserrat", "weight": 900, "size": 86, "all_caps": True,
                    "color": "#FFFFFF", "highlight_color": "#FFE14D",
                    "emphasis_color": "#FF5A4D", "outline": 6, "outline_color": "#000000",
                    "shadow": 2},
    "red_banner": {"font": "Inter", "weight": 700, "size": 56, "all_caps": False,
                   "color": "#FFFFFF", "highlight_color": "#FFFFFF",
                   "emphasis_color": "#FFE14D", "outline": 3, "outline_color": "#000000",
                   "shadow": 0, "hook_box": "#E3262B"},
    "clean_white": {"font": "Inter", "weight": 700, "size": 72, "all_caps": False,
                    "color": "#FFFFFF", "highlight_color": "#FFFFFF",
                    "emphasis_color": "#7FD1FF", "outline": 3, "outline_color": "#1A1A1A",
                    "shadow": 1},
}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Templates:
    def __init__(self, db: Database) -> None:
        self.db = db

    def seed(self) -> None:
        at = now_iso()
        with self.db.transaction() as conn:
            for name, t in BUILT_IN_TEMPLATES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO templates (name, version, description, defaults_json, "
                    "read_only, created_at) VALUES (?, 1, ?, ?, 1, ?)",
                    (name, t["description"], json.dumps({"spec": t["spec"], "plan": t["plan"]}),
                     at))
            for name, style in CAPTION_STYLES.items():
                conn.execute("INSERT OR IGNORE INTO caption_styles (name, style_json) "
                             "VALUES (?, ?)", (name, json.dumps(style)))

    def get(self, ref: str) -> dict[str, Any]:
        """``name`` or ``name@version`` → {name, version, ref, description, spec, plan}."""
        name, _, ver = ref.partition("@")
        with self.db.connect() as conn:
            if ver.isdigit():
                row = conn.execute("SELECT * FROM templates WHERE name = ? AND version = ?",
                                   (name, int(ver))).fetchone()
            else:
                row = conn.execute("SELECT * FROM templates WHERE name = ? "
                                   "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            raise FvsError(ErrorCode.NOT_FOUND, f"There is no template {ref!r}.",
                           f"Use one of: {', '.join(t['name'] for t in self.list())}.")
        defaults = json.loads(row["defaults_json"])
        return {"name": row["name"], "version": row["version"],
                "ref": f"{row['name']}@{row['version']}", "description": row["description"],
                "spec": defaults["spec"], "plan": defaults["plan"]}

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT name, MAX(version) AS version, description "
                                "FROM templates GROUP BY name ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def caption_styles(self) -> dict[str, dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM caption_styles ORDER BY name").fetchall()
        return {r["name"]: json.loads(r["style_json"]) for r in rows}
