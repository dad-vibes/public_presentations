#!/usr/bin/env python3
"""
Concept-art batch generator for the LLM Team Wiki presentation.

Generates concept art with Google's "Nano Banana 2" model
(Gemini 3.1 Flash Image, model id: gemini-3.1-flash-image) via the
Gemini REST API.

For every (style x concept x definition) combination it writes one PNG:

    images/concepts/<style_slug>/<concept_slug>__<definition_slug>.png   (folder-per-style)

The concepts + definitions + styles all come from a JSON config
(default: tools/concepts.json) so the prompts live as data, not code.

Quick start
-----------
    export GEMINI_API_KEY=...                       # never commit this
    python3 tools/generate_concept_art.py --dry-run # preview every prompt + path, no API calls
    python3 tools/generate_concept_art.py --first   # generate ONLY the first concept/definition
                                                    #   across all styles (the validation pass)
    python3 tools/generate_concept_art.py           # full batch (resumable; skips files that exist)

Useful flags
------------
    --config PATH     concept/style config (default: tools/concepts.json)
    --out DIR         output root (default: images/concepts)
    --model ID        override model id (default: gemini-3.1-flash-image)
    --aspect RATIO    aspect ratio, e.g. 16:9 (good for slides), 1:1, 4:3 (default: 16:9)
    --size SIZE       image resolution: 1K | 2K | 4K (default: 2K)
    --style NAME      restrict to one style (repeatable)
    --concept NAME    restrict to one concept (repeatable)
    --first           only the first concept's first definition, across all styles
    --limit N         stop after N generated images
    --force           regenerate even if the output file already exists
    --dry-run         print prompts + paths, make no API calls
    --delay SECONDS   pause between calls for rate limiting (default: 2.0)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "tools" / "concepts.json"
DEFAULT_OUT = REPO_ROOT / "images" / "concepts"
DEFAULT_MODEL = "gemini-3.1-flash-image"  # Nano Banana 2
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# How many times to retry a single image on transient (429 / 5xx) errors.
MAX_RETRIES = 4


def slugify(text: str) -> str:
    """Filesystem-safe lower-case slug."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


def load_api_key() -> str:
    """Read the key from the environment, falling back to a local .env file."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY"):
                _, _, val = line.partition("=")
                return val.strip().strip('"').strip("'")
    sys.exit(
        "ERROR: GEMINI_API_KEY is not set.\n"
        "  Set it for this session:  export GEMINI_API_KEY=your_key_here\n"
        "  Or drop it in a .env file at the repo root (it is git-ignored)."
    )


def build_prompt(style: dict, concept: dict, definition: dict, global_guidance: str) -> str:
    """Compose the text prompt sent to the model for one image."""
    parts = [
        style["prompt"].strip(),
        f'Subject: "{concept["name"]}" — {definition["name"]}.',
        definition.get("description", "").strip(),
    ]
    if global_guidance.strip():
        parts.append(global_guidance.strip())
    return " ".join(p for p in parts if p)


def generate_image(prompt: str, *, api_key: str, model: str, aspect: str, size: str) -> bytes:
    """Call the Gemini image API once and return the decoded PNG bytes."""
    url = f"{API_BASE}/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect, "imageSize": size},
        },
    }

    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.post(url, headers=headers, json=body, timeout=180)
        if resp.status_code == 200:
            return _extract_image_bytes(resp.json())
        # Retry on rate-limit / transient server errors with exponential backoff.
        if resp.status_code in (429, 500, 503) and attempt < MAX_RETRIES:
            wait = delay * (2 ** (attempt - 1))
            print(f"    -> HTTP {resp.status_code}, retry {attempt}/{MAX_RETRIES} in {wait:.0f}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"API error HTTP {resp.status_code}: {resp.text[:600]}")
    raise RuntimeError("Exhausted retries")


def _extract_image_bytes(payload: dict) -> bytes:
    """Pull the first inline image out of a generateContent response."""
    candidates = payload.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"No candidates in response: {json.dumps(payload)[:600]}")
    for part in candidates[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError(f"No image in response: {json.dumps(payload)[:600]}")


def iter_jobs(config: dict, args) -> list[dict]:
    """Expand the config into the flat list of images to generate, honoring filters."""
    styles = config["styles"]
    concepts = config["concepts"]

    wanted_styles = set(s.lower() for s in args.style) if args.style else None
    wanted_concepts = set(c.lower() for c in args.concept) if args.concept else None

    if args.first:
        concepts = concepts[:1]
        if concepts:
            concepts = [{**concepts[0], "definitions": concepts[0]["definitions"][:1]}]

    jobs = []
    for style_name, style in styles.items():
        if wanted_styles and style_name.lower() not in wanted_styles:
            continue
        for concept in concepts:
            if wanted_concepts and concept["name"].lower() not in wanted_concepts:
                continue
            for definition in concept["definitions"]:
                jobs.append(
                    {
                        "style_name": style_name,
                        "style": style,
                        "concept": concept,
                        "definition": definition,
                    }
                )
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch concept-art generator (Gemini / Nano Banana 2).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--aspect", default="16:9")
    parser.add_argument("--size", default="2K", choices=["1K", "2K", "4K"])
    parser.add_argument("--style", action="append", default=[])
    parser.add_argument("--concept", action="append", default=[])
    parser.add_argument("--first", action="store_true", help="Only the first concept/definition, all styles")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    if not args.config.exists():
        sys.exit(f"ERROR: config not found: {args.config}")
    config = json.loads(args.config.read_text())
    global_guidance = config.get("global_guidance", "")

    jobs = iter_jobs(config, args)
    if not jobs:
        sys.exit("No jobs to run (check your --style/--concept filters and the config).")

    api_key = None if args.dry_run else load_api_key()

    print(f"Model: {args.model}   Size: {args.size}   Aspect: {args.aspect}")
    print(f"Planned images: {len(jobs)}{'  (DRY RUN)' if args.dry_run else ''}\n")

    generated = skipped = failed = 0
    for i, job in enumerate(jobs, 1):
        style_slug = slugify(job["style_name"])
        out_dir = args.out / style_slug
        fname = f'{slugify(job["concept"]["name"])}__{slugify(job["definition"]["name"])}.png'
        out_path = out_dir / fname
        prompt = build_prompt(job["style"], job["concept"], job["definition"], global_guidance)

        rel = out_path.relative_to(REPO_ROOT)
        print(f"[{i}/{len(jobs)}] {rel}")
        print(f"    prompt: {prompt}")

        if args.dry_run:
            continue
        if out_path.exists() and not args.force:
            print("    -> exists, skipping")
            skipped += 1
            continue

        try:
            img = generate_image(
                prompt, api_key=api_key, model=args.model, aspect=args.aspect, size=args.size
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(img)
            generated += 1
            print(f"    -> saved ({len(img) // 1024} KB)")
        except Exception as exc:  # noqa: BLE001 - keep the batch going, report at the end
            failed += 1
            print(f"    -> FAILED: {exc}")

        if args.limit and generated >= args.limit:
            print(f"\nReached --limit {args.limit}, stopping.")
            break
        if not args.dry_run and i < len(jobs):
            time.sleep(args.delay)

    if not args.dry_run:
        print(f"\nDone. generated={generated} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
