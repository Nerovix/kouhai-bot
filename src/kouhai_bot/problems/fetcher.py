#!/usr/bin/env python3
"""
cf_statement.py — Fetch CF problem statement, handle formula images via VL.

Usage:
  python cf_statement.py 542 D                    # contestId + index
  python cf_statement.py 542D                     # combined pid
  python cf_statement.py --url https://codeforces.com/problemset/problem/542/D

VL backends (set via --vl-backend or CF_VL_BACKEND env):
  - qwen     Qwen-VL API (needs QWEN_API_KEY, QWEN_BASE_URL)
  - openai   OpenAI-compatible VL (needs OPENAI_API_KEY, OPENAI_BASE_URL)
  - none     Dry-run only

Returns:
  - has_non_formula_images: True if problem contains tex-graphics diagrams
  - graphics_details: VL descriptions for tex-graphics diagrams when available
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from io import BytesIO

import cloudscraper
from PIL import Image

# ── Config ──────────────────────────────────────────────────────────────

DEFAULT_VL_BACKEND = os.environ.get("CF_VL_BACKEND", "none")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "").strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

_scraper = None

# Hallucination patterns — if VL output matches these, it's likely garbage
_HALLUCINATION_PATTERNS = [
    r"\\documentclass",
    r"\\usepackage",
    r"\\begin\{document\}",
    r"\\begin\{tikzpicture\}",
    r"\\usetikzlibrary",
]

# Max reasonable LaTeX length for a single CF formula
_MAX_FORMULA_LENGTH = 400

# Context window size (chars) to extract around each formula image
_CONTEXT_WINDOW = 300


def _qwen_config() -> tuple[str, str, str]:
    api_key = QWEN_API_KEY
    base_url = QWEN_BASE_URL
    model = QWEN_MODEL

    try:
        from ..config import get_config

        cfg = get_config()
        api_key = api_key or cfg.qwen_api_key
        base_url = os.environ.get("QWEN_BASE_URL", "").strip() or cfg.qwen_base_url
        model = model or cfg.qwen_model
    except Exception:
        pass

    return api_key.strip(), base_url.rstrip("/"), model.strip()


def get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = cloudscraper.create_scraper()
    return _scraper


# ── Fetch problem page ──────────────────────────────────────────────────

def fetch_problem_html(contest_id: int, index: str) -> tuple[str, str]:
    """Fetch CF problem page, return (html, pid)."""
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    scraper = get_scraper()
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text, f"{contest_id}{index}"


# ── Extract problem statement block ─────────────────────────────────────

def extract_problem_statement(html: str) -> str | None:
    """Extract the <div class='problem-statement'> HTML block."""
    m = re.search(
        r'<div class="problem-statement"[^>]*>([\s\S]*?)</div>\s*<script',
        html,
    )
    return m.group(1) if m else None


# ── Find formula / graphics images ──────────────────────────────────────

def find_all_tex_images(ps_html: str) -> tuple[list[dict], list[dict]]:
    """Find all tex-formula and tex-graphics images.
    Returns (formulas, graphics) — each a list of {tag, src, class, start, end}.
    tex-formula = math formula images (should convert to LaTeX)
    tex-graphics = diagrams/charts/drawings (should describe and attach)
    """
    formulas = []
    graphics = []
    for m in re.finditer(
        r'<img[^>]*class="[^"]*\b(tex-formula|tex-graphics)\b[^"]*"[^>]*>',
        ps_html,
    ):
        tag = m.group(0)
        src_m = re.search(r'src="([^"]+)"', tag)
        cls_m = re.search(r'class="([^"]+)"', tag)
        cls = cls_m.group(1) if cls_m else ""
        entry = {
            "tag": tag,
            "src": normalize_image_url(src_m.group(1) if src_m else ""),
            "class": cls,
            "start": m.start(),
            "end": m.end(),
        }
        if "tex-graphics" in cls:
            graphics.append(entry)
        else:
            formulas.append(entry)
    return formulas, graphics


# ── VL backends ─────────────────────────────────────────────────────────

def image_to_base64_url(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    return f"data:{mime};base64,{b64}"


def normalize_image_url(url: str) -> str:
    """Normalize CF image URLs so urllib and OneBot can consume them."""
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urllib.parse.urljoin("https://codeforces.com", url)
    return url


def download_image(url: str) -> bytes:
    """Download an image from URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def preprocess_formula_png(raw_bytes: bytes, upscale: int = 2) -> bytes:
    """Convert transparent PNG to white background + upscale for better OCR."""
    img = Image.open(BytesIO(raw_bytes))
    if img.mode in ("LA", "PA"):
        img = img.convert("RGBA")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if upscale > 1:
        w, h = img.size
        img = img.resize((w * upscale, h * upscale), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def call_qwen_vl(prompt: str, image_urls: list[str]) -> str | None:
    """Call Qwen-VL API. Returns text or None."""
    api_key, base_url, model = _qwen_config()
    if not api_key:
        print("  Qwen-VL error: qwen.api_key is not configured", file=sys.stderr)
        return None
    if not base_url:
        print("  Qwen-VL error: qwen.base_url is not configured", file=sys.stderr)
        return None
    if not model:
        print("  Qwen-VL error: QWEN_MODEL is not configured", file=sys.stderr)
        return None

    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  Qwen-VL error: {e}", file=sys.stderr)
        return None


def call_openai_vl(prompt: str, image_urls: list[str]) -> str | None:
    """Call OpenAI-compatible VL API. Returns text or None."""
    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  OpenAI VL error: {e}", file=sys.stderr)
        return None


VL_BACKENDS = {
    "qwen": call_qwen_vl,
    "openai": call_openai_vl,
}


# ── Context extraction ──────────────────────────────────────────────────

def extract_context(ps_html: str, start: int, end: int, window: int = _CONTEXT_WINDOW) -> str:
    """Extract surrounding plain text around a formula image position.
    Returns stripped text of ~window chars before and after, with HTML tags removed.
    """
    ctx_start = max(0, start - window)
    ctx_end = min(len(ps_html), end + window)
    ctx_html = ps_html[ctx_start:ctx_end]
    # Strip HTML tags, normalize whitespace
    text = re.sub(r"<[^>]+>", " ", ctx_html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Hallucination detection ─────────────────────────────────────────────

def is_hallucination(latex_str: str | None) -> bool:
    """Check if VL output looks like a hallucination rather than a real formula."""
    if not latex_str or latex_str.startswith("(VL failed)") or latex_str.startswith("(download error"):
        return True
    if len(latex_str) > _MAX_FORMULA_LENGTH:
        return True
    for pattern in _HALLUCINATION_PATTERNS:
        if re.search(pattern, latex_str):
            return True
    return False


# ── VL call with retry ──────────────────────────────────────────────────

def call_vl_with_retry(
    vl_fn,
    b64_url: str,
    context: str,
    pid: str,
    formula_idx: int,
    max_retries: int = 2,
) -> str:
    """Call VL with context injection; retry on hallucination with stricter prompt."""
    base_prompt = (
        "Output ONLY the exact LaTeX source code of this mathematical formula. "
        "Use standard LaTeX: \\sum \\prod \\int \\frac \\gcd \\operatorname "
        "\\leq \\geq \\mid \\nmid \\bmod \\cdot \\times \\cdots \\substack. "
        "Preserve all subscript and superscript ranges exactly. "
        "NO explanation, NO markdown fences, just raw LaTeX."
    )

    # First attempt: with context
    prompt = f"Context from problem: {context}\n\n{base_prompt}"
    result = vl_fn(prompt, [b64_url])
    result = result.strip() if result else "(VL failed)"

    if not is_hallucination(result):
        return result

    print(f"  [{pid}] Formula {formula_idx}: possible hallucination, retrying...",
          file=sys.stderr)

    # Retry with stricter prompt (no context, lower temperature implicitly)
    for attempt in range(max_retries):
        strict_prompt = (
            "Look at this image carefully. It is a single mathematical formula from a "
            "competitive programming problem. Output ONLY its exact LaTeX. "
            "If it's a diagram or drawing, output the word DIAGRAM. "
            "Do NOT generate LaTeX document structure. "
            "Do NOT use \\documentclass, \\usepackage, \\begin, or \\end. "
            "Just the formula, nothing else."
        )
        result = vl_fn(strict_prompt, [b64_url])
        result = result.strip() if result else "(VL failed)"

        if is_hallucination(result):
            print(f"  [{pid}] Formula {formula_idx}: retry {attempt+1} still hallucinated",
                  file=sys.stderr)
            continue

        # Check if VL says it's a diagram
        if result.upper().strip() == "DIAGRAM":
            return "[DIAGRAM — not a formula]"

        return result

    return "(VL hallucination after retries)"


def call_vl_for_graphic(
    vl_fn,
    b64_url: str,
    context: str,
    pid: str,
    graphic_idx: int,
) -> str:
    """Ask VL for a faithful text rendering of a diagram."""
    prompt = (
        "This image is a diagram from a competitive programming problem statement. "
        "Describe only visible, task-relevant facts so a solver and a judging model can "
        "understand the statement without seeing the image. Include labels, numbers, "
        "nodes, edges, arrows, axes, coordinates, colors, shapes, examples, and order "
        "relationships if present. Do not infer a solution or add unstated facts. "
        "If the image contains no useful problem information, say: no task-relevant "
        "details visible.\n\n"
        f"Surrounding statement text: {context}"
    )
    result = vl_fn(prompt, [b64_url])
    result = re.sub(r"\s+", " ", result or "").strip()
    if not result:
        return "(VL failed)"
    if len(result) > 1000:
        result = result[:1000].rstrip() + "..."
    print(f"  [{pid}] Graphic {graphic_idx}: {result[:120]}", file=sys.stderr)
    return result


# ── HTML to text ────────────────────────────────────────────────────────

def html_to_text(ps_html: str) -> str:
    """Strip HTML tags, normalize whitespace."""
    text = re.sub(r"<[^>]+>", "", ps_html)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\$\$\$|\$\$|\$", "", text)
    return text


# ── Main pipeline ───────────────────────────────────────────────────────

def process_problem(
    contest_id: int,
    index: str,
    vl_backend: str = "none",
) -> dict:
    """Full pipeline: fetch → extract → handle formulas → return text + metadata.

    Returns dict with keys:
      - pid, url, text, text_length
      - formulas_found, formulas_processed
      - formula_details: list of {src, latex}
      - has_non_formula_images: True if tex-graphics (diagrams) found
      - graphics_details: list of {src, label, description}
      - formulas_failed: number of formulas that couldn't be converted after retries
    """
    html, pid = fetch_problem_html(contest_id, index)
    ps_html = extract_problem_statement(html)
    if not ps_html:
        return {"error": "Could not find problem-statement div", "pid": pid}

    formulas, graphics = find_all_tex_images(ps_html)
    has_non_formula_images = len(graphics) > 0

    print(f"[{pid}] Found {len(formulas)} formula(s), {len(graphics)} graphic(s)",
          file=sys.stderr)

    if has_non_formula_images:
        print(f"  [{pid}] contains tex-graphics (diagrams) — describing with VL",
              file=sys.stderr)

    # Process formula images
    formula_results = []
    formulas_failed = 0
    graphic_results = []
    graphics_failed = 0

    if formulas and vl_backend in VL_BACKENDS:
        vl_fn = VL_BACKENDS[vl_backend]
        for i, fm in enumerate(formulas):
            print(f"  [{pid}] Formula {i+1}/{len(formulas)}: {fm['src'][:60]}...",
                  file=sys.stderr)
            try:
                # Download + preprocess
                img_bytes = download_image(fm["src"])
                img_bytes = preprocess_formula_png(img_bytes)
                b64_url = image_to_base64_url(img_bytes)

                # Extract context from surrounding text
                context = extract_context(ps_html, fm["start"], fm["end"])

                # Call VL with retry + hallucination detection
                desc = call_vl_with_retry(vl_fn, b64_url, context, pid, i + 1)

                formula_results.append({
                    "src": fm["src"],
                    "latex": desc,
                })
                print(f"    → {desc[:100]}", file=sys.stderr)

                if desc.startswith("(VL") or "[DIAGRAM" in desc:
                    formulas_failed += 1

            except Exception as e:
                formula_results.append({
                    "src": fm["src"],
                    "latex": f"(error: {e})",
                })
                formulas_failed += 1
                print(f"    → error: {e}", file=sys.stderr)

    elif formulas:
        print(f"  [{pid}] VL backend not configured, skipping {len(formulas)}",
              file=sys.stderr)
        for fm in formulas:
            formula_results.append({"src": fm["src"], "latex": "[FORMULA IMAGE]"})

    # Describe non-formula graphics instead of rejecting the whole problem.
    if graphics and vl_backend in VL_BACKENDS:
        vl_fn = VL_BACKENDS[vl_backend]
        for i, gm in enumerate(graphics, 1):
            label = f"Diagram {i}"
            try:
                img_bytes = download_image(gm["src"])
                b64_url = image_to_base64_url(preprocess_formula_png(img_bytes, upscale=1))
                context = extract_context(ps_html, gm["start"], gm["end"])
                description = call_vl_for_graphic(vl_fn, b64_url, context, pid, i)
                if description.startswith("(VL"):
                    graphics_failed += 1
                graphic_results.append({
                    "src": gm["src"],
                    "label": label,
                    "description": description,
                })
            except Exception as e:
                graphics_failed += 1
                graphic_results.append({
                    "src": gm["src"],
                    "label": label,
                    "description": f"(error: {e})",
                })
                print(f"    → graphic error: {e}", file=sys.stderr)
    elif graphics:
        for i, gm in enumerate(graphics, 1):
            graphic_results.append({
                "src": gm["src"],
                "label": f"Diagram {i}",
                "description": "[DIAGRAM IMAGE]",
            })

    # Replace all image tags with their text representation (reverse order)
    result_html = ps_html
    all_images = sorted(formulas + graphics, key=lambda x: x["start"], reverse=True)
    all_results_iter = iter(sorted(
        formula_results,
        key=lambda x: _find_match_index(x["src"], formulas + graphics),
        reverse=True,
    ))

    for img_entry in all_images:
        # Find matching result by src URL
        if "tex-graphics" in img_entry.get("class", ""):
            matching_graphics = [r for r in graphic_results if r["src"] == img_entry["src"]]
            graphic = matching_graphics[0] if matching_graphics else {}
            label = graphic.get("label", "Diagram")
            description = graphic.get("description", "[DIAGRAM IMAGE]")
            replacement_text = f"[{label}: {description}]"
        else:
            matching = [r for r in formula_results if r["src"] == img_entry["src"]]
            latex = matching[0]["latex"] if matching else "[IMAGE]"
            replacement_text = f"({latex})"
        replacement = f'<span class="tex-formula-text">{replacement_text}</span>'
        result_html = (
            result_html[:img_entry["start"]] + replacement + result_html[img_entry["end"]:]
        )

    plain_text = html_to_text(result_html)

    return {
        "pid": pid,
        "url": f"https://codeforces.com/problemset/problem/{contest_id}/{index}",
        "formulas_found": len(formulas),
        "graphics_found": len(graphics),
        "has_non_formula_images": has_non_formula_images,
        "graphics_processed": len(graphics) - graphics_failed,
        "graphics_failed": graphics_failed,
        "graphics_details": graphic_results,
        "formulas_processed": len(formulas) - formulas_failed,
        "formulas_failed": formulas_failed,
        "formula_details": formula_results,
        "text": plain_text,
        "text_length": len(plain_text),
    }


def _find_match_index(src: str, items: list[dict]) -> int:
    """Find index of item with matching src, or -1."""
    for i, item in enumerate(items):
        if item["src"] == src:
            return i
    return -1


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch CF problem statement with formula handling"
    )
    parser.add_argument(
        "args", nargs="*", help="contestId index, or combined pid, or nothing with --url"
    )
    parser.add_argument("--url", help="Full CF problem URL")
    parser.add_argument(
        "--vl-backend",
        choices=list(VL_BACKENDS) + ["none"],
        default=DEFAULT_VL_BACKEND,
        help="VL backend for formula images",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Only detect formulas, don't call VL"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    vl_backend = "none" if args.dry_run else args.vl_backend

    # Parse contestId + index
    if args.url:
        m = re.match(r".*/problem/(\d+)/(\w+)", args.url)
        if not m:
            print("Error: could not parse --url", file=sys.stderr)
            sys.exit(1)
        contest_id, index = int(m.group(1)), m.group(2)
    elif len(args.args) == 1:
        m = re.match(r"(\d+)(\w+)", args.args[0])
        if not m:
            print("Error: could not parse pid", file=sys.stderr)
            sys.exit(1)
        contest_id, index = int(m.group(1)), m.group(2)
    elif len(args.args) >= 2:
        contest_id, index = int(args.args[0]), args.args[1]
    else:
        parser.print_help()
        sys.exit(1)

    result = process_problem(contest_id, index, vl_backend)
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        json_out = {k: v for k, v in result.items() if k != "text"}
        json_out["text_preview"] = result["text"][:500]
        print(json.dumps(json_out, indent=2, ensure_ascii=False))
    else:
        print(f"=== CF{result['pid']} ===")
        print(f"URL: {result['url']}")
        print(f"Formulas: {result['formulas_found']} found, {result['formulas_processed']} processed")
        if result["has_non_formula_images"]:
            print(
                f"Diagrams: {result['graphics_found']} found, "
                f"{result.get('graphics_processed', 0)} described"
            )
        if result["formulas_failed"]:
            print(f"⚠ {result['formulas_failed']} formula(s) failed")
        if result.get("graphics_failed"):
            print(f"⚠ {result['graphics_failed']} diagram(s) failed")
        for fr in result.get("formula_details", []):
            src_short = fr["src"][:60]
            print(f"  {src_short}")
            print(f"    → {fr.get('latex', '?')[:120]}")
        for gr in result.get("graphics_details", []):
            src_short = gr["src"][:60]
            print(f"  {src_short}")
            print(f"    → {gr.get('description', '?')[:120]}")
        print(f"\n--- Problem text ({result['text_length']} chars) ---")
        print(result["text"][:2000])


if __name__ == "__main__":
    main()
