"""Official QQ bot API field probe — for owner-led live calibration (Q10 / E3 / E4).

The migration design assumes several things about live payloads that the docs
document but community has not 实测 yet. This tool gathers the ground truth so
those features can be implemented against real shapes instead of speculation:

  capture  Listen to the gateway and dump RAW group-event payloads to a local
           JSONL file, highlighting the fields we care about: author.avatar
           (is an avatar URL actually delivered?), mentions[] (structured @),
           msg_elements[] (message_type=103 quote + nested content), and
           message_scene.ext (msg_idx / ref_msg_idx). READ-ONLY: it sends nothing
           except the gateway identify/heartbeat needed to connect.

  length   Bracket the outbound text/markdown codepoint limit (official docs only
           give error 40054007, no number). Sends a ladder of increasing-length
           messages to ONE explicit group and reports where 40054007 starts.
           DRY-RUN by default; pass --send to actually send (consumes active
           quota and posts real messages — owner decides the target group).

Usage:
  KOUHAI_CONFIG=/path/to/config.yaml uv run python tools/official_probe.py capture
  KOUHAI_CONFIG=/path/to/config.yaml uv run python tools/official_probe.py capture --group <group_openid>
  KOUHAI_CONFIG=/path/to/config.yaml uv run python tools/official_probe.py length --group <group_openid>            # dry-run
  KOUHAI_CONFIG=/path/to/config.yaml uv run python tools/official_probe.py length --group <group_openid> --send     # live

Safety:
  - No credentials or QQ/group ids are hardcoded; everything comes from config.
  - `capture` writes only to a local file under data_dir; it never posts messages.
  - Do NOT run `capture` against the same app_id while the live bot is running:
    a second gateway connection can conflict with the bot's session. Stop the bot
    first, or use a separate test app_id.
  - `length --send` posts real messages to the group you name; it is opt-in and
    is never run automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from kouhai_bot.config import get_config
from kouhai_bot.official.gateway import OfficialGateway
from kouhai_bot.official.rest import OfficialApiError, OfficialRestClient
from kouhai_bot.official.tokens import AccessTokenManager


def _clients():
    cfg = get_config()
    cfg.validate_official()
    tokens = AccessTokenManager(
        cfg.app_id, cfg.app_secret, token_base=cfg.official_token_base,
    )
    rest = OfficialRestClient(tokens, api_base=cfg.official_api_base)
    return cfg, tokens, rest


def _highlight(ev: dict) -> str:
    author = ev.get("author") or {}
    elements = ev.get("msg_elements") or []
    etypes = [e.get("message_type") for e in elements if isinstance(e, dict)]
    scene = ev.get("message_scene") or {}
    return (
        f"{ev.get('event_type', '?')} group={ev.get('group_openid', '')[:8]} "
        f"user={ev.get('user_openid', '')[:8]} nick={ev.get('nickname', '')!r} "
        f"avatar={'YES' if author.get('avatar') else 'no'} "
        f"mentions={len(ev.get('mentions') or [])} "
        f"msg_elements={etypes} scene_ext={scene.get('ext', [])}"
    )


async def _capture(args) -> None:
    cfg, tokens, rest = _clients()
    out_dir = Path(cfg.data_dir).expanduser() / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else out_dir / f"events-{stamp}.jsonl"
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    count = 0
    fh = out_path.open("a", encoding="utf-8")

    async def on_event(ev: dict) -> None:
        nonlocal count
        if args.group and ev.get("group_openid") != args.group:
            return
        raw = ev.get("raw") or {}
        fh.write(json.dumps({"t": ev.get("event_type"), "raw": raw}, ensure_ascii=False) + "\n")
        fh.flush()
        count += 1
        print(_highlight(ev), flush=True)

    print(f"capturing raw events → {out_path}", flush=True)
    print("send real @/quote messages in the group now; Ctrl-C to stop.", flush=True)
    if args.group:
        print(f"filter: group_openid={args.group}", flush=True)
    gateway = OfficialGateway(rest, tokens, on_event=on_event, intents=cfg.official_intents)
    try:
        await gateway.run(stop)
    finally:
        fh.close()
        await rest.close()
        print(f"\ncaptured {count} events → {out_path}", flush=True)


def _ladder(args):
    if args.kind == "markdown":
        start = args.start if args.start is not None else 2500
        stop = args.max if args.max is not None else 5000
    else:
        start = args.start if args.start is not None else 1500
        stop = args.max if args.max is not None else 4000
    step = args.step or 100
    return list(range(start, stop + 1, step))


async def _length(args) -> None:
    if not args.group:
        print("length probe requires --group <group_openid>", file=sys.stderr)
        raise SystemExit(2)
    ladder = _ladder(args)
    print(f"kind={args.kind} group={args.group} ladder={ladder[0]}..{ladder[-1]} step={args.step or 100}")
    if not args.send:
        print("DRY-RUN (no messages sent). Pass --send to post real messages to this group.")
        return
    cfg, tokens, rest = _clients()
    print("LIVE: posting real messages; consumes active quota. Ctrl-C to abort.")
    threshold = None
    try:
        for n in ladder:
            filler = "测" * n  # n codepoints
            if args.kind == "markdown":
                payload = {"msg_type": 2, "markdown": {"content": filler}}
            else:
                payload = {"content": filler, "msg_type": 0}
            try:
                resp = await rest.send_group_message(args.group, payload)
                ref = (resp.get("ext_info") or {}).get("ref_idx")
                print(f"len={n}: OK id={resp.get('id', '')[:12]} ref_idx={ref}")
            except OfficialApiError as e:
                code = (e.body or {}).get("code") if isinstance(e.body, dict) else None
                print(f"len={n}: ERROR HTTP {e.status} code={code} body={e.body}")
                if code == 40054007 or "长度" in str(e.body):
                    threshold = n
                    print(f"\nlength limit bracketed: first failure at {n} codepoints "
                          f"(last OK below). kind={args.kind}")
                    break
                # Other errors (quota/passive) are not length-related; stop to avoid spam.
                print("non-length error; stopping.")
                break
            await asyncio.sleep(args.delay)
    finally:
        await rest.close()
    if threshold is None:
        print("\nno length error reached within ladder; raise --max.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Official QQ bot API field probe")
    sub = parser.add_subparsers(dest="cmd")

    cap = sub.add_parser("capture", help="dump raw group-event payloads (read-only)")
    cap.add_argument("--group", default="", help="only capture this group_openid")
    cap.add_argument("--out", default="", help="output JSONL path (default data_dir/probe/)")

    ln = sub.add_parser("length", help="bracket outbound text/markdown length limit")
    ln.add_argument("--group", default="", help="target group_openid (required)")
    ln.add_argument("--kind", choices=["text", "markdown"], default="text")
    ln.add_argument("--send", action="store_true", help="actually send (default dry-run)")
    ln.add_argument("--start", type=int, default=None)
    ln.add_argument("--max", type=int, default=None)
    ln.add_argument("--step", type=int, default=None)
    ln.add_argument("--delay", type=float, default=1.0, help="seconds between sends")

    args = parser.parse_args()
    cmd = args.cmd or "capture"
    if cmd == "capture":
        if not hasattr(args, "group"):
            args.group, args.out = "", ""
        asyncio.run(_capture(args))
    else:
        asyncio.run(_length(args))


if __name__ == "__main__":
    main()