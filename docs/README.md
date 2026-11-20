# Faceless Video Studio — design package (v1.1)

Local app + MCP server that lets Claude turn a folder of clips into finished TikTok videos
(script → ElevenLabs voice → FFmpeg edit → Claude's own check → your review → export).

## What's in this folder

| File | What it is |
|---|---|
| `Faceless Video Studio - Functional Specification v1.1.pdf` | What the system must do: use cases, 99 requirements, the 24 MCP tools, acceptance criteria AC-1 to AC-15 (ISO/IEC/IEEE 29148 layout) |
| `Faceless Video Studio - Design Specification v1.1.pdf` | How it is built: architecture, decisions (ADRs), components, Edit Spec format, data model, lifecycle, MCP server design, test strategy and roadmap (IEEE 1016 viewpoints) |
| `Faceless Video Studio - Functional Specification v1.1.md` | **Same spec as text, for coding tools (Claude, Claude Code).** Tables are intact, so every requirement's priority and phase are exact |
| `Faceless Video Studio - Design Specification v1.1.md` | **Same design spec as text.** The 4 diagrams are written out as Mermaid (architecture, lifecycle with all 10 states and transitions, the UC-1 sequence) and a table (roadmap) |
| `requirements.csv` | All 99 requirements: ID, requirement, priority (Must/Should/Could), phase (P1–P5) |
| `mockups/` | The four web app screens (Dashboard, Project, Review queue, Settings), each as a picture (`.png`) and as a page you can open in your browser (`.html`, the menu links between them work) |
| `screens/` | The design-canvas source of the same screens, kept as a reference for building the real UI (these don't open on their own; use `mockups/`) |

## Live versions (always the latest)

- Functional Specification: https://claude.ai/artifact/WN4FB6rqt3QSCPTTfFdmA4
- Design Specification: https://claude.ai/artifact/LpCbmcGzkdjhw5Kgu53oNN
- Web app screens: https://claude.ai/artifact/TMtG5tNepjfJy1kFPGP4d4

The docs can also be exported to Word from the doc's Export menu.

## Still to decide before building

1. Operating system to target first (this folder is on Windows).
2. Typical video length.
3. Daily ElevenLabs character budget.
4. Upload approved videos to TikTok as drafts, or only save them to a folder.

## For a coding assistant

Use the two `.md` files and `requirements.csv` as the source of truth, not the PDFs (text pulled out of a PDF mixes up table columns, and diagrams in a PDF are only pictures). Section 7 of the Design Specification has the full lifecycle; section 11.2 has the roadmap.
