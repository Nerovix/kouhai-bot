#!/usr/bin/env python3
"""Local server for solved-problem annotation bundles."""

from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web

from kouhai_bot.annotations.exporter import sync_annotation_bundles
from kouhai_bot.annotations.store import LABELED, PENDING, list_bundle_summaries, load_bundle, save_bundle
from kouhai_bot.handlers.shared import get_problem_summary
from kouhai_bot.handlers.shared import summarize_problem

STATIC_DIR = Path(__file__).resolve().parent / "annotation_web"


def _parse_group_id(value: str | None) -> int | None:
    if not value:
        return None
    return int(value)


def _translation_inputs(statement: dict) -> tuple[str, str, str]:
    stmt_text = statement.get("description", "") or ""
    input_parts = []
    if statement.get("input"):
        input_parts.append(f"Input:\n{statement['input']}")
    if statement.get("output"):
        input_parts.append(f"Output:\n{statement['output']}")
    input_text = "\n\n".join(input_parts)
    tl = statement.get("time_limit", "?")
    ml = statement.get("memory_limit", "?")
    limits_text = f"Time: {tl}, Memory: {ml}"
    return stmt_text, input_text, limits_text


async def _ensure_statement_translation(bundle: dict, status: str) -> dict:
    statement = bundle.get("statement")
    if not isinstance(statement, dict):
        return bundle
    if statement.get("summary_zh"):
        return bundle

    group_id = int(bundle.get("group_id", 0))
    problem_id = str(bundle.get("problem_id", ""))
    saved_summary = get_problem_summary(group_id, problem_id)
    if saved_summary:
        statement = dict(statement)
        statement["summary_zh"] = saved_summary
        bundle = dict(bundle)
        bundle["statement"] = statement
        save_bundle(bundle, status)
        return bundle

    stmt_text, input_text, limits_text = _translation_inputs(statement)
    if not (stmt_text or input_text):
        return bundle

    summary, _model_tag = await summarize_problem(stmt_text, input_text, limits_text)
    if not summary:
        summary, _model_tag = await summarize_problem(stmt_text, input_text, limits_text)
    if not summary:
        return bundle

    statement = dict(statement)
    statement["summary_zh"] = summary.strip()
    bundle = dict(bundle)
    bundle["statement"] = statement
    save_bundle(bundle, status)
    return bundle


async def handle_index(_request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_list(request: web.Request) -> web.Response:
    status = request.query.get("status")
    if status == "all":
        status = None
    group_id = _parse_group_id(request.query.get("group_id"))
    summaries = list_bundle_summaries(status=status, group_id=group_id)
    return web.json_response({"items": summaries})


async def handle_detail(request: web.Request) -> web.Response:
    group_id = int(request.match_info["group_id"])
    problem_id = request.match_info["problem_id"]
    status = request.query.get("status")
    if status == "all":
        status = None
    bundle, actual_status = load_bundle(group_id, problem_id, status=status)
    if bundle is None or actual_status is None:
        raise web.HTTPNotFound(text="annotation bundle not found")
    return web.json_response({"status": actual_status, "bundle": bundle})


async def handle_translate(request: web.Request) -> web.Response:
    group_id = int(request.match_info["group_id"])
    problem_id = request.match_info["problem_id"]
    status = request.query.get("status")
    if status == "all":
        status = None
    bundle, actual_status = load_bundle(group_id, problem_id, status=status)
    if bundle is None or actual_status is None:
        raise web.HTTPNotFound(text="annotation bundle not found")
    bundle = await _ensure_statement_translation(bundle, actual_status)
    statement = bundle.get("statement", {})
    return web.json_response({
        "ok": True,
        "summary_zh": statement.get("summary_zh", ""),
        "status": actual_status,
    })


async def handle_save(request: web.Request) -> web.Response:
    group_id = int(request.match_info["group_id"])
    problem_id = request.match_info["problem_id"]
    body = await request.json()
    status = body.get("status", PENDING)
    bundle = body.get("bundle")
    if not isinstance(bundle, dict):
        raise web.HTTPBadRequest(text="missing bundle")
    if int(bundle.get("group_id", -1)) != group_id or str(bundle.get("problem_id", "")) != problem_id:
        raise web.HTTPBadRequest(text="bundle identity mismatch")
    if status not in {PENDING, LABELED}:
        raise web.HTTPBadRequest(text="invalid status")
    path = save_bundle(bundle, status)
    return web.json_response({"ok": True, "path": str(path), "status": status})


async def handle_sync(request: web.Request) -> web.Response:
    if request.content_length:
        body = await request.json()
    else:
        body = {}
    group_id = body.get("group_id")
    group = int(group_id) if group_id is not None else None
    created = sync_annotation_bundles(group_id=group)
    return web.json_response({"ok": True, "created": [str(path) for path in created], "count": len(created)})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application(client_max_size=2**20)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/annotations", handle_list)
    app.router.add_get("/api/annotations/{group_id}/{problem_id}", handle_detail)
    app.router.add_post("/api/annotations/{group_id}/{problem_id}/translate", handle_translate)
    app.router.add_post("/api/annotations/{group_id}/{problem_id}/save", handle_save)
    app.router.add_post("/api/sync", handle_sync)
    app.router.add_static("/static/", STATIC_DIR)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local annotation UI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--group", type=int, default=None, help="Only sync one group on startup.")
    parser.add_argument("--no-sync", action="store_true", help="Skip startup backfill sync.")
    args = parser.parse_args()

    if not args.no_sync:
        created = sync_annotation_bundles(group_id=args.group)
        print(f"[annotation] synced {len(created)} new pending bundles")

    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
