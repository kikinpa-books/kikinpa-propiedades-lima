# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture: WAT Framework

This repo uses the **WAT (Workflows, Agents, Tools)** architecture — a three-layer system where probabilistic AI handles reasoning and deterministic scripts handle execution.

- **`workflows/`** — Markdown SOPs defining objectives, required inputs, which tools to use, expected outputs, and edge case handling. These are the source of truth for how tasks should be done.
- **`tools/`** — Python scripts that do the actual work (API calls, data transformations, file I/O, database queries). These are deterministic and reusable.
- **`.tmp/`** — Disposable intermediate files generated during processing. Never treat these as persistent.
- **`.env`** — All API keys and credentials. Never store secrets anywhere else.
- **`credentials.json`, `token.json`** — Google OAuth files (gitignored).

Final outputs go to cloud services (Google Sheets, Slides, etc.), not local files.

## How to Operate

**Before building anything**, check `tools/` for an existing script that does the task. Only create new tools when nothing exists.

**To execute a task:**
1. Read the relevant workflow in `workflows/`
2. Identify required inputs and the correct tool sequence
3. Run the tools — never try to replicate what a tool does directly

**When a tool fails:**
1. Read the full error and trace
2. Fix the script and retest — if it uses paid API calls, confirm before re-running
3. Update the workflow with what you learned (rate limits, quirks, better approaches)

**Workflows are living documents.** Update them when you discover better methods or constraints. Do not create or overwrite workflows without being explicitly asked to.

## Running Tools

Tools are Python scripts run directly:

```
python tools/<script_name>.py
```

Credentials are loaded from `.env` at runtime. Ensure `.env` is populated before running any tool that makes external API calls.
