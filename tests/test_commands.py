"""Realistic end-to-end command tests with proper state mocking.

Covers every path: correct, incorrect, off-topic, already-solved,
no-problem, with-problem, empty-input, cooldown.
"""

import sys, os, json, asyncio, tempfile, shutil, time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.config import UserGroupConfig
from kouhai_bot.llm import ChatCompletionResult


# ═══════════════════════════════════════════════════════════════════════
# Test infrastructure
# ═══════════════════════════════════════════════════════════════════════

GID = 999999
UID = 42
PID = "542D"
OTHER_UID = 99
GID2 = 888888
UID2 = 77
PID2 = "100A"

_sent: list[dict] = []
_reacted: list[tuple] = []
_private_sent: list[dict] = []
_forwarded: list[dict] = []
_private_forwarded: list[dict] = []
_deleted: list[str] = []
_deepseek_response: dict | None = None
_group_members: dict[int, list[dict]] = {}
_deepseek_calls: list[dict] = []
_temp_dir = None


def _reset_state():
    global _sent, _reacted, _private_sent, _forwarded, _private_forwarded, _deleted, _deepseek_response, _group_members, _deepseek_calls, _temp_dir
    _sent.clear()
    _reacted.clear()
    _private_sent.clear()
    _forwarded.clear()
    _private_forwarded.clear()
    _deleted.clear()
    _deepseek_response = None
    _group_members = {
        GID: [
            {"user_id": UID, "nickname": "Alice", "card": ""},
            {"user_id": OTHER_UID, "nickname": "Bob", "card": ""},
            {"user_id": UID2, "nickname": "Carol", "card": ""},
        ],
        GID2: [
            {"user_id": UID, "nickname": "Alice", "card": ""},
            {"user_id": OTHER_UID, "nickname": "Bob", "card": ""},
            {"user_id": UID2, "nickname": "Carol", "card": ""},
        ],
    }
    _deepseek_calls.clear()
    _temp_dir = tempfile.mkdtemp(prefix="xcpc_test_")
    data_dir = os.path.join(_temp_dir, "data")
    os.makedirs(os.path.join(data_dir, "groups", str(GID)), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "statements"), exist_ok=True)


def _cleanup():
    global _temp_dir
    if _temp_dir and os.path.exists(_temp_dir):
        shutil.rmtree(_temp_dir)
    _temp_dir = None


def _data_dir() -> str:
    return os.path.join(_temp_dir, "data")


def _write_state(group_id: int, data: dict):
    d = os.path.join(_data_dir(), "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(data, f)


def _write_statement(pid: str, data: dict):
    d = os.path.join(_data_dir(), "statements")
    os.makedirs(d, exist_ok=True)
    payload = dict(data)
    payload.setdefault("_vl_processed", True)
    with open(os.path.join(d, f"{pid}.json"), "w") as f:
        json.dump(payload, f)


def _write_scoreboard(group_id: int, data: dict):
    d = os.path.join(_data_dir(), "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "scoreboard.json"), "w") as f:
        json.dump(data, f)


def _write_group_file(group_id: int, filename: str, data: dict):
    d = os.path.join(_data_dir(), "groups", str(group_id))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, filename), "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_problem_ratings(group_id: int, data: dict):
    _write_group_file(group_id, "problem_ratings.json", data)


def _set_group_members(group_id: int, members: list[dict]):
    _group_members[group_id] = members


def _write_tutorial(pid: str, data: dict):
    d = os.path.join(_data_dir(), "tutorials")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{pid}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _has_sent(substring: str) -> bool:
    for s in _sent:
        msg = s.get("message", [])
        for seg in msg if isinstance(msg, list) else [msg]:
            text = seg.get("data", {}).get("text", "") if isinstance(seg, dict) else str(seg)
            if substring in text:
                return True
    return False


def _last_text() -> str:
    if not _sent:
        return ""
    msg = _sent[-1]["message"]
    if isinstance(msg, list):
        return " ".join(
            seg.get("data", {}).get("text", "")
            for seg in msg if seg.get("type") == "text"
        )
    return str(msg)


async def _mock_send_group(group_id, message):
    _sent.append({"group_id": group_id, "message": message})
    return True


async def _mock_react(message_id, emoji_id):
    _reacted.append((message_id, emoji_id))


async def _mock_send_private(user_id, message):
    message_id = 1000 + len(_private_sent)
    _private_sent.append({"user_id": user_id, "message": message, "message_id": message_id})
    return message_id


async def _mock_send_group_forward(group_id, messages):
    _forwarded.append({"group_id": group_id, "messages": messages})
    return 2000 + len(_forwarded)


async def _mock_send_private_forward(user_id, messages):
    _private_forwarded.append({"user_id": user_id, "messages": messages})
    return 3000 + len(_private_forwarded)


async def _mock_delete_msg(message_id):
    _deleted.append(str(message_id))


async def _mock_http_post(action, data):
    if action == "get_group_member_list":
        return {"status": "ok", "data": list(_group_members.get(int(data["group_id"]), []))}
    if action == "get_group_member_info":
        members = _group_members.get(int(data["group_id"]), [])
        target_uid = str(data["user_id"])
        for member in members:
            if str(member.get("user_id")) == target_uid:
                return {"status": "ok", "data": member}
        return {"status": "failed", "data": {}}
    return {"status": "failed", "data": {}}


async def _mock_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                         response_format=None, thinking=None):
    _deepseek_calls.append({
        "messages": messages,
        "task": task,
        "model": model,
    })
    if task == "summary":
        return "官方题解中文翻译。" * 20
    return json.dumps(_deepseek_response) if isinstance(_deepseek_response, dict) else _deepseek_response


async def _mock_chat_completion_result(messages, model="", task="", temperature=0.7, timeout=120,
                                       response_format=None, thinking=None):
    result = await _mock_deepseek(
        messages,
        model=model,
        task=task,
        temperature=temperature,
        timeout=timeout,
        response_format=response_format,
        thinking=thinking,
    )
    if result is None:
        return ChatCompletionResult(text=None, failure_kind="service_unavailable")
    return ChatCompletionResult(text=result, failure_kind=None)


async def _mock_judge_result(problem_text, submission, history=None):
    result = await _mock_chat_completion_result(
        [
            {"role": "system", "content": ""},
            {"role": "user", "content": submission},
        ],
        task="judge",
        timeout=1200,
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
    )
    return result


def _wrap_llm_result(fn, failure_kind="service_unavailable"):
    async def _wrapped(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if result is None:
            return ChatCompletionResult(text=None, failure_kind=failure_kind)
        return ChatCompletionResult(text=result, failure_kind=None)
    return _wrapped


def _wrap_deepseek_as_judge_result(fn, failure_kind="service_unavailable"):
    async def _wrapped(problem_text, submission, history=None):
        result = await fn(
            [{}, {"content": json.dumps({"submission": submission, "history": history})}],
            task="judge",
            timeout=1200,
            response_format={"type": "json_object"},
            thinking={"type": "enabled"},
        )
        if result is None:
            return ChatCompletionResult(text=None, failure_kind=failure_kind)
        return ChatCompletionResult(text=result, failure_kind=None)
    return _wrapped


def _make_event(text="", group_id=GID, user_id=UID, message_id="msg_001", message=None):
    if message is None:
        message = [{"type": "text", "data": {"text": text}}]
    return {
        "type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "sender": {"nickname": "Alice", "card": "", "user_id": user_id},
        "message_id": message_id,
        "raw_message": text,
        "message": message,
    }


def _make_private_event(text="", user_id=UID, message_id="priv_001", message=None):
    if message is None:
        message = [{"type": "text", "data": {"text": text}}]
    return {
        "type": "message",
        "message_type": "private",
        "group_id": GID,
        "user_id": user_id,
        "sender": {"nickname": "Alice", "card": "", "user_id": user_id},
        "message_id": message_id,
        "raw_message": text,
        "message": message,
    }


def _kwargs(event):
    return {
        "group_id": event["group_id"],
        "user_id": event["user_id"],
        "sender": event["sender"],
        "message_id": event["message_id"],
        "raw_text": event["raw_message"],
        "segments": event["message"],
        "event": event,
    }


def _setup_problem():
    """Create a realistic problem state: 542D with Joker function."""
    _setup_problem_for(GID, PID)


def _setup_problem_for(group_id: int, pid: str):
    contest_id = int(pid[:-1]) if pid[:-1].isdigit() else 542
    index = pid[-1]
    _write_state(group_id, {
        "today": pid,
        "contestId": contest_id, "index": index,
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
    })
    _write_statement(pid, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": (
            "It's tough to be a superhero. The bomb has a note saying J(x) = A. "
            "The Joker function: J(x) = sum over k where k|x and gcd(k,x/k)=1. "
            "Find x such that J(x)=A. Input A (1≤A≤10^12). Output x or -1."
        ),
        "input": "A single integer A (1 ≤ A ≤ 10^12).",
        "samples": [
            {"input": "4", "output": "6"},
            {"input": "8", "output": "-1"},
        ],
    })


# ═══════════════════════════════════════════════════════════════════════
# Patch helpers
# ═══════════════════════════════════════════════════════════════════════

def _config_dict():
    return {
        "bot_qq": 1,
        "napcat_ws_port": 8095, "napcat_http_host": "127.0.0.1", "napcat_http_port": 3000,
        "deepseek_api_key": "test-key", "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_model": "deepseek-reasoner",
        "llm_provider": "deepseek",
        "llm_openai_api_key": "",
        "llm_openai_base_url": "https://api.openai.com/v1",
        "llm_openai_model": "gpt-5",
        "llm_reasoning_effort": "",
        "judge_model": "",
        "clarify_model": "",
        "review_model": "",
        "summary_model": "",
        "judge_timeout_sec": 1200,
        "clarify_timeout_sec": 600,
        "review_timeout_sec": 600,
        "summary_timeout_sec": 120,
        "qwen_api_key": "",
        "qwen_model": "qwen-vl-max",
        "current_group": GID,
        "min_rating": 2000, "max_rating": 3000,
        "newproblem_cooldown": 300,
        "submit_ac_backdoor": "",
        "user_groups": [],
        "max_context_per_session": 100,
    }


class _LazyConfig:
    """Return data_dir lazily (depends on _temp_dir set by _reset_state)."""
    _config = _config_dict()

    def llm_provider_name(self):
        return self._config.get("llm_provider", "deepseek")

    def llm_api_key(self):
        if self.llm_provider_name() == "openai":
            return self._config.get("llm_openai_api_key", "")
        return self._config.get("deepseek_api_key", "")

    def llm_base_url(self):
        if self.llm_provider_name() == "openai":
            return self._config.get("llm_openai_base_url", "")
        return self._config.get("deepseek_base_url", "")

    def llm_default_model(self):
        if self.llm_provider_name() == "openai":
            return self._config.get("llm_openai_model", "")
        return self._config.get("deepseek_model", "")

    def llm_model_for(self, task: str = "", explicit_model: str = ""):
        if explicit_model:
            return explicit_model
        task_name = (task or "").strip().lower()
        if task_name == "judge":
            return self._config.get("judge_model") or "deepseek-v4-pro"
        if task_name == "clarify":
            return self._config.get("clarify_model") or "deepseek-v4-flash"
        if task_name == "review":
            return self._config.get("review_model") or "deepseek-v4-pro"
        if task_name == "summary":
            return self._config.get("summary_model") or "deepseek-v4-pro"
        return self.llm_default_model()

    def __getattr__(self, name):
        if name == "data_dir":
            return _data_dir()
        return self._config.get(name, getattr(super(), name, None))


def _all_patches():
    """Return a context manager applying all patches."""
    stack = ExitStack()
    stack.enter_context(patch("kouhai_bot.config._config", _LazyConfig()))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.napcat.client.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_private_forward_msg", _mock_send_private_forward))
    stack.enter_context(patch("kouhai_bot.napcat.client.delete_msg", _mock_delete_msg))
    stack.enter_context(patch("kouhai_bot.napcat.client._http_post", _mock_http_post))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_private_forward_msg", _mock_send_private_forward))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.delete_msg", _mock_delete_msg))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.clarify.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.review.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.clear.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.newproblem.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.newproblem.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.newproblem.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.stubs.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.stubs.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.setproblem.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.setproblem.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.sync.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.sync.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.sync.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.testcd.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.testcd.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.editorial_followup.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.editorial_followup.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.private_judge.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.private_judge.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.private_judge.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.private_judge.send_private_forward_msg", _mock_send_private_forward))
    stack.enter_context(patch("kouhai_bot.handlers.shared.call_chat_completion_result", _mock_chat_completion_result))
    stack.enter_context(patch("kouhai_bot.handlers.shared.judge_submission_result", _mock_judge_result))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _mock_chat_completion_result))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _mock_judge_result))
    return stack


def _starred_config_for_user(user_id: int = UID, *, submit_delay_sec: int = 300) -> _LazyConfig:
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[user_id],
                submit_delay_sec=submit_delay_sec,
                submit_delay_message="将机会多留给年轻人吧～{wait}",
            )
        ],
    }
    return lazy


# ═══════════════════════════════════════════════════════════════════════
# Tests: /submit
# ═══════════════════════════════════════════════════════════════════════

def test_submit_correct():
    """Correct submission → congratulations, scoreboard update, reveal."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    editorial_body = "x" * 120
    _write_tutorial(PID, {
        "problem_id": PID,
        "tutorial_url": "https://codeforces.com/blog/entry/1",
        "tutorial_title": "Editorial",
        "sections": [{
            "label": "D",
            "title": "Superhero's Job",
            "hint": "",
            "solution": editorial_body,
            "code_blocks": [],
            "raw_text": editorial_body,
        }],
    })
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "Correct divisor sum", "reply": ""}

    async def _run_submit():
        from kouhai_bot import editorial_followup

        def _schedule_and_await(group_id, pid):
            editorial_tasks.append(
                asyncio.create_task(
                    editorial_followup.run_post_solve_editorial_followup(group_id, pid)
                )
            )

        editorial_tasks: list[asyncio.Task] = []
        with _all_patches(), \
                patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")), \
                patch(
                    "kouhai_bot.handlers.cmd.submit.schedule_post_solve_editorial_followup",
                    _schedule_and_await,
                ):
            from kouhai_bot.handlers.cmd.submit import handle
            await handle(**_kwargs(_make_event(
                "/submit Precompute J(x) for all x up to 1e6 using divisor enumeration, then check if A is in the map."
            )))
            if editorial_tasks:
                await asyncio.gather(*editorial_tasks)

    asyncio.run(_run_submit())

    assert _has_sent("恭喜") or _has_sent("通过") or _has_sent("solved"), \
        f"No congrats. Messages: {[_last_text()]}"
    assert _forwarded, f"Expected official tutorial forward, got: {_forwarded}"
    assert _private_sent, "Expected private self-send for tutorial forward"
    first_private = _private_sent[0]["message"]
    private_text = first_private if isinstance(first_private, str) else str(first_private)
    if isinstance(first_private, list):
        private_text = " ".join(
            seg.get("data", {}).get("text", "")
            for seg in first_private
            if isinstance(seg, dict) and seg.get("type") == "text"
        )
    assert "官方题解" in private_text
    assert "官方题解中文翻译" in private_text
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "submit"
    assert records[-1]["result"] == "correct"
    _cleanup()
    print("✅ submit: correct")

def test_submit_correct_uses_fresh_top5_nicknames_and_points():
    _reset_state()
    _setup_problem()
    _write_problem_ratings(GID, {"100A": 2000})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": OTHER_UID, "nickname": "OldBob", "problem": "100A", "timestamp": time.time() - 100},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit valid idea"))))

    text = _last_text()
    assert "总分 4" in text, f"Missing score summary: {text}"
    assert "FreshAlice" in text and "FreshBob" in text, f"Fresh top5 names missing: {text}"
    assert "OldBob" not in text, f"Stale nickname leaked into top5: {text}"
    _cleanup()
    print("✅ submit: top5 uses fresh nicknames and points")


def test_submit_alias_dispatches_to_submit_handler():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "not enough detail", "reply": "不对"}

    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(process_event(_make_event("/sbm valid idea"), spawn_handlers=False))

    assert "不对" in _last_text(), f"Expected submit alias to run judge, got: {_last_text()}"
    _cleanup()
    print("✅ submit alias: /sbm")


def test_submit_starred_user_shows_own_group_top5():
    _reset_state()
    _setup_problem()
    _write_problem_ratings(GID, {"100A": 2000})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": OTHER_UID, "nickname": "OldBob", "problem": "100A", "timestamp": time.time() - 100},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=0,
            )
        ],
    }
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit valid idea"))))

    text = _last_text()
    assert "当前第 1" in text, f"Starred user should rank inside own group: {text}"
    assert "🏆 打星 Top 5：" in text, f"Missing starred top5 heading: {text}"
    assert "FreshAlice" in text, f"Starred user missing from own group top5: {text}"
    assert "FreshBob" not in text, f"Default user leaked into starred top5: {text}"
    _cleanup()
    print("✅ submit: starred user sees own group top5")


def test_submit_same_score_shares_rank():
    _reset_state()
    _setup_problem()
    _write_problem_ratings(GID, {"100A": 2600})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": OTHER_UID, "nickname": "OldBob", "problem": "100A", "timestamp": time.time() - 100},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit valid idea"))))

    text = _last_text()
    assert "当前第 1" in text, f"Tied score should share rank 1: {text}"
    assert "1. FreshAlice (1 题，4 分)" in text, f"Alice top5 rank wrong: {text}"
    assert "1. FreshBob (1 题，4 分)" in text, f"Bob top5 rank wrong: {text}"
    _cleanup()
    print("✅ submit: same score shares rank")


def test_submit_correct_schedules_editorial_without_blocking():
    """Editorial followup is scheduled from finalize, not awaited (coordinator can continue)."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}
    scheduled: list[tuple[int, str]] = []

    def _record_schedule(group_id, pid):
        scheduled.append((group_id, pid))

    async def _run():
        with _all_patches(), \
                patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")), \
                patch(
                    "kouhai_bot.handlers.cmd.submit.schedule_post_solve_editorial_followup",
                    _record_schedule,
                ):
            from kouhai_bot.handlers.cmd.submit import handle
            await handle(**_kwargs(_make_event("/submit valid solution with enough detail")))

    asyncio.run(_run())
    assert scheduled == [(GID, PID)], f"Expected editorial scheduled once, got {scheduled}"
    assert _has_sent("恭喜") or _has_sent("solved")
    _cleanup()
    print("✅ submit: editorial followup does not block coordinator")


def test_do_daily_post_schedules_editorial_prefetch():
  _reset_state()
  pid = "542D"
  state_dir = os.path.join(_data_dir(), "groups", str(GID))
  os.makedirs(state_dir, exist_ok=True)
  with open(os.path.join(state_dir, "state.json"), "w") as f:
      json.dump({"today": pid}, f)
  _write_statement(pid, {"description": "d", "input": "i", "time_limit": "1s", "memory_limit": "256MB"})

  scheduled: list[str] = []
  summary_started = asyncio.Event()

  def _record_prefetch(p):
      scheduled.append(p)

  async def _slow_summary(*args, **kwargs):
      summary_started.set()
      await asyncio.sleep(0.2)
      return "summary", ""

  async def _mock_picker_proc(*args, **kwargs):
      proc = MagicMock()
      proc.returncode = 0
      payload = {
          "today": pid,
          "contestId": 542,
          "index": "D",
          "name": "Superhero's Job",
          "rating": 2600,
          "tags": [],
          "date": "2026-05-22",
      }
      proc.communicate = AsyncMock(return_value=(json.dumps(payload).encode(), b""))
      return proc

  async def _run():
      with _all_patches(), \
              patch("kouhai_bot.handlers.cmd.newproblem._picker_args", return_value=["picker"]), \
              patch("kouhai_bot.handlers.cmd.newproblem.asyncio.create_subprocess_exec", _mock_picker_proc), \
              patch("kouhai_bot.handlers.cmd.newproblem.summarize_problem", _slow_summary), \
              patch("kouhai_bot.handlers.cmd.newproblem._send_problem_forward_card", AsyncMock(return_value=(123, {}))), \
              patch("kouhai_bot.handlers.cmd.newproblem.schedule_prefetch_editorial", _record_prefetch):
          from kouhai_bot.handlers.cmd.newproblem import do_daily_post
          await do_daily_post(GID, prefix="test")

  asyncio.run(_run())
  assert scheduled == [pid]
  assert summary_started.is_set() and scheduled, \
      "prefetch should run before summarize finishes"
  _cleanup()
  print("✅ newproblem: schedules editorial prefetch early")


def test_do_daily_post_does_not_switch_state_when_send_fails():
  _reset_state()
  old_pid = "542D"
  new_pid = "100A"
  state_dir = os.path.join(_data_dir(), "groups", str(GID))
  os.makedirs(state_dir, exist_ok=True)
  with open(os.path.join(state_dir, "state.json"), "w") as f:
      json.dump({"today": old_pid, "contestId": 542, "index": "D"}, f)
  _write_statement(new_pid, {"description": "d", "input": "i", "time_limit": "1s", "memory_limit": "256MB"})

  async def _mock_picker_proc(*args, **kwargs):
      proc = MagicMock()
      proc.returncode = 0
      payload = {
          "today": new_pid,
          "contestId": 100,
          "index": "A",
          "name": "New Problem",
          "rating": 2000,
          "tags": [],
          "date": "2026-05-22",
      }
      proc.communicate = AsyncMock(return_value=(json.dumps(payload).encode(), b""))
      return proc

  async def _fail_send_group(*args, **kwargs):
      return False

  async def _run():
      with _all_patches(), \
              patch("kouhai_bot.handlers.cmd.newproblem._picker_args", return_value=["picker"]), \
              patch("kouhai_bot.handlers.cmd.newproblem.asyncio.create_subprocess_exec", _mock_picker_proc), \
              patch("kouhai_bot.handlers.cmd.newproblem.summarize_problem", AsyncMock(return_value=("summary", ""))), \
              patch("kouhai_bot.handlers.cmd.newproblem._send_problem_forward_card", AsyncMock(return_value=(None, {}))), \
              patch("kouhai_bot.handlers.cmd.newproblem.send_group_msg", _fail_send_group):
          from kouhai_bot.handlers.cmd.newproblem import do_daily_post
          await do_daily_post(GID, prefix="test")

  asyncio.run(_run())
  with open(os.path.join(state_dir, "state.json")) as f:
      state = json.load(f)
  assert state["today"] == old_pid, f"Failed post should leave old state intact: {state}"
  _cleanup()
  print("✅ newproblem: failed delivery does not switch current problem")


def test_submit_correct_no_editorial_sends_nothing():
    """First AC without scraped editorial → no tutorial message."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}

    async def _run():
        from kouhai_bot import editorial_followup

        editorial_tasks: list[asyncio.Task] = []

        def _schedule(group_id, pid):
            editorial_tasks.append(
                asyncio.create_task(
                    editorial_followup.run_post_solve_editorial_followup(group_id, pid)
                )
            )

        with _all_patches(), \
                patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")), \
                patch(
                    "kouhai_bot.handlers.cmd.submit.schedule_post_solve_editorial_followup",
                    _schedule,
                ):
            from kouhai_bot.handlers.cmd.submit import handle
            await handle(**_kwargs(_make_event("/submit valid solution text here")))
            if editorial_tasks:
                await asyncio.gather(*editorial_tasks)

    asyncio.run(_run())

    assert _has_sent("恭喜") or _has_sent("solved")
    assert not _forwarded, f"Should not forward without editorial: {_forwarded}"
    assert not _has_sent("暂无 HTML 版官方题解"), f"Should not notify: {_sent}"
    _cleanup()
    print("✅ submit: correct without editorial sends nothing")


def test_submit_incorrect():
    """Incorrect submission → LLM reply."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "Brute force too slow", "reply": "暴力不行哦，A最大1e12～"}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit brute force from 1 to A"))))

    assert "暴力" in _last_text(), f"Expected reply, got: {_last_text()}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "submit"
    assert records[-1]["result"] == "incorrect"
    _cleanup()
    print("✅ submit: incorrect")


def test_submit_llm_failure_shows_admin_message():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _fail_judge(problem_text, submission, history=None):
        return ChatCompletionResult(text=None, failure_kind="service_unavailable")

    with _all_patches(), patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _fail_judge):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit brute force from 1 to A"))))

    assert "模型服务出故障了，联系一下管理员帮帮忙吧～" in _last_text()
    assert ("msg_001", "268") in _reacted
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["content"] == "brute force from 1 to A"
    assert records[-1]["result"] == "service_unavailable"
    assert records[-1]["reply"] == ""
    _cleanup()
    print("✅ submit: llm failure shows admin message")


def test_submit_timeout_is_saved_as_context():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _timeout_judge(problem_text, submission, history=None):
        return ChatCompletionResult(text=None, failure_kind="timeout")

    with _all_patches(), patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _timeout_judge):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit maybe too slow"))))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["content"] == "maybe too slow"
    assert records[-1]["result"] == "timeout"
    assert records[-1]["reason"] == ""
    assert records[-1]["reply"] == ""
    _cleanup()
    print("✅ submit: timeout saved as context")


def test_submit_ac_backdoor_accepts_before_judge():
    """Configured backdoor string should accept a submit without calling judge."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _fail_deepseek(*args, **kwargs):
        raise AssertionError("submit backdoor should not call judge LLM")

    with _all_patches(), \
            patch.dict(_LazyConfig._config, {"submit_ac_backdoor": "OPEN-SESAME"}), \
            patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="CF542D")):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit OPEN-SESAME"))))

    assert _has_sent("恭喜") or _has_sent("通过") or _has_sent("solved"), \
        f"No congrats. Messages: {[_last_text()]}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "submit"
    assert records[-1]["result"] == "correct"
    assert records[-1]["reason"] == "SUBMIT_AC_BACKDOOR matched"
    assert not _reacted
    _cleanup()
    print("✅ submit: AC backdoor accepts before judge")


def test_submit_already_solved():
    """Already solved → appropriate message."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{"user_id": str(OTHER_UID), "nickname": "Bob", "problem": PID, "timestamp": time.time()}],
        "user_submissions": {},
    })

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit my solution"))))

    assert _has_sent("已经有人解出"), \
        f"Expected 'already solved', got: {_last_text()}"
    _cleanup()
    print("✅ submit: already solved")


def test_review_uses_latest_solved_problem():
    """Review should use the most recently solved problem, not today's unsolved one."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(UID): [{
                "timestamp": "2026-05-13T12:00:00+08:00",
                "content": "old attempt",
                "result": "incorrect",
                "reason": "reason",
                "reply": "reply",
                "problem": PID,
            }],
        },
    })
    global _deepseek_response
    _deepseek_response = "这是上一道已通过题目的复盘。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        asyncio.run(handle(**_kwargs(_make_event("/review 我当时为什么会想歪？"))))

    assert "上一道已通过题目" in _last_text(), f"Expected review reply, got: {_last_text()}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "review"
    assert records[-1]["result"] == "review"
    assert records[-1]["problem"] == PID
    _cleanup()
    print("✅ review: latest solved problem")


def test_review_includes_editorial_in_llm_payload():
    """Review LLM payload should include official editorial when available."""
    _reset_state()
    _setup_problem()
    editorial_body = "y" * 120
    _write_tutorial(PID, {
        "problem_id": PID,
        "tutorial_url": "https://codeforces.com/blog/entry/2",
        "tutorial_title": "Editorial",
        "sections": [{
            "label": "D",
            "title": "Superhero's Job",
            "hint": "",
            "solution": editorial_body,
            "code_blocks": [],
            "raw_text": editorial_body,
        }],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })
    global _deepseek_response
    _deepseek_response = "复盘一下你的思路。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        asyncio.run(handle(**_kwargs(_make_event("/review 我当时哪里想错了？"))))

    review_calls = [c for c in _deepseek_calls if c.get("task") == "review"]
    assert review_calls, f"Expected review LLM call, got: {_deepseek_calls}"
    user_content = review_calls[-1]["messages"][-1]["content"]
    assert "官方题解" in user_content, f"Missing editorial in review payload: {user_content[:300]}"
    assert editorial_body[:40] in user_content
    _cleanup()
    print("✅ review: includes official editorial in LLM payload")


def test_review_includes_mentioned_user_context():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(UID): [{
                "timestamp": "2026-05-13T12:00:00+08:00",
                "type": "submit",
                "content": "alice idea",
                "result": "incorrect",
                "reason": "alice reason",
                "reply": "alice reply",
                "problem": PID,
            }],
            str(OTHER_UID): [{
                "timestamp": "2026-05-13T12:01:00+08:00",
                "type": "submit",
                "content": "bob decomposition idea",
                "result": "correct",
                "reason": "bob reason",
                "reply": "",
                "problem": PID,
            }],
        },
    })
    global _deepseek_response
    _deepseek_response = "这是带 Bob 上下文的复盘。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review @99 Bob 的做法为什么对？",
            message=[
                {"type": "text", "data": {"text": "/review "}},
                {"type": "at", "data": {"qq": str(OTHER_UID)}},
                {"type": "text", "data": {"text": " Bob 的做法为什么对？"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    review_calls = [c for c in _deepseek_calls if c.get("task") == "review"]
    assert review_calls, f"Expected review LLM call, got: {_deepseek_calls}"
    user_content = review_calls[-1]["messages"][-1]["content"]
    assert "发起人在此题的提交/判定记录" in user_content
    assert "alice idea" in user_content
    assert "被 @ 群友在此题的上下文" in user_content
    assert f"用户 {OTHER_UID}：" in user_content
    assert "bob decomposition idea" in user_content
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["user_submissions"][str(UID)][-1]["type"] == "review"
    assert saved["user_submissions"][str(OTHER_UID)][-1]["content"] == "bob decomposition idea"
    _cleanup()
    print("✅ review: includes mentioned user context")


def test_review_mentioned_users_are_deduped_and_filtered():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(OTHER_UID): [{
                "timestamp": "2026-05-13T12:01:00+08:00",
                "type": "submit",
                "content": "bob only once",
                "result": "correct",
                "reason": "ok",
                "reply": "",
                "problem": PID,
            }],
        },
    })
    global _deepseek_response
    _deepseek_response = "这是多 @ 上下文的复盘。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review 多人上下文",
            message=[
                {"type": "text", "data": {"text": "/review "}},
                {"type": "at", "data": {"qq": str(OTHER_UID)}},
                {"type": "at", "data": {"qq": str(OTHER_UID)}},
                {"type": "at", "data": {"qq": f"0{OTHER_UID}"}},
                {"type": "at", "data": {"qq": str(UID)}},
                {"type": "at", "data": {"qq": "1"}},
                {"type": "at", "data": {"qq": "all"}},
                {"type": "at", "data": {"qq": str(UID2)}},
                {"type": "text", "data": {"text": " 多人上下文"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    review_calls = [c for c in _deepseek_calls if c.get("task") == "review"]
    user_content = review_calls[-1]["messages"][-1]["content"]
    assert user_content.count(f"用户 {OTHER_UID}：") == 1
    assert user_content.count(f"用户 {UID2}：") == 1
    assert f"用户 {UID}：" not in user_content
    assert "用户 1：" not in user_content
    assert "用户 all：" not in user_content
    assert user_content.index(f"用户 {OTHER_UID}：") < user_content.index(f"用户 {UID2}：")
    assert "bob only once" in user_content
    assert f"用户 {UID2}：\n(无)" in user_content
    _cleanup()
    print("✅ review: mentioned users deduped and filtered")


def test_review_alias_dispatches_to_review_handler():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })
    global _deepseek_response
    _deepseek_response = "这是复盘回复。"

    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(process_event(_make_event("/rv 我哪里想错了"), spawn_handlers=False))

    assert "复盘回复" in _last_text(), f"Expected review alias reply, got: {_last_text()}"
    _cleanup()
    print("✅ review alias: /rv")


def test_review_requires_previous_solve():
    """Review should fail when the group has never solved any problem."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        asyncio.run(handle(**_kwargs(_make_event("/review 能讲讲这题吗？"))))

    assert "还没有已通过的题目可以 review" in _last_text(), f"Unexpected reply: {_last_text()}"
    _cleanup()
    print("✅ review: no solved problem")


def test_review_uses_referenced_history_card_even_if_unsolved():
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Old Hard Problem",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old skipped problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    _write_group_file(GID, "problem_card_refs.json", {
        "card_old": {"problem": PID, "source": "daily_post", "created_at": 1},
    })
    global _deepseek_response
    _deepseek_response = "这是老题的复盘。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review 这题当时该怎么想？",
            message=[
                {"type": "reply", "data": {"id": "card_old"}},
                {"type": "text", "data": {"text": "/review 这题当时该怎么想？"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    assert "老题的复盘" in _last_text(), f"Expected referenced review reply, got: {_last_text()}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["user_submissions"][str(UID)][-1]["problem"] == PID
    _cleanup()
    print("✅ review: referenced unsolved history card works")


def test_review_rejects_current_problem_card():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    _write_group_file(GID, "problem_card_refs.json", {
        "card_today": {"problem": PID, "source": "daily_post", "created_at": 1},
    })

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review 讲讲这题",
            message=[
                {"type": "reply", "data": {"id": "card_today"}},
                {"type": "text", "data": {"text": "/review 讲讲这题"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    assert "当前题" in _last_text() and "解出来后再 /review" in _last_text(), f"Unexpected reply: {_last_text()}"
    _cleanup()
    print("✅ review: current problem card rejected")


def test_review_allows_current_problem_card_after_solve():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })
    _write_group_file(GID, "problem_card_refs.json", {
        "card_today": {"problem": PID, "source": "daily_post", "created_at": 1},
    })
    global _deepseek_response
    _deepseek_response = "当前题已经解出，可以正常复盘。"

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review 这题怎么想更顺？",
            message=[
                {"type": "reply", "data": {"id": "card_today"}},
                {"type": "text", "data": {"text": "/review 这题怎么想更顺？"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    assert "可以正常复盘" in _last_text(), f"Unexpected reply: {_last_text()}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["user_submissions"][str(UID)][-1]["problem"] == PID
    _cleanup()
    print("✅ review: solved current problem card allowed")


def test_review_unknown_referenced_card_is_friendly():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.review import handle
        event = _make_event(
            "/review 这题呢",
            message=[
                {"type": "reply", "data": {"id": "missing_card"}},
                {"type": "text", "data": {"text": "/review 这题呢"}},
            ],
        )
        asyncio.run(handle(**_kwargs(event)))

    text = _last_text()
    assert "认不出对应哪道题" in text and "可能不是题目卡片" in text and "也可能卡片太久了" in text, \
        f"Unexpected reply: {text}"
    _cleanup()
    print("✅ review: unknown referenced card is friendly")


def test_review_long_reply_is_chunked_into_one_forward_card():
    """Long review replies should be chunked and forwarded without truncation."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })
    global _deepseek_response
    _deepseek_response = "A" * 1000 + "B" * 250

    with _all_patches():
        with patch("kouhai_bot.handlers.cmd.submit.asyncio.sleep", AsyncMock()):
            from kouhai_bot.handlers.cmd.review import handle
            asyncio.run(handle(**_kwargs(_make_event("/review 细讲一下这题"))))

    assert len(_private_sent) == 1, f"Expected 1 private chunk, got {_private_sent}"
    assert _private_sent[0]["message"][0]["data"]["text"] == _deepseek_response
    assert len(_forwarded) == 1, f"Expected 1 forward card, got {_forwarded}"
    assert len(_forwarded[0]["messages"]) == 1, f"Expected 1 forward node, got {_forwarded}"
    assert "回复较长，已折叠到卡片里啦" in _last_text(), f"Unexpected final message: {_last_text()}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["reply"] == _deepseek_response
    _cleanup()
    print("✅ review: long reply chunked into one forward card")


def test_review_llm_failure_shows_admin_message():
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })

    async def _fail_review(*args, **kwargs):
        return ChatCompletionResult(text=None, failure_kind="service_unavailable")

    with _all_patches(), patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _fail_review):
        from kouhai_bot.handlers.cmd.review import handle
        asyncio.run(handle(**_kwargs(_make_event("/review 细讲一下这题"))))

    assert "模型服务出故障了，联系一下管理员帮帮忙吧～" in _last_text()
    assert ("msg_001", "268") in _reacted
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "review"
    assert records[-1]["result"] == "service_unavailable"
    assert records[-1]["content"] == "细讲一下这题"
    _cleanup()
    print("✅ review: llm failure shows admin message")


def test_review_parallel_same_group():
    """Concurrent reviews in one group should start compute in parallel."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })

    second_started = asyncio.Event()

    async def _parallel_review_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                        response_format=None, thinking=None):
        assert timeout == 600
        content = messages[-1]["content"]
        if "first review question" in content:
            await asyncio.wait_for(second_started.wait(), timeout=0.3)
            return "FIRST REVIEW"
        if "second review question" in content:
            second_started.set()
            return "SECOND REVIEW"
        return "OTHER REVIEW"

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _wrap_llm_result(_parallel_review_deepseek)):
                from kouhai_bot.handlers.cmd.review import handle
                ev1 = _kwargs(_make_event("/review first review question", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/review second review question", group_id=GID, user_id=OTHER_UID))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    texts = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "FIRST REVIEW" in text or "SECOND REVIEW" in text:
            texts.append(text)

    assert len(texts) == 2, f"Expected 2 review replies, got: {texts}"
    assert any("FIRST REVIEW" in text for text in texts), f"Missing first review reply: {texts}"
    assert any("SECOND REVIEW" in text for text in texts), f"Missing second review reply: {texts}"
    _cleanup()
    print("✅ review: same-group compute runs in parallel")


def test_review_same_user_runs_in_parallel_with_pending_archive_context():
    """Two reviews from the same user should not serialize on the earlier LLM call."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })

    active_reviews = 0
    max_active_reviews = 0

    async def _serial_review_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                      response_format=None, thinking=None):
        nonlocal active_reviews, max_active_reviews
        active_reviews += 1
        max_active_reviews = max(max_active_reviews, active_reviews)
        await asyncio.sleep(0.05)
        active_reviews -= 1
        content = messages[-1]["content"]
        if "first review question" in content:
            return "FIRST REVIEW"
        if "second review question" in content:
            return "SECOND REVIEW"
        return "OTHER REVIEW"

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _wrap_llm_result(_serial_review_deepseek)):
                from kouhai_bot.handlers.cmd.review import handle
                ev1 = _kwargs(_make_event("/review first review question", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/review second review question", group_id=GID, user_id=UID))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    assert max_active_reviews == 2, f"Expected same-user reviews to run in parallel, got max concurrency {max_active_reviews}"
    _cleanup()
    print("✅ review: same-user compute runs in parallel")


def test_review_after_pending_submit_uses_snapshotted_solved_problem():
    """Review behind a pending submit should still target the solved problem visible at enqueue time."""
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_statement(PID, {
        "name": "D. Old Solved Problem",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Old solved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_statement(PID2, {
        "name": "A. Current Problem",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Current unsolved problem statement.",
        "input": "A single integer A.",
        "samples": [{"input": "4", "output": "6"}],
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": OTHER_UID,
            "nickname": "Bob",
            "date": "2026-05-13",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {},
    })

    release_submit = asyncio.Event()
    review_started = asyncio.Event()

    async def _mixed_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                              response_format=None, thinking=None):
        content = messages[-1]["content"]
        if content.startswith("{"):
            payload = json.loads(content)
            if payload["submission"] == "solve now":
                await asyncio.wait_for(release_submit.wait(), timeout=0.5)
                return json.dumps({"correct": True, "reason": "", "reply": "", "reaction": ""})
            return json.dumps({"correct": False, "reason": "wrong", "reply": "OTHER"})

        assert "Old solved problem statement." in content, f"Review targeted wrong problem: {content}"
        assert "Current unsolved problem statement." not in content, f"Review should not target current problem: {content}"
        review_started.set()
        release_submit.set()
        return "REVIEW OLD"

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.shared.call_chat_completion_result", _wrap_llm_result(_mixed_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _wrap_llm_result(_mixed_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_mixed_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")):
                from kouhai_bot.handlers.cmd.submit import handle as submit_handle
                from kouhai_bot.handlers.cmd.review import handle as review_handle
                ev1 = _kwargs(_make_event("/submit solve now", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/review 继续复盘上一题", group_id=GID, user_id=OTHER_UID))
                t1 = asyncio.create_task(submit_handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(review_handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    assert review_started.is_set(), "Review compute never started before the submit resolved"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    review_records = saved["user_submissions"][str(OTHER_UID)]
    assert review_records[-1]["type"] == "review"
    assert review_records[-1]["result"] == "review"
    assert review_records[-1]["problem"] == PID, f"Expected snapshotted review pid {PID}, got: {review_records[-1]}"
    _cleanup()
    print("✅ review: pending submit does not retarget snapshotted solved problem")


def test_submit_off_topic():
    """Model-marked off-topic → 123 reaction, no message."""
    _reset_state()
    _setup_problem()
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "", "reply": "", "reaction": "123"}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit 傻逼"))))

    assert any(r[1] == "123" for r in _reacted), f"Expected 123, got {_reacted}"
    assert len(_sent) == 0, f"Expected no message for off-topic"
    _cleanup()
    print("✅ submit: off-topic")


def test_private_submit_off_topic_sends_face_123():
    _reset_state()
    _setup_problem()
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "", "reply": "", "reaction": "123"}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        from kouhai_bot.handlers.shared import get_today_problem
        from kouhai_bot.private_judge import set_private_current_problem

        set_private_current_problem(UID, get_today_problem(GID))
        asyncio.run(handle(**_kwargs(_make_private_event("/submit 傻逼"))))

    face_segments = [
        seg
        for item in _private_sent if item["user_id"] == UID
        for seg in item["message"] if isinstance(seg, dict) and seg.get("type") == "face"
    ]
    face_ids = {seg.get("data", {}).get("id") for seg in face_segments}
    assert "123" in face_ids, _private_sent
    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "😵" not in private_text, private_text
    _cleanup()
    print("✅ private submit: off-topic uses face 123")


def test_submit_operation_not_blocked():
    """Normal text containing 操作 should not be blocked by a local blacklist."""
    _reset_state()
    _setup_problem()
    global _deepseek_response
    _deepseek_response = {
        "correct": False,
        "reason": "做法还不够完整",
        "reply": "思路方向还行，但关键计数这步还得再补严一点～",
        "reaction": "",
    }

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit 记操作 1 为前 n 个排序"))))

    assert not any(r[1] == "123" for r in _reacted), f"Unexpected 123, got {_reacted}"
    assert any(r[1] in {"128064", "289"} for r in _reacted), f"Expected ack reaction, got {_reacted}"
    assert "关键计数" in _last_text(), f"Expected normal judge reply, got: {_last_text()}"
    _cleanup()
    print("✅ submit: 操作 not blocked")


def test_private_submit_sends_face_ack_instead_of_text_or_reaction():
    _reset_state()
    _setup_problem()
    global _deepseek_response
    _deepseek_response = {
        "correct": False,
        "reason": "做法还不够完整",
        "reply": "再补一下关键证明～",
        "reaction": "",
    }

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        from kouhai_bot.handlers.shared import get_today_problem
        from kouhai_bot.private_judge import set_private_current_problem

        set_private_current_problem(UID, get_today_problem(GID))
        asyncio.run(handle(**_kwargs(_make_private_event("/submit try dp"))))

    face_segments = [
        seg
        for item in _private_sent if item["user_id"] == UID
        for seg in item["message"] if isinstance(seg, dict) and seg.get("type") == "face"
    ]
    assert {seg.get("data", {}).get("id") for seg in face_segments} == {"289"}, _private_sent
    assert not _reacted, f"Private submit should not use group reactions: {_reacted}"
    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "[睁眼]" not in private_text, private_text
    assert "关键证明" in private_text, private_text
    _cleanup()
    print("✅ private submit: ack uses face message")


def test_private_clear_invalid_usage_sends_text_ack_instead_of_face():
    _reset_state()

    with _all_patches():
        from kouhai_bot.handlers.cmd.clear import handle
        asyncio.run(handle(**_kwargs(_make_private_event("/clear now"))))

    face_segments = [
        seg
        for item in _private_sent if item["user_id"] == UID
        for seg in item["message"] if isinstance(seg, dict) and seg.get("type") == "face"
    ]
    assert not face_segments, f"Private clear ack should avoid face segments: {_private_sent}"
    assert not _reacted, f"Private clear should not use group reactions: {_reacted}"
    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "👌" in private_text, private_text
    _cleanup()
    print("✅ private clear invalid usage: ack uses safe text message")


def test_submit_no_problem():
    """No problem → appropriate message."""
    _reset_state()

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit my solution"))))

    assert "没有" in _last_text(), f"Expected 'no problem', got: {_last_text()}"
    _cleanup()
    print("✅ submit: no problem")


# ═══════════════════════════════════════════════════════════════════════
# Tests: /clarify
# ═══════════════════════════════════════════════════════════════════════

def test_clarify_with_problem():
    """Clarify with problem → LLM reply."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = '{"reply": "J(x) 是一元因子之和，满足 gcd(k,x/k)=1。", "reaction": ""}'

    ctx_file = os.path.join(_data_dir(), "groups", f"groupctx_{GID}.json")
    with open(ctx_file, "w") as f:
        json.dump([{"role": "assistant", "content": "J(x)=所有gcd(k,x/k)=1的因子k之和。"}], f)

    with _all_patches():
        from kouhai_bot.handlers.cmd.clarify import handle
        asyncio.run(handle(**_kwargs(_make_event("/clarify Joker函数是什么"))))

    assert "J(x)" in _last_text() or "因子" in _last_text(), \
        f"Expected clarification, got: {_last_text()}"
    _cleanup()
    print("✅ clarify: with problem")


def test_private_submit_correct_message_includes_problem_id():
    _reset_state()
    _setup_problem_for(GID, PID)
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "ok", "reply": ""}

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        from kouhai_bot.handlers.shared import get_today_problem
        from kouhai_bot.private_judge import set_private_current_problem

        set_private_current_problem(UID, get_today_problem(GID))
        asyncio.run(handle(**_kwargs(_make_private_event("/submit valid private solution"))))

    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert f"做对了 {PID}" in private_text, private_text
    assert "/sync" in private_text, private_text
    _cleanup()
    print("✅ private submit: correct message includes pid")


def test_private_clarify_uses_private_problem_summary():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_statement(PID2, {
        "name": "A. Private Problem",
        "time_limit": "1s",
        "memory_limit": "256MB",
        "description": "Private statement asks for a path count.",
        "input": "n",
        "output": "answer",
        "samples": [{"input": "1", "output": "1"}],
    })
    _write_group_file(GID, "problem_summaries.json", {
        PID2: {"summary_zh": "PRIVATE_PID_SUMMARY"},
    })
    ctx_file = os.path.join(_data_dir(), "groups", f"groupctx_{GID}.json")
    with open(ctx_file, "w") as f:
        json.dump([{"role": "assistant", "content": "GROUP_CURRENT_SUMMARY"}], f)
    global _deepseek_response
    _deepseek_response = '{"reply": "按 private 题面解释。", "reaction": ""}'

    with _all_patches():
        from kouhai_bot.private_judge import set_private_current_problem
        from kouhai_bot.handlers.cmd.clarify import handle

        set_private_current_problem(UID, {
            "today": PID2,
            "contestId": 100,
            "index": "A",
            "name": "Private Problem",
            "rating": 2000,
            "tags": [],
        })
        asyncio.run(handle(**_kwargs(_make_private_event("/clarify 输入是什么？"))))

    clarify_calls = [call for call in _deepseek_calls if call.get("task") == "clarify"]
    assert clarify_calls, _deepseek_calls
    user_content = clarify_calls[-1]["messages"][-1]["content"]
    assert "PRIVATE_PID_SUMMARY" in user_content, user_content
    assert "GROUP_CURRENT_SUMMARY" not in user_content, user_content
    _cleanup()
    print("✅ private clarify: uses pid-specific summary")


def test_clarify_llm_failure_shows_admin_message():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _fail_clarify(*args, **kwargs):
        return ChatCompletionResult(text=None, failure_kind="service_unavailable")

    with _all_patches(), patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _fail_clarify):
        from kouhai_bot.handlers.cmd.clarify import handle
        asyncio.run(handle(**_kwargs(_make_event("/clarify Joker函数是什么"))))

    assert "模型服务出故障了，联系一下管理员帮帮忙吧～" in _last_text()
    assert ("msg_001", "268") in _reacted
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert records[-1]["type"] == "clarify"
    assert records[-1]["result"] == "service_unavailable"
    assert records[-1]["content"] == "Joker函数是什么"
    _cleanup()
    print("✅ clarify: llm failure shows admin message")


def test_clarify_no_problem():
    """Clarify without problem → appropriate message."""
    _reset_state()

    with _all_patches():
        from kouhai_bot.handlers.cmd.clarify import handle
        asyncio.run(handle(**_kwargs(_make_event("/clarify what is this"))))

    assert "没有" in _last_text(), f"Expected 'no problem', got: {_last_text()}"
    _cleanup()
    print("✅ clarify: no problem")


def test_clarify_alias_dispatches_to_clarify_handler():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = '{"reply": "J(x) 是特殊因子和。", "reaction": ""}'

    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(process_event(_make_event("/clrf Joker函数是什么"), spawn_handlers=False))

    assert "J(x)" in _last_text(), f"Expected clarify alias reply, got: {_last_text()}"
    _cleanup()
    print("✅ clarify alias: /clrf")


def test_clear_removes_current_problem_history_and_reacts_ok():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [],
        "user_submissions": {
            str(UID): [
                {
                    "timestamp": "2026-05-16T12:00:00+08:00",
                    "type": "clarify",
                    "content": "old question",
                    "result": "clarify",
                    "reason": "",
                    "reply": "old reply",
                    "problem": PID,
                },
                {
                    "timestamp": "2026-05-16T12:05:00+08:00",
                    "type": "submit",
                    "content": "other problem solution",
                    "result": "incorrect",
                    "reason": "x",
                    "reply": "y",
                    "problem": PID2,
                },
            ]
        },
    })

    with _all_patches():
        from kouhai_bot.handlers.cmd.clear import handle
        asyncio.run(handle(**_kwargs(_make_event("/clear"))))

    assert not _sent, f"Expected no text reply on successful clear, got: {_sent}"
    assert ("msg_001", "128076") in _reacted, f"Expected OK reaction, got: {_reacted}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert len(records) == 1, f"Expected only non-current-problem history to remain, got: {records}"
    assert records[0]["problem"] == PID2
    _cleanup()
    print("✅ clear: removes current problem history and reacts ok")


def test_clear_no_problem():
    _reset_state()

    with _all_patches():
        from kouhai_bot.handlers.cmd.clear import handle
        asyncio.run(handle(**_kwargs(_make_event("/clear"))))

    assert not _sent, f"Expected no text reply on clear error, got: {_sent}"
    assert ("msg_001", "10060") in _reacted, f"Expected error reaction, got: {_reacted}"
    _cleanup()
    print("✅ clear: no problem")


# ═══════════════════════════════════════════════════════════════════════
# /problem, /tag, /reveal, /scoreboard, /newproblem
# ═══════════════════════════════════════════════════════════════════════

def test_problem_with_data():
    """With problem but no daily_msg.json → shows fallback message."""
    _reset_state(); _setup_problem()
    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))
    assert "暂时无法重新发送" in _last_text(), f"Expected fallback: {_last_text()}"
    _cleanup()
    print("✅ problem: fallback when no daily_msg.json")


def test_problem_rebuilds_forward_card_when_node_ids_are_stale():
    _reset_state()
    _setup_problem()
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "sample_msg_ids": [1112],
        "snake_msg_id": 1113,
        "pid": PID,
        "post_msg": "题目正文",
        "sample_messages": ["样例 1\nInput:\n1\n\nOutput:\n2"],
        "snake_enabled": True,
    })

    async def _fail_old_nodes(group_id, messages):
        if messages and messages[0]["data"]["id"] == "1111":
            return None
        _forwarded.append({"group_id": group_id, "messages": messages})
        return 3000 + len(_forwarded)

    with _all_patches():
        with patch("kouhai_bot.napcat.client.send_group_forward_msg", _fail_old_nodes), \
                patch("kouhai_bot.handlers.cmd.newproblem.send_group_forward_msg", _fail_old_nodes), \
                patch("kouhai_bot.handlers.cmd.newproblem.asyncio.sleep", AsyncMock()):
            from kouhai_bot.handlers.cmd.stubs import handle_problem
            asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))

    assert len(_private_sent) >= 2, f"Expected rebuilt self-sends, got {_private_sent}"
    assert len(_forwarded) == 1, f"Expected rebuilt forward card, got {_forwarded}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "daily_msg.json")) as f:
        saved = json.load(f)
    assert saved["fwd_message_id"] == 3001
    assert saved["msg_id"] != 1111
    with open(os.path.join(_data_dir(), "groups", str(GID), "problem_card_refs.json")) as f:
        refs = json.load(f)
    assert refs["3001"]["problem"] == PID
    _cleanup()
    print("✅ problem: rebuilds stale forward card")


def test_problem_ignores_stale_daily_msg_pid():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID2,
        "post_msg": "旧题正文",
        "sample_messages": ["旧样例"],
        "snake_enabled": True,
    })

    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))

    assert not _forwarded, f"Should not resend stale problem card: {_forwarded}"
    assert "暂时无法重新发送" in _last_text(), f"Expected fallback: {_last_text()}"
    _cleanup()
    print("✅ problem: ignores stale daily_msg pid")


def test_problem_solved_resend_shows_next_problem_hint():
    _reset_state()
    _setup_problem()
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID,
    })
    _write_scoreboard(GID, {
        "solves": [{"user_id": UID, "nickname": "Alice", "problem": PID, "timestamp": time.time()}],
        "user_submissions": {},
    })

    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))

    assert len(_forwarded) == 1, f"Expected problem card resend, got: {_forwarded}"
    text = _last_text()
    assert "这题已经通过" in text and "/newproblem" in text, \
        f"Expected solved problem hint, got: {text}"
    _cleanup()
    print("✅ problem: solved resend shows next-problem hint")


def test_problem_unsolved_resend_does_not_show_next_problem_hint():
    _reset_state()
    _setup_problem()
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID,
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))

    assert len(_forwarded) == 1, f"Expected problem card resend, got: {_forwarded}"
    assert not _sent, f"Unsolved problem should not send next-problem hint: {_sent}"
    _cleanup()
    print("✅ problem: unsolved resend has no next-problem hint")


def test_problem_high_difficulty_resend_warns_after_card():
    _reset_state()
    _setup_problem()
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2901,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
    })
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID,
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))

    assert len(_forwarded) == 1, f"Expected problem card resend, got: {_forwarded}"
    text = _last_text()
    assert "难度较高" in text, text
    assert "题解" in text, text
    _cleanup()
    print("✅ problem: high difficulty resend warns after card")


def test_problem_no_current_problem():
    """No current problem → friendly /newproblem hint."""
    _reset_state()
    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_problem
        asyncio.run(handle_problem(**_kwargs(_make_event("/problem"))))
    text = _last_text()
    assert "暂时不能查看当前题目" in text and "/newproblem" in text, \
        f"Expected no-current-problem hint: {text}"
    _cleanup()
    print("✅ problem: no current problem")


def test_problem_alias_dispatches_to_problem_handler():
    _reset_state()
    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(process_event(_make_event("/pb"), spawn_handlers=False))
    text = _last_text()
    assert "暂时不能查看当前题目" in text and "/newproblem" in text, \
        f"Expected problem alias no-current-problem hint: {text}"
    _cleanup()
    print("✅ problem alias: /pb")


def test_tag_with_tags():
    _reset_state(); _setup_problem()
    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_tag
        asyncio.run(handle_tag(**_kwargs(_make_event("/tag"))))
    assert "number theory" in _last_text().lower(), f"No tags: {_last_text()}"
    _cleanup()
    print("✅ tag: shows tags")


def test_scoreboard_with_data():
    _reset_state(); _setup_problem()
    _write_problem_ratings(GID, {"542D": 2600, "100A": 2000, "200B": 2000})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": "42", "nickname": "OldAlice", "problem": PID, "timestamp": time.time()},
            {"user_id": "99", "nickname": "OldBob", "problem": "200B", "timestamp": time.time() - 100},
            {"user_id": "99", "nickname": "OldBob", "problem": "100A", "timestamp": time.time() - 50},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_scoreboard
        asyncio.run(handle_scoreboard(**_kwargs(_make_event("/scoreboard"))))
    assert len(_private_sent) == 1, f"Expected scoreboard self-send, got {_private_sent}"
    text = _private_sent[0]["message"][0]["data"]["text"]
    lines = text.splitlines()
    assert lines[:3] == [
        "📊 累计解题排行榜（共 2 人）",
        "计分公式：2000=1 分，每 +300 分翻倍（2^((rating-2000)/300)）",
        "",
    ], f"Scoreboard header wrong: {text}"
    assert any("FreshAlice" in line for line in lines), f"Fresh nickname missing: {text}"
    assert any("FreshBob" in line for line in lines), f"Fresh nickname missing: {text}"
    assert "OldAlice" not in text and "OldBob" not in text, f"Used stale nickname: {text}"
    assert any(line.startswith("#1 FreshAlice") and "1 题 / 4 分" in line for line in lines), f"Wrong first rank: {text}"
    assert any(line.startswith("#2 FreshBob") and "2 题 / 2 分" in line for line in lines), f"Wrong second rank: {text}"
    assert len(_forwarded) == 1, f"Expected one scoreboard forward card, got {_forwarded}"
    _cleanup()
    print("✅ scoreboard: refreshes nicknames and shows points")


def test_scoreboard_same_score_shares_rank():
    _reset_state(); _setup_problem()
    _write_problem_ratings(GID, {"542D": 2600, "101A": 2300, "102A": 2300})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": "42", "nickname": "OldAlice", "problem": PID, "timestamp": time.time()},
            {"user_id": "99", "nickname": "OldBob", "problem": "101A", "timestamp": time.time() - 100},
            {"user_id": "99", "nickname": "OldBob", "problem": "102A", "timestamp": time.time() - 50},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    with _all_patches():
        from kouhai_bot.handlers.cmd.stubs import handle_scoreboard
        asyncio.run(handle_scoreboard(**_kwargs(_make_event("/scoreboard"))))
    text = _private_sent[0]["message"][0]["data"]["text"]
    full_lines = text.splitlines()
    assert full_lines[:3] == [
        "📊 累计解题排行榜（共 2 人）",
        "计分公式：2000=1 分，每 +300 分翻倍（2^((rating-2000)/300)）",
        "",
    ], f"Scoreboard header wrong: {text}"
    lines = [line for line in full_lines if line.startswith("#")]
    assert any(line.startswith("#1 FreshAlice") and "1 题 / 4 分" in line for line in lines), f"Alice rank wrong: {text}"
    assert any(line.startswith("#1 FreshBob") and "2 题 / 4 分" in line for line in lines), f"Bob rank wrong: {text}"
    _cleanup()
    print("✅ scoreboard: same score shares rank")


def test_scoreboard_splits_default_and_starred_groups():
    _reset_state(); _setup_problem()
    _write_problem_ratings(GID, {"542D": 2600, "100A": 2000})
    _write_scoreboard(GID, {
        "solves": [
            {"user_id": UID, "nickname": "OldAlice", "problem": PID, "timestamp": time.time()},
            {"user_id": OTHER_UID, "nickname": "OldBob", "problem": "100A", "timestamp": time.time() - 100},
        ],
        "user_submissions": {},
    })
    _set_group_members(GID, [
        {"user_id": UID, "nickname": "FreshAlice", "card": ""},
        {"user_id": OTHER_UID, "nickname": "FreshBob", "card": ""},
    ])
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[OTHER_UID],
                submit_delay_sec=1800,
            )
        ],
    }
    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.stubs import handle_scoreboard
        asyncio.run(handle_scoreboard(**_kwargs(_make_event("/scoreboard"))))

    text = _private_sent[0]["message"][0]["data"]["text"]
    lines = text.splitlines()
    default_idx = next(i for i, line in enumerate(lines) if line.startswith("#1 FreshAlice"))
    starred_header_idx = lines.index("📊 打星排行榜")
    starred_idx = next(i for i, line in enumerate(lines) if line.startswith("#1 FreshBob"))
    assert default_idx < starred_header_idx < starred_idx, f"Wrong group order: {text}"
    assert "#2 FreshBob" not in text, f"Starred user should not be ranked in default board: {text}"
    assert "FreshAlice — 1 题 / 4 分" in text, f"Default entry missing: {text}"
    assert "FreshBob — 1 题 / 1 分" in text, f"Starred entry missing: {text}"
    _cleanup()
    print("✅ scoreboard: splits default and starred groups")


def test_help_shows_short_aliases_and_configured_newproblem_cooldown():
    _reset_state()
    lazy = _LazyConfig()
    lazy._config = {**_config_dict(), "newproblem_cooldown": 90}
    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.help import handle
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(handle(**_kwargs(_make_event("/help"))))

    assert _private_sent, "Expected help to self-send before forward"
    msg = _private_sent[0]["message"]
    text = " ".join(
        seg.get("data", {}).get("text", "")
        for seg in msg if isinstance(seg, dict) and seg.get("type") == "text"
    )
    assert "/newproblem(/np) [--force] — 刷一道新题（未解须 --force；90秒冷却）" in text, text
    assert "/problem(/pb) — 重新查看当前题目" in text, text
    assert "/submit(/sbm) 你的做法 — 提交做法，AI 判定对错" in text, text
    assert "/tag — 查看当前题目的算法标签" in text, text
    assert "/review(/rv) 你的问题 — 默认复盘上一道已通过题；引用题目卡片可复盘旧题；@群友可带入其上下文" in text, text
    assert "/clarify(/clrf) 你的问题 — 向AI澄清题目细节，只回答题目本身不剧透做法" in text, text
    assert "/setproblem(/sp)" not in text, text
    assert "/sync —" not in text, text
    assert "/testcd —" not in text, text
    assert "private judge" in text and "详细用法请私聊发 /help" in text, text
    _cleanup()
    print("✅ help: aliases and dynamic cooldown")


def test_private_help_only_shows_private_judge_commands():
    _reset_state()
    with _all_patches():
        from kouhai_bot.handlers.cmd.help import handle
        from kouhai_bot.handlers.registry import discover_commands
        discover_commands()
        asyncio.run(handle(**_kwargs(_make_private_event("/help"))))

    assert _private_forwarded, "Expected private help to be sent as a forward card"
    assert _private_forwarded[-1]["user_id"] == UID, _private_forwarded
    self_sends = [item for item in _private_sent if item["user_id"] == 1]
    assert self_sends, "Expected private help to self-send before forward"
    msg = self_sends[-1]["message"]
    text = " ".join(
        seg.get("data", {}).get("text", "")
        for seg in msg if isinstance(seg, dict) and seg.get("type") == "text"
    )
    assert "/setproblem(/sp) [题号|链接|random] — 设置 private judge 当前题" in text, text
    assert "/sync — 在群聊和 private judge 间同步当前群题记录" in text, text
    assert "/testcd — 查看当前群题提交 CD" in text, text
    assert "/newproblem(/np)" not in text, text
    assert "/scoreboard" not in text, text
    assert "可能丢掉当前侧这题交流历史" in text, text
    _cleanup()
    print("✅ private help: filters group-only commands")


def test_private_testcd_allows_submit_when_no_cooldown_and_dispatches():
    _reset_state()
    _setup_problem_for(GID, PID)

    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.registry import discover_commands

        discover_commands()
        asyncio.run(process_event(_make_private_event("/testcd"), spawn_handlers=False))

    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "你现在可以提交当前群内的题目！" in private_text, private_text
    _cleanup()
    print("✅ private testcd: no cooldown allows submit")


def test_private_testcd_shows_remaining_for_starred_user():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": 1000,
    })

    with _all_patches(), patch("kouhai_bot.config._config", _starred_config_for_user()), patch(
        "kouhai_bot.user_groups.time.time",
        return_value=1000,
    ):
        from kouhai_bot.handlers.cmd.testcd import handle
        asyncio.run(handle(**_kwargs(_make_private_event("/testcd"))))

    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "你在5分钟后才能提交当前群内的题目，先休息一下吧～" in private_text, private_text
    _cleanup()
    print("✅ private testcd: shows starred cooldown")


def test_private_testcd_formats_multi_unit_remaining():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": 1000,
    })

    with _all_patches(), patch(
        "kouhai_bot.config._config",
        _starred_config_for_user(submit_delay_sec=90061),
    ), patch("kouhai_bot.user_groups.time.time", return_value=1000):
        from kouhai_bot.handlers.cmd.testcd import handle
        asyncio.run(handle(**_kwargs(_make_private_event("/testcd"))))

    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "你在1天1小时1分钟1秒后才能提交当前群内的题目，先休息一下吧～" in private_text, private_text
    _cleanup()
    print("✅ private testcd: formats multi-unit cooldown")


def test_private_testcd_allows_after_cooldown_expires():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": 699,
    })

    with _all_patches(), patch("kouhai_bot.config._config", _starred_config_for_user()), patch(
        "kouhai_bot.user_groups.time.time",
        return_value=1000,
    ):
        from kouhai_bot.handlers.cmd.testcd import handle
        asyncio.run(handle(**_kwargs(_make_private_event("/testcd"))))

    private_text = "\n".join(_last_text_item(item) for item in _private_sent if item["user_id"] == UID)
    assert "你现在可以提交当前群内的题目！" in private_text, private_text
    _cleanup()
    print("✅ private testcd: allows after cooldown expires")


def test_newproblem_cooldown():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{"user_id": UID, "nickname": "Alice", "date": "2026-05-14",
                    "problem": PID, "order": 1}],
        "user_submissions": {},
    })
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import handle, _cooldowns
        _cooldowns.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            ev = _kwargs(_make_event("/newproblem"))
            asyncio.run(handle(**ev))
            asyncio.run(handle(**ev))
    assert ran == [GID], f"Expected one post after solved bypass, got {ran}"
    assert "太频繁" in _last_text(), f"No cooldown: {_last_text()}"
    _cleanup()
    print("✅ newproblem: cooldown")


def test_newproblem_busy_rejects_concurrent_force():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import (
            handle,
            _cooldowns,
            _newproblem_active,
            _newproblem_locks,
        )
        _cooldowns.clear()
        _newproblem_active.clear()
        _newproblem_locks.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            await asyncio.sleep(0.05)
            return True

        async def _run():
            ev = _kwargs(_make_event("/newproblem --force"))
            await asyncio.gather(handle(**ev), handle(**ev))

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(_run())
    assert ran == [GID], f"Concurrent force should post once: {ran}"
    assert "正在准备中" in _last_text(), f"No busy rejection: {_last_text()}"
    _cleanup()
    print("✅ newproblem --force: concurrent busy rejection")


def test_newproblem_unsolved_rejects():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import handle, _cooldowns
        _cooldowns.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(handle(**_kwargs(_make_event("/newproblem"))))
    assert not ran, f"Unsolved should not post: {ran}"
    assert "/newproblem --force" in _last_text(), _last_text()
    assert "/problem" in _last_text(), _last_text()
    _cleanup()
    print("✅ newproblem: unsolved rejected")


def test_newproblem_force_posts_when_unsolved():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import handle, _cooldowns
        _cooldowns.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(handle(**_kwargs(_make_event("/newproblem --force"))))
    assert ran == [GID], f"Force should post: {ran}"
    _cleanup()
    print("✅ newproblem --force: posts when unsolved")


def test_newproblem_alias_force_posts_when_unsolved():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    with _all_patches():
        from kouhai_bot.handlers import process_event
        from kouhai_bot.handlers.cmd.newproblem import _cooldowns
        from kouhai_bot.handlers.registry import discover_commands
        _cooldowns.clear()
        discover_commands()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(process_event(_make_event("/np --force"), spawn_handlers=False))
    assert ran == [GID], f"Alias force should post: {ran}"
    _cleanup()
    print("✅ newproblem alias: /np --force")


def test_newproblem_force_requires_space():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import handle, _cooldowns
        _cooldowns.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(handle(**_kwargs(_make_event("/newproblem--force"))))
    assert not ran, f"Missing space should not post: {ran}"
    assert "用法" in _last_text(), _last_text()
    _cleanup()
    print("✅ newproblem --force: requires space before flag")


def test_newproblem_solved_posts():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{"user_id": UID, "nickname": "Alice", "date": "2026-05-14",
                    "problem": PID, "order": 1}],
        "user_submissions": {},
    })
    with _all_patches():
        from kouhai_bot.handlers.cmd.newproblem import handle, _cooldowns
        _cooldowns.clear()
        ran: list[int] = []

        async def _mock_post(gid, prefix=None, **_):
            ran.append(gid)
            return True

        with patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            asyncio.run(handle(**_kwargs(_make_event("/newproblem"))))
    assert ran == [GID], f"Solved should post: {ran}"
    _cleanup()
    print("✅ newproblem: solved posts")


def test_newproblem_commit_settles_dynamic_wait_for_previous_problem():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{"user_id": UID, "nickname": "Alice", "date": "2026-05-14",
                    "problem": PID, "order": 1}],
        "user_submissions": {},
    })
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID, OTHER_UID],
                submit_delay_sec=300,
            )
        ],
    }

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.newproblem import _commit_problem_state
        asyncio.run(_commit_problem_state(GID, {
            "today": PID2,
            "contestId": 100,
            "index": "A",
            "name": "Next",
            "rating": 2000,
        }))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        sb = json.load(f)
    users = sb["user_group_waits"]["groups"]["starred"]["users"]
    assert users[str(UID)]["wait_sec"] == 600
    assert users[str(OTHER_UID)]["wait_sec"] == 300
    assert sb["user_group_waits"]["settled_problems"][PID]
    _cleanup()
    print("✅ newproblem: commit settles dynamic wait for previous solve")


def test_newproblem_commit_does_not_settle_unsolved_previous_problem():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=300,
            )
        ],
    }

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.newproblem import _commit_problem_state
        asyncio.run(_commit_problem_state(GID, {
            "today": PID2,
            "contestId": 100,
            "index": "A",
            "name": "Next",
            "rating": 2000,
        }))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        sb = json.load(f)
    assert "user_group_waits" not in sb or not sb["user_group_waits"].get("settled_problems")
    _cleanup()
    print("✅ newproblem: unsolved previous problem does not change dynamic wait")


def test_newproblem_notify_group_on_pick_failure():
    """After 3 failed pick attempts, notify_group=True sends error msg."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _mock_pick_fail(*args, **kwargs):
        """Fail the pick-json subprocess, succeed reveal."""
        cmd_args = args[1:]  # skip 'python' executable
        if any("reveal" in str(a) for a in cmd_args):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc
        if any("pick-json" in str(a) for a in cmd_args):
            proc = AsyncMock()
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"SSL EOF"))
            return proc
        raise RuntimeError(f"unexpected subprocess: {cmd_args}")

    with _all_patches(), patch(
        "asyncio.create_subprocess_exec", _mock_pick_fail
    ), patch(
        "asyncio.sleep", AsyncMock()
    ):
        from kouhai_bot.handlers.cmd.newproblem import _do_daily_post_locked

        # Test 1: notify_group=True → should send error message
        _sent.clear()
        asyncio.run(_do_daily_post_locked(GID, notify_group=True))
        assert _has_sent("刷题失败了"), f"No error msg: {_last_text()}"
        assert _has_sent("3 次"), f"No retry mention: {_last_text()}"

        # Test 2: notify_group=False → should NOT send error message
        _sent.clear()
        asyncio.run(_do_daily_post_locked(GID, notify_group=False))
        assert not _sent, f"Unexpected msg with notify_group=False: {_last_text()}"

    _cleanup()
    print("✅ newproblem: pick failure notifies group when notify_group=True")


def test_newproblem_fallback_direct_saves_daily_msg_for_current_pid():
    _reset_state()
    _setup_problem_for(GID, PID)
    _setup_problem_for(GID, PID2)
    _write_state(GID, {"today": PID, "contestId": 542, "index": "D"})
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID,
        "post_msg": "旧题正文",
        "sample_messages": ["旧样例"],
        "snake_enabled": True,
    })

    picked = {
        "today": PID2,
        "contestId": 100,
        "index": "A",
        "name": "Next",
        "rating": 2000,
        "tags": ["dp"],
    }

    async def _mock_subprocess(*args, **kwargs):
        cmd_args = args[1:]
        proc = AsyncMock()
        if any("reveal" in str(a) for a in cmd_args):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc
        if any("pick-json" in str(a) for a in cmd_args):
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(json.dumps(picked).encode(), b""))
            return proc
        raise RuntimeError(f"unexpected subprocess: {cmd_args}")

    async def _fail_forward(group_id, messages):
        return None

    with _all_patches(), \
            patch("asyncio.create_subprocess_exec", _mock_subprocess), \
            patch("kouhai_bot.handlers.cmd.newproblem.send_group_forward_msg", _fail_forward), \
            patch("kouhai_bot.handlers.cmd.newproblem.asyncio.sleep", AsyncMock()):
        from kouhai_bot.handlers.cmd.newproblem import _do_daily_post_locked
        posted = asyncio.run(_do_daily_post_locked(GID, prefix="刷新了一道新题🌟", notify_group=True))

    assert posted is True
    assert _has_sent("刷新了一道新题"), _sent
    with open(os.path.join(_data_dir(), "groups", str(GID), "state.json")) as f:
        state = json.load(f)
    assert state["today"] == PID2
    with open(os.path.join(_data_dir(), "groups", str(GID), "daily_msg.json")) as f:
        daily = json.load(f)
    assert daily["pid"] == PID2, daily
    assert daily["post_msg"].startswith("刷新了一道新题"), daily
    assert "sample_messages" in daily
    assert "fwd_message_id" not in daily
    _cleanup()
    print("✅ newproblem: fallback direct saves daily_msg for current pid")


def test_submit_scoreboard_update_settles_late_old_problem():
    _reset_state()
    _setup_problem_for(GID, PID2)
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID, OTHER_UID],
                submit_delay_sec=300,
            )
        ],
    }

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import _update_scoreboard_for_pid
        _update_scoreboard_for_pid(
            GID,
            UID,
            "Alice",
            PID,
            {"today": PID, "rating": 2600},
        )

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        sb = json.load(f)
    users = sb["user_group_waits"]["groups"]["starred"]["users"]
    assert users[str(UID)]["wait_sec"] == 600
    assert users[str(OTHER_UID)]["wait_sec"] == 300
    assert sb["user_group_waits"]["settled_problems"][PID]
    _cleanup()
    print("✅ submit: late old-problem AC settles dynamic wait")


def test_newproblem_picker_args_follow_env_rating_range():
    _reset_state()
    with _all_patches(), patch.dict(_LazyConfig._config, {"min_rating": 2100, "max_rating": 2900}):
        from kouhai_bot.handlers.cmd.newproblem import _picker_args

        args = _picker_args("pick", GID, "--with-statement")

    assert "--min-rating" in args and "--max-rating" in args, f"Missing rating flags: {args}"
    assert args[args.index("--min-rating") + 1] == "2100", f"Wrong min rating args: {args}"
    assert args[args.index("--max-rating") + 1] == "2900", f"Wrong max rating args: {args}"
    _cleanup()
    print("✅ newproblem: picker args follow env rating range")


def test_newproblem_picker_args_prefer_scheduler_override():
    _reset_state()
    with _all_patches(), \
            patch.dict(_LazyConfig._config, {"min_rating": 2000, "max_rating": 3000}), \
            patch("kouhai_bot.scheduler.engine.load_group_configs", return_value={
                GID: MagicMock(min_rating=2300, max_rating=2700)
            }):
        from kouhai_bot.handlers.cmd.newproblem import _picker_args

        args = _picker_args("pick", GID, "--with-statement")

    assert args[args.index("--min-rating") + 1] == "2300", f"Scheduler min override ignored: {args}"
    assert args[args.index("--max-rating") + 1] == "2700", f"Scheduler max override ignored: {args}"
    _cleanup()
    print("✅ newproblem: picker args prefer scheduler override")


def test_status_ignores_other_groups_and_reports_idle():
    _reset_state()
    with _all_patches():
        with patch("kouhai_bot.handlers.cmd.submit.get_group_lock_status", return_value=None):
            from kouhai_bot.handlers.cmd.stubs import handle_status
            asyncio.run(handle_status(**_kwargs(_make_event("/status"))))
    assert "当前空闲" in _last_text(), f"Expected idle status, got: {_last_text()}"
    _cleanup()
    print("✅ status: only considers current group")


def test_status_reports_newproblem_busy():
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {
        "solves": [{"user_id": UID, "nickname": "Alice", "date": "2026-05-14",
                    "problem": PID, "order": 1}],
        "user_submissions": {},
    })
    started = asyncio.Event()
    release = asyncio.Event()

    async def _mock_post(gid, prefix=None, **_):
        started.set()
        await release.wait()
        return True

    async def _run():
        with _all_patches(), \
                patch("kouhai_bot.handlers.cmd.submit.get_group_lock_status", return_value=None), \
                patch("kouhai_bot.handlers.cmd.newproblem._do_daily_post_locked", _mock_post):
            from kouhai_bot.handlers.cmd.newproblem import (
                handle as newproblem_handle,
                _cooldowns,
                _newproblem_active,
                _newproblem_locks,
            )
            from kouhai_bot.handlers.cmd.stubs import handle_status
            _cooldowns.clear()
            _newproblem_active.clear()
            _newproblem_locks.clear()
            task = asyncio.create_task(newproblem_handle(**_kwargs(_make_event(
                "/newproblem", message_id="np_busy"
            ))))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            await handle_status(**_kwargs(_make_event("/status", message_id="status_busy")))
            release.set()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_run())

    assert any(
        "newproblem" in (
            " ".join(
                seg.get("data", {}).get("text", "")
                for seg in item.get("message", [])
                if seg.get("type") == "text"
            )
        )
        for item in _sent
    ), f"Expected status to report newproblem busy: {_sent}"
    _cleanup()
    print("✅ status: reports newproblem busy")


def test_submit_parallel_replies_follow_completion_order():
    """Incorrect submit replies should not wait for earlier slow judges."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    allow_first = asyncio.Event()

    async def _ordered_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        if submission == "first solution":
            await allow_first.wait()
            return json.dumps({"correct": False, "reason": "R1", "reply": "FIRST"})
        if submission == "second solution":
            allow_first.set()
            return json.dumps({"correct": False, "reason": "R2", "reply": "SECOND"})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_ordered_deepseek)):
                from kouhai_bot.handlers.cmd.submit import handle
                ev1 = _kwargs(_make_event("/submit first solution", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit second solution", group_id=GID, user_id=OTHER_UID))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.gather(t1, t2)

    asyncio.run(_run())

    texts = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "FIRST" in text or "SECOND" in text:
            texts.append(text)

    assert len(texts) == 2, f"Expected 2 ordered replies, got: {texts}"
    assert "SECOND" in texts[0] and "FIRST" in texts[1], f"Replies should follow completion order: {texts}"
    _cleanup()
    print("✅ submit: same-group incorrect replies do not wait for earlier judges")


def test_submit_parallel_late_wrong_results_are_reused_after_first_solve():
    """Concurrent wrong submits queued behind a correct one should use judge replies."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    allow_first = asyncio.Event()

    async def _mixed_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                              response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        if submission == "first correct":
            await allow_first.wait()
            return json.dumps({"correct": True, "reason": "ok", "reply": ""})
        if submission == "second wrong":
            return json.dumps({"correct": False, "reason": "R2", "reply": "SECOND WRONG"})
        if submission == "third wrong":
            allow_first.set()
            return json.dumps({"correct": False, "reason": "R3", "reply": "THIRD WRONG"})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_mixed_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")):
                from kouhai_bot.handlers.cmd.submit import handle
                ev1 = _kwargs(_make_event("/submit first correct", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit second wrong", group_id=GID, user_id=OTHER_UID))
                ev3 = _kwargs(_make_event("/submit third wrong", group_id=GID, user_id=UID2))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.sleep(0)
                t3 = asyncio.create_task(handle(**ev3))
                await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=1.0)

    asyncio.run(_run())

    texts = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "本题 +" in text or "SECOND WRONG" in text or "THIRD WRONG" in text or "已经有人解出" in text:
            texts.append(text)

    assert len(texts) == 3, f"Expected 3 submit replies, got: {texts}"
    assert "SECOND WRONG" in texts[0], f"Second wrong should reply before slow first solve: {texts}"
    assert "THIRD WRONG" in texts[1], f"Third wrong should reply before slow first solve: {texts}"
    assert "本题 +4 分" in texts[2], f"First solve should settle scoreboard later: {texts}"
    assert all("已经有人解出" not in text for text in texts), f"Unexpected already-solved reply: {texts}"

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert len(saved["solves"]) == 1
    assert saved["user_submissions"][str(OTHER_UID)][-1]["result"] == "incorrect"
    assert saved["user_submissions"][str(UID2)][-1]["result"] == "incorrect"
    _cleanup()
    print("✅ submit: late wrong concurrent results are reused after first solve")


def test_submit_parallel_late_correct_result_does_not_update_scoreboard_twice():
    """A later concurrent correct submit should be recorded but not counted again."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    allow_first = asyncio.Event()

    async def _both_correct_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                     response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        if submission == "first correct":
            await allow_first.wait()
            return json.dumps({"correct": True, "reason": "first ok", "reply": ""})
        if submission == "second correct":
            allow_first.set()
            return json.dumps({"correct": True, "reason": "second ok", "reply": ""})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_both_correct_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")):
                from kouhai_bot.handlers.cmd.submit import handle
                ev1 = _kwargs(_make_event("/submit first correct", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit second correct", group_id=GID, user_id=OTHER_UID))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    texts = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "做法被判定为正确" in text or "本题 +" in text:
            texts.append(text)

    assert any("更早发出的提交正在判题" in text for text in texts), \
        f"Expected waiting-for-earlier explanation, got: {texts}"
    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert len(saved["solves"]) == 1
    assert str(saved["solves"][0]["user_id"]) == str(UID)
    assert saved["user_submissions"][str(UID)][-1]["result"] == "correct"
    assert saved["user_submissions"][str(OTHER_UID)][-1]["result"] == "correct"
    _cleanup()
    print("✅ submit: late correct concurrent result is recorded without double scoring")


def test_submit_same_user_includes_previous_submit_history():
    """A later submit from the same user should see the earlier submit in history."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _history_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        history = payload["history"] or []
        if submission == "first solution":
            assert history == [], f"First submit should have empty history, got: {history}"
            return json.dumps({"correct": False, "reason": "R1", "reply": "FIRST"})
        if submission == "second solution":
            assert any(
                item.get("content") == "first solution"
                and item.get("reply") == "FIRST"
                and item.get("problem") == PID
                for item in history
            ), f"Second submit missed first submit history: {history}"
            return json.dumps({"correct": False, "reason": "R2", "reply": "SECOND"})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_history_deepseek)):
                from kouhai_bot.handlers.cmd.submit import handle
                ev1 = _kwargs(_make_event("/submit first solution", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit second solution", group_id=GID, user_id=UID))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    _cleanup()
    print("✅ submit: same-user submit history is chained")


def test_submit_same_user_later_submit_drops_unanswered_previous_submit():
    """A same-user same-problem submit drops judging but keeps prior text as context."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()

    async def _drop_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                             response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        history = payload["history"] or []
        if submission == "first solution":
            first_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                first_cancelled.set()
        if submission == "second solution":
            superseded = [
                item.get("content") == "first solution"
                and item.get("result") == "superseded"
                and item.get("problem") == PID
                for item in history
            ]
            assert sum(1 for matched in superseded if matched) == 1, \
                f"Dropped submit should remain visible once as superseded context: {history}"
            return json.dumps({"correct": False, "reason": "R2", "reply": "SECOND"})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            from kouhai_bot.eventlog import EVENT_META_KEY, log_command_received, load_events
            from kouhai_bot.handlers.cmd.submit import handle

            ev1_raw = _make_event("/submit first solution", group_id=GID, user_id=UID, message_id="drop_1")
            ev2_raw = _make_event("/submit second solution", group_id=GID, user_id=UID, message_id="drop_2")
            ev1_raw[EVENT_META_KEY] = log_command_received(
                group_id=GID,
                user_id=UID,
                sender=ev1_raw["sender"],
                command="submit",
                message_id=ev1_raw["message_id"],
                raw_text=ev1_raw["raw_message"],
            )
            stale_request_id = ev1_raw[EVENT_META_KEY]["request_id"]
            event_date = ev1_raw[EVENT_META_KEY]["date"]

            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_drop_deepseek)):
                t1 = asyncio.create_task(handle(**_kwargs(ev1_raw)))
                await asyncio.wait_for(first_started.wait(), timeout=1.0)
                t2 = asyncio.create_task(handle(**_kwargs(ev2_raw)))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
                await asyncio.sleep(0)

            return [
                item for item in load_events(GID, event_date)
                if item.get("request_id") == stale_request_id
                and item.get("type") == "finished"
            ]

    stale_events = asyncio.run(_run())

    texts = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "FIRST" in text or "SECOND" in text:
            texts.append(text)
    assert texts == [" SECOND"], f"Only the second submit should reply, got: {texts}"
    assert first_cancelled.is_set(), "First submit task should be cancelled locally"
    assert stale_events and stale_events[-1]["status"] == "stale", stale_events

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"].get(str(UID), [])
    assert [item["content"] for item in records] == ["first solution", "second solution"], records
    assert records[0]["result"] == "superseded", records
    assert records[0]["reason"] == "", records
    assert records[0]["reply"] == "", records
    assert records[1]["result"] == "incorrect", records

    _cleanup()
    print("✅ submit: later same-user submit drops unanswered previous submit")


def test_clear_drops_unanswered_same_user_submit():
    """A same-user /clear drops an earlier unanswered submit for the current problem."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    first_started = asyncio.Event()

    async def _slow_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                             response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        if payload["submission"] == "first solution":
            first_started.set()
            await asyncio.Event().wait()
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            from kouhai_bot.eventlog import EVENT_META_KEY, log_command_received, load_events
            from kouhai_bot.handlers.cmd.clear import handle as clear_handle
            from kouhai_bot.handlers.cmd.submit import handle as submit_handle

            ev1_raw = _make_event("/submit first solution", group_id=GID, user_id=UID, message_id="clear_drop_1")
            ev1_raw[EVENT_META_KEY] = log_command_received(
                group_id=GID,
                user_id=UID,
                sender=ev1_raw["sender"],
                command="submit",
                message_id=ev1_raw["message_id"],
                raw_text=ev1_raw["raw_message"],
            )
            stale_request_id = ev1_raw[EVENT_META_KEY]["request_id"]
            event_date = ev1_raw[EVENT_META_KEY]["date"]
            ev2 = _kwargs(_make_event("/clear", group_id=GID, user_id=UID, message_id="clear_drop_2"))

            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_slow_deepseek)):
                t1 = asyncio.create_task(submit_handle(**_kwargs(ev1_raw)))
                await asyncio.wait_for(first_started.wait(), timeout=1.0)
                t2 = asyncio.create_task(clear_handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
                await asyncio.sleep(0)

            return [
                item for item in load_events(GID, event_date)
                if item.get("request_id") == stale_request_id
                and item.get("type") == "finished"
            ]

    stale_events = asyncio.run(_run())

    assert not _sent, f"Discarded submit should not send a verdict: {_sent}"
    assert ("clear_drop_2", "128076") in _reacted, f"Clear should still react OK: {_reacted}"
    assert stale_events and stale_events[-1]["status"] == "stale", stale_events

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["user_submissions"].get(str(UID), []) == []

    _cleanup()
    print("✅ clear: drops unanswered same-user submit")


def test_dropping_unanswered_submit_unblocks_score_resolution():
    """Discarding an earlier unresolved candidate should let later correct submits score."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    first_started = asyncio.Event()

    async def _mixed_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                              response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        if submission == "first maybe correct":
            first_started.set()
            await asyncio.Event().wait()
        if submission == "second correct":
            return json.dumps({"correct": True, "reason": "second ok", "reply": ""})
        return json.dumps({"correct": False, "reason": "RX", "reply": "OTHER"})

    async def _run():
        with _all_patches(), \
                patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_mixed_deepseek)), \
                patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")):
            from kouhai_bot.handlers.cmd.clear import handle as clear_handle
            from kouhai_bot.handlers.cmd.submit import handle as submit_handle

            t1 = asyncio.create_task(submit_handle(**_kwargs(_make_event(
                "/submit first maybe correct", group_id=GID, user_id=UID, message_id="unblock_1"
            ))))
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            t2 = asyncio.create_task(submit_handle(**_kwargs(_make_event(
                "/submit second correct", group_id=GID, user_id=OTHER_UID, message_id="unblock_2"
            ))))
            for _ in range(20):
                if _has_sent("更早发出的提交正在判题"):
                    break
                await asyncio.sleep(0.01)
            assert _has_sent("更早发出的提交正在判题"), f"Later correct submit should wait first: {_sent}"
            t3 = asyncio.create_task(clear_handle(**_kwargs(_make_event(
                "/clear", group_id=GID, user_id=UID, message_id="unblock_3"
            ))))
            await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=1.0)

    asyncio.run(_run())

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert len(saved["solves"]) == 1, saved
    assert str(saved["solves"][0]["user_id"]) == str(OTHER_UID), saved
    assert saved["user_submissions"].get(str(UID), []) == []
    assert saved["user_submissions"][str(OTHER_UID)][-1]["result"] == "correct"

    _cleanup()
    print("✅ submit: dropping unanswered candidate unblocks scoring")


def test_review_history_formats_types_and_submit_numbers():
    """Review context should separate interaction numbers from submit numbers."""
    from kouhai_bot.handlers.cmd.submit import _build_review_history

    history = [
        {
            "content": "要判无解吗",
            "result": "clarify",
            "reply": "要的",
        },
        {
            "content": "wrong idea",
            "result": "incorrect",
            "reason": "bad condition",
            "reply": "try again",
        },
        {
            "type": "submit",
            "content": "fixed idea",
            "result": "correct",
            "reason": "ok",
            "reply": "",
        },
    ]

    text = _build_review_history(history)

    assert "统计：clarify=1，submit=2（correct=1，incorrect=1），review=0。" in text
    assert "--- 交互 #1 | type=clarify ---" in text
    assert "--- 交互 #2 | type=submit | submit #1 | result=incorrect ---" in text
    assert "--- 交互 #3 | type=submit | submit #2 | result=correct ---" in text
    assert "提交 #2 [incorrect]" not in text
    print("✅ review: history format separates interaction and submit numbers")


def test_user_submission_history_is_unbounded_and_upserts_by_request_id():
    _reset_state()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    from kouhai_bot.handlers.shared import load_user_submissions, save_user_submission

    for i in range(25):
        save_user_submission(GID, UID, {
            "timestamp": f"2026-05-14T00:00:{i:02d}+08:00",
            "type": "submit",
            "content": f"idea {i}",
            "result": "incorrect",
            "reason": "",
            "reply": "",
            "problem": PID,
            "request_id": f"req-{i}",
        })
    save_user_submission(GID, UID, {
        "timestamp": "2026-05-14T00:00:03+08:00",
        "type": "submit",
        "content": "idea 3 updated",
        "result": "superseded",
        "reason": "",
        "reply": "",
        "problem": PID,
        "request_id": "req-3",
    })

    records = load_user_submissions(GID, UID)
    assert len(records) == 25, records
    assert records[0]["content"] == "idea 0", records
    assert records[3]["content"] == "idea 3 updated", records
    assert records[3]["result"] == "superseded", records
    _cleanup()
    print("✅ history: user submissions are unbounded and upsert by request id")


def test_clarify_prompt_hides_original_problem_identity():
    from kouhai_bot.handlers.cmd.submit import CLARIFY_PROMPT

    assert "不要透露原题是哪一道" in CLARIFY_PROMPT
    assert "题号" in CLARIFY_PROMPT and "题目名" in CLARIFY_PROMPT
    assert "比赛编号" in CLARIFY_PROMPT
    print("✅ clarify: prompt hides original problem identity")


def test_submit_same_user_includes_previous_clarify_history():
    """A submit should wait for and include the same user's earlier clarify record."""
    _reset_state()
    _setup_problem()
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})

    async def _mixed_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                              response_format=None, thinking=None):
        content = messages[-1]["content"]
        if content.startswith("{"):
            payload = json.loads(content)
            history = payload["history"] or []
            assert any(
                item.get("result") in {"clarify", "pending"}
                and item.get("content") == "输入格式是啥"
                and item.get("problem") == PID
                for item in history
            ), f"Submit missed pending clarify context: {history}"
            return json.dumps({"correct": False, "reason": "R1", "reply": "WITH CLARIFY"})

        await asyncio.sleep(0.05)
        return json.dumps({"reply": "先确认输入格式", "reaction": ""})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _wrap_llm_result(_mixed_deepseek)), \
                    patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_mixed_deepseek)):
                from kouhai_bot.handlers.cmd.clarify import handle as clarify_handle
                from kouhai_bot.handlers.cmd.submit import handle as submit_handle
                ev1 = _kwargs(_make_event("/clarify 输入格式是啥", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit 我想先确认输入格式再做", group_id=GID, user_id=UID))
                t1 = asyncio.create_task(clarify_handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(submit_handle(**ev2))
                await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)

    asyncio.run(_run())

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    records = saved["user_submissions"][str(UID)]
    assert any(item["type"] == "clarify" and item["result"] == "clarify" for item in records)
    assert any(item["type"] == "submit" and item["result"] == "incorrect" for item in records)

    _cleanup()
    print("✅ submit: same-user pending clarify context is visible")


def test_submit_parallel_different_groups_do_not_block():
    """Concurrent submits in different groups should not wait for each other's final reply order."""
    _reset_state()
    _setup_problem_for(GID, PID)
    _setup_problem_for(GID2, PID2)
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    _write_scoreboard(GID2, {"solves": [], "user_submissions": {}})

    release_slow = asyncio.Event()

    async def _grouped_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                                response_format=None, thinking=None):
        payload = json.loads(messages[-1]["content"])
        submission = payload["submission"]
        if submission == "slow group1":
            await release_slow.wait()
            return json.dumps({"correct": False, "reason": "slow", "reply": "SLOW"})
        if submission == "fast group2":
            return json.dumps({"correct": False, "reason": "fast", "reply": "FAST"})
        return json.dumps({"correct": False, "reason": "other", "reply": "OTHER"})

    async def _run():
        with _all_patches():
            with patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _wrap_deepseek_as_judge_result(_grouped_deepseek)):
                from kouhai_bot.handlers.cmd.submit import handle
                ev1 = _kwargs(_make_event("/submit slow group1", group_id=GID, user_id=UID))
                ev2 = _kwargs(_make_event("/submit fast group2", group_id=GID2, user_id=UID2))
                t1 = asyncio.create_task(handle(**ev1))
                await asyncio.sleep(0)
                t2 = asyncio.create_task(handle(**ev2))
                await asyncio.sleep(0.1)
                release_slow.set()
                await asyncio.gather(t1, t2)

    asyncio.run(_run())

    reply_groups = []
    for item in _sent:
        msg = item.get("message", [])
        text = " ".join(seg.get("data", {}).get("text", "") for seg in msg if seg.get("type") == "text")
        if "SLOW" in text or "FAST" in text:
            reply_groups.append((item["group_id"], text))

    assert len(reply_groups) == 2, f"Expected 2 replies, got: {reply_groups}"
    assert reply_groups[0][0] == GID2 and "FAST" in reply_groups[0][1], \
        f"Expected fast other-group reply first, got: {reply_groups}"
    _cleanup()
    print("✅ submit: different groups do not block each other")


def test_submit_user_group_blocked_within_window():
    """Delayed starred group /submit is recalled and judged in private."""
    _reset_state()
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "private no", "reply": "还不对"}
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()),
    })

    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=300,
                submit_delay_message="将机会多留给年轻人吧～{wait}",
            )
        ],
    }

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit my solution text here"))))

    assert _deleted == ["msg_001"], f"Expected original group submit to be recalled: {_deleted}"
    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "已转到 private judge" in private_text, private_text
    assert "刚才被撤回的提交内容" in private_text, private_text
    assert "my solution text here" in private_text, private_text
    assert _deepseek_calls, "Private judge should run for redirected submit"
    _cleanup()
    print("✅ submit: delayed user group submit redirects to private")


def test_starred_redirect_does_not_mark_first_notice_when_private_intro_fails():
    """If the first private notice cannot be sent, retry should still send intro/card later."""
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()),
    })

    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=300,
                submit_delay_message="将机会多留给年轻人吧～{wait}",
            )
        ],
    }

    async def _fail_private_send(user_id, message):
        return None

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import handle
        from kouhai_bot.private_judge import has_group_problem_private_notified

        with patch("kouhai_bot.handlers.cmd.submit.send_private_msg", _fail_private_send):
            asyncio.run(handle(**_kwargs(_make_event("/submit my solution text here"))))

        notified = has_group_problem_private_notified(UID, PID)

    assert not notified
    assert _deleted == [], f"Submit should not be recalled when private repeat failed: {_deleted}"
    assert not _deepseek_calls, f"Judge should not run when private repeat failed: {_deepseek_calls}"
    assert any("私聊发送失败" in _last_text_item(item) for item in _sent), _sent
    _cleanup()
    print("✅ submit: failed private redirect does not consume first notice")


def test_starred_redirect_does_not_mark_first_notice_when_private_repeat_fails():
    """If repeat DM fails after intro/card, retry should still send intro/card later."""
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()),
    })

    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=300,
                submit_delay_message="将机会多留给年轻人吧～{wait}",
            )
        ],
    }

    async def _fail_repeat_send(user_id, message):
        text = _last_text_item({"message": message})
        if "刚才被撤回的提交内容" in text:
            return None
        return await _mock_send_private(user_id, message)

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import handle
        from kouhai_bot.private_judge import has_group_problem_private_notified

        with patch("kouhai_bot.handlers.cmd.submit.send_private_msg", _fail_repeat_send):
            asyncio.run(handle(**_kwargs(_make_event("/submit my solution text here"))))

        notified = has_group_problem_private_notified(UID, PID)

    assert not notified
    assert _deleted == [], f"Submit should not be recalled when private repeat failed: {_deleted}"
    assert not any(call.get("task") == "judge" for call in _deepseek_calls), (
        f"Judge should not run when private repeat failed: {_deepseek_calls}"
    )
    assert any("私聊复述提交失败" in _last_text_item(item) for item in _sent), _sent
    _cleanup()
    print("✅ submit: failed private repeat does not consume first notice")


def test_submit_user_group_blocked_uses_dynamic_wait():
    """Dynamic wait still redirects starred submits to private judge."""
    _reset_state()
    global _deepseek_response
    _deepseek_response = {"correct": False, "reason": "private no", "reply": "还不对"}
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()) - 400,
    })
    _write_scoreboard(GID, {
        "solves": [],
        "user_submissions": {},
        "user_group_waits": {
            "groups": {
                "starred": {
                    "users": {
                        str(UID): {"wait_sec": 900},
                    },
                },
            },
        },
    })

    lazy = _LazyConfig()
    lazy._config = {
        **_config_dict(),
        "user_groups": [
            UserGroupConfig(
                name="starred",
                display_name="打星",
                user_ids=[UID],
                submit_delay_sec=300,
                submit_delay_message="将机会多留给年轻人吧～{wait}",
            )
        ],
    }

    with _all_patches(), patch("kouhai_bot.config._config", lazy):
        from kouhai_bot.handlers.cmd.submit import handle
        asyncio.run(handle(**_kwargs(_make_event("/submit my solution text here"))))

    assert _deleted == ["msg_001"], f"Expected original group submit to be recalled: {_deleted}"
    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "刚才被撤回的提交内容" in private_text, private_text
    assert _deepseek_calls, "Private judge should run for redirected submit"
    _cleanup()
    print("✅ submit: dynamic wait redirects to private")


def test_parse_problem_ref_accepts_loose_codeforces_links():
    from kouhai_bot.private_judge import parse_problem_ref, problem_id_from_ref

    cases = {
        "CF2234B": "2234B",
        "2234B": "2234B",
        "https://codeforces.com/contest/2233/problem/F": "2233F",
        "codeforces.com/contest/2233/problem/F": "2233F",
        "/contest/2233/problem/F": "2233F",
        "contest/2233/problem/F": "2233F",
        "https://codeforces.com/problemset/problem/2234/B": "2234B",
        "codeforces.com/problemset/problem/2234/B": "2234B",
        "/problemset/problem/2234/B": "2234B",
        "problem/2230/F": "2230F",
        "problem/2230/F?locale=en": "2230F",
    }
    for raw, expected in cases.items():
        parsed = parse_problem_ref(raw)
        assert parsed is not None, raw
        assert problem_id_from_ref(*parsed) == expected, (raw, parsed)

    assert parse_problem_ref("contest/abc/problem/F") is None
    print("✅ private setproblem: accepts loose Codeforces links")


def test_private_setproblem_current_copies_empty_private_history():
    _reset_state()
    _setup_problem_for(GID, PID)
    group_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "clarify",
        "content": "n 是多少？",
        "result": "clarify",
        "reply": "n ≤ 1e5",
        "problem": PID,
    }
    _write_scoreboard(GID, {"solves": [], "user_submissions": {str(UID): [group_record]}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.setproblem import handle
        from kouhai_bot.private_judge import get_private_current_problem, load_private_problem_history

        asyncio.run(handle(**_kwargs(_make_private_event("/setproblem"))))
        current = get_private_current_problem(UID)
        history = load_private_problem_history(UID, PID)

    assert current and current["today"] == PID, current
    assert history and history[0]["content"] == "n 是多少？", history
    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "来自群聊的历史记录" in private_text, private_text
    _cleanup()
    print("✅ private setproblem: copies empty private history")


def test_resolve_problem_by_pid_revalidates_cached_statement():
    _reset_state()
    _write_statement(PID, {
        "name": "stale cache",
        "description": "old statement without VL marker",
    })
    problem = {
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
    }

    with _all_patches(), \
        patch("kouhai_bot.private_judge._cached_problem_by_pid", return_value=problem), \
        patch("kouhai_bot.private_judge._ensure_statement", return_value={"name": "validated"}) as ensure:
        from kouhai_bot.private_judge import resolve_problem_by_pid

        state = resolve_problem_by_pid(PID)

    ensure.assert_called_once_with(problem)
    assert state["today"] == PID, state
    assert state["name"] == "Superhero's Job", state
    _cleanup()
    print("✅ private setproblem: revalidates cached statements")


def test_resolve_random_problem_revalidates_cached_statement():
    _reset_state()
    _write_statement(PID, {
        "name": "stale cache",
        "description": "old statement without VL marker",
    })
    problem = {
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
    }

    with _all_patches(), \
        patch("kouhai_bot.private_judge._fetch_problemset", return_value=[problem]), \
        patch("kouhai_bot.private_judge.random.shuffle", lambda items: None), \
        patch("kouhai_bot.private_judge._ensure_statement", return_value={"name": "validated"}) as ensure:
        from kouhai_bot.private_judge import resolve_random_problem

        state = resolve_random_problem(GID)

    ensure.assert_called_once_with(problem)
    assert state["today"] == PID, state
    _cleanup()
    print("✅ private setproblem random: revalidates cached statements")


def test_private_setproblem_non_formula_image_hint():
    _reset_state()

    with _all_patches():
        from kouhai_bot.handlers.cmd.setproblem import handle
        from kouhai_bot.private_judge import NonFormulaImageProblem

        with patch(
            "kouhai_bot.handlers.cmd.setproblem.resolve_problem_by_pid",
            side_effect=NonFormulaImageProblem("1065C"),
        ):
            asyncio.run(handle(**_kwargs(_make_private_event("/setproblem 1065C"))))

    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "非公式图片" in private_text, private_text
    assert "处理能力有限" in private_text, private_text
    assert "换一道题" in private_text, private_text
    _cleanup()
    print("✅ private setproblem: explains non-formula image limitation")


def test_build_problem_card_payload_revalidates_cached_statement():
    _reset_state()
    _write_statement(PID, {
        "name": "stale cache",
        "description": "old statement without VL marker",
    })
    statement = {
        "name": "validated",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "validated statement",
        "input": "n",
        "samples": [],
        "notes": "",
    }

    with _all_patches(), \
        patch("kouhai_bot.private_judge._ensure_statement", return_value=statement) as ensure:
        from kouhai_bot.private_judge import build_problem_card_payload

        payload = asyncio.run(build_problem_card_payload(GID, {
            "today": PID,
            "contestId": 542,
            "index": "D",
            "name": "Superhero's Job",
            "rating": 2600,
            "tags": [],
        }))

    ensure.assert_called_once()
    assert payload["post_msg"].startswith("private judge 题目"), payload
    _cleanup()
    print("✅ private problem card: revalidates cached statements")


def test_private_problem_card_hides_original_problem_identity():
    _reset_state()
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "time_limit": "2s",
        "memory_limit": "256MB",
        "description": "Statement text",
        "input": "n",
        "output": "answer",
        "samples": [{"input": "1", "output": "1"}],
    })
    _write_group_file(GID, "problem_summaries.json", {
        PID: {"summary_zh": "SUMMARY_ONLY"},
    })

    with _all_patches():
        from kouhai_bot.private_judge import build_problem_card_payload

        payload = asyncio.run(build_problem_card_payload(GID, {
            "today": PID,
            "contestId": 542,
            "index": "D",
            "name": "Superhero's Job",
            "rating": 2600,
            "tags": [],
        }))

    post_msg = payload["post_msg"]
    assert post_msg.startswith("private judge 题目"), post_msg
    assert "SUMMARY_ONLY" in post_msg, post_msg
    assert PID not in post_msg, post_msg
    assert "CF" not in post_msg, post_msg
    assert "Superhero" not in post_msg, post_msg
    assert "2600" not in post_msg and "rating" not in post_msg, post_msg
    _cleanup()
    print("✅ private problem card: hides original identity")


def test_private_problem_card_high_difficulty_warns_after_card():
    _reset_state()
    _write_group_file(GID, "daily_msg.json", {
        "msg_id": 1111,
        "pid": PID,
    })
    problem = {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2901,
        "tags": [],
    }

    with _all_patches(), patch("kouhai_bot.private_judge.asyncio.sleep", AsyncMock()):
        from kouhai_bot.private_judge import send_problem_card_private

        ok = asyncio.run(send_problem_card_private(UID, GID, problem, prefer_group_card=True))

    assert ok
    assert len(_private_forwarded) == 1, f"Expected private problem card, got: {_private_forwarded}"
    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "难度较高" in private_text, private_text
    assert "题解" in private_text, private_text
    _cleanup()
    print("✅ private problem card: high difficulty warns after card")


def test_private_problem_card_falls_back_when_main_self_send_fails():
    _reset_state()
    problem = {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": [],
    }
    payload = {
        "post_msg": "MAIN CARD",
        "sample_messages": ["SAMPLE CARD"],
        "notes_message": "NOTE CARD",
    }

    async def _send_private_with_main_self_send_failure(user_id, message):
        text = _last_text_item({"message": message})
        if user_id == 1 and text == "MAIN CARD":
            return None
        return await _mock_send_private(user_id, message)

    with _all_patches(), \
        patch("kouhai_bot.private_judge.asyncio.sleep", AsyncMock()), \
        patch("kouhai_bot.private_judge.build_problem_card_payload", AsyncMock(return_value=payload)), \
        patch("kouhai_bot.private_judge.send_private_msg", _send_private_with_main_self_send_failure):
        from kouhai_bot.private_judge import send_problem_card_private

        ok = asyncio.run(send_problem_card_private(UID, GID, problem, prefer_group_card=False))

    assert ok
    assert _private_forwarded == [], f"Should not forward without main card node: {_private_forwarded}"
    private_text = "\n".join(_last_text_item(item) for item in _private_sent)
    assert "MAIN CARD" in private_text, private_text
    assert "SAMPLE CARD" in private_text, private_text
    assert "NOTE CARD" in private_text, private_text
    _cleanup()
    print("✅ private problem card: falls back when main node self-send fails")


def test_private_state_load_logs_corrupt_json_once(caplog):
    _reset_state()
    state_dir = os.path.join(_data_dir(), "private_judge", "users")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, f"{UID}.json"), "w") as f:
        f.write("{bad json")

    with _all_patches():
        from kouhai_bot.private_judge import _PRIVATE_STATE_LOAD_WARNED, load_private_state

        _PRIVATE_STATE_LOAD_WARNED.clear()
        caplog.set_level("WARNING", logger="kouhai-bot.private_judge")
        state = load_private_state(UID)
        state_again = load_private_state(UID)

    assert state["user_submissions"] == []
    assert state_again["user_submissions"] == []
    messages = [record.getMessage() for record in caplog.records]
    assert sum("failed to load private judge state" in message for message in messages) == 1
    _cleanup()
    print("✅ private state: corrupt JSON logs once")


def test_private_state_save_writes_complete_json_without_temp_leftovers():
    _reset_state()
    with _all_patches():
        from kouhai_bot.private_judge import load_private_state, save_private_state

        save_private_state(UID, {
            "current_problem": {"today": PID},
            "user_submissions": [{"problem": PID, "content": "x"}],
            "solved_problems": {},
            "last_solved_problem": "",
            "notified_group_problem_private": {},
            "problem_cards": {},
        })
        loaded = load_private_state(UID)

    assert loaded["current_problem"]["today"] == PID
    assert loaded["user_submissions"][0]["content"] == "x"
    state_dir = os.path.join(_data_dir(), "private_judge", "users")
    leftovers = [name for name in os.listdir(state_dir) if name.endswith(".tmp")]
    assert leftovers == []
    _cleanup()
    print("✅ private state: atomic save writes complete JSON")


def test_private_review_allowed_after_group_solves_current_problem():
    _reset_state()
    global _deepseek_response
    _deepseek_response = "可以复盘这题。"
    _setup_problem_for(GID, PID)
    _write_scoreboard(GID, {
        "solves": [{"user_id": OTHER_UID, "nickname": "Bob", "problem": PID, "timestamp": time.time()}],
        "user_submissions": {},
    })

    with _all_patches():
        from kouhai_bot.private_judge import set_private_current_problem
        from kouhai_bot.handlers.cmd.review import handle

        set_private_current_problem(UID, {
            "today": PID,
            "contestId": 542,
            "index": "D",
            "name": "Superhero's Job",
            "rating": 2600,
            "tags": [],
        })
        asyncio.run(handle(**_kwargs(_make_private_event("/review 为什么这样做？"))))

    review_calls = [call for call in _deepseek_calls if call.get("task") == "review"]
    assert review_calls, _deepseek_calls
    private_text = "\n".join(
        " ".join(seg.get("data", {}).get("text", "") for seg in item["message"] if seg.get("type") == "text")
        for item in _private_sent
    )
    assert "可以复盘" in private_text, private_text
    _cleanup()
    print("✅ private review: group solve unlocks current problem")


def test_sync_aborts_when_source_has_no_history_without_clearing_target():
    _reset_state()
    _setup_problem_for(GID, PID)
    target_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "clarify",
        "content": "group keep",
        "result": "clarify",
        "reply": "keep",
        "problem": PID,
    }
    _write_scoreboard(GID, {"solves": [], "user_submissions": {str(UID): [target_record]}})

    with _all_patches():
        from kouhai_bot.handlers.cmd.sync import handle
        asyncio.run(handle(**_kwargs(_make_event("/sync"))))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["user_submissions"][str(UID)][0]["content"] == "group keep"
    assert "已取消" in _last_text(), _last_text()
    _cleanup()
    print("✅ sync: empty source aborts without clearing target")


def test_sync_private_correct_current_problem_scores_for_normal_user():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    private_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "submit",
        "content": "private ac",
        "result": "correct",
        "reason": "ok",
        "reply": "",
        "problem": PID,
    }

    with _all_patches(), patch(
        "kouhai_bot.handlers.cmd.sync._reveal_problem_source",
        AsyncMock(return_value="本题来自 CF542D✨"),
    ):
        from kouhai_bot.private_judge import save_private_submission
        from kouhai_bot.handlers.cmd.sync import handle
        from kouhai_bot.eventlog import EVENT_META_KEY, load_events, log_command_received

        save_private_submission(UID, private_record)
        event = _make_event("/sync")
        event[EVENT_META_KEY] = log_command_received(
            group_id=GID,
            user_id=UID,
            sender=event["sender"],
            command="sync",
            message_id=event["message_id"],
            raw_text=event["raw_message"],
        )
        asyncio.run(handle(**_kwargs(event)))
        logged_events = load_events(GID, event[EVENT_META_KEY]["date"])

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["solves"] and str(saved["solves"][0]["user_id"]) == str(UID), saved
    assert saved["user_submissions"][str(UID)][0]["content"] == "private ac"
    text = "\n".join(_last_text_item(item) for item in _sent)
    assert "本题 +" in text, _sent
    assert "本题来自 CF542D" in text, text
    assert "已同步完成" not in text, text
    assert ("msg_001", "128076") in _reacted, _reacted
    finished = [item for item in logged_events if item.get("type") == "finished"]
    assert finished and finished[-1]["status"] == "correct", finished
    assert finished[-1]["synced_submit_count"] == 1, finished[-1]
    assert finished[-1]["synced_correct_count"] == 1, finished[-1]
    _cleanup()
    print("✅ sync: private AC scores current group problem for normal user")


def test_starred_sync_within_cd_only_syncs_clarify_and_rejects_empty_source():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()),
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    private_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "submit",
        "content": "private ac in cd",
        "result": "correct",
        "reason": "ok",
        "reply": "",
        "problem": PID,
    }

    with _all_patches(), patch("kouhai_bot.config._config", _starred_config_for_user()):
        from kouhai_bot.private_judge import save_private_submission
        from kouhai_bot.handlers.cmd.sync import handle

        save_private_submission(UID, private_record)
        asyncio.run(handle(**_kwargs(_make_event("/sync"))))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved.get("solves", []) == [], saved
    assert saved.get("user_submissions", {}) == {}, saved
    text = "\n".join(_last_text_item(item) for item in _sent)
    assert "打星用户" in text and "CD 内" in text and "仅能同步 clarify" in text, text
    assert "没有可同步的 clarify" in text, text
    assert not _reacted, _reacted
    _cleanup()
    print("✅ sync: starred user in CD cannot sync private submit")


def test_starred_sync_after_cd_scores_like_normal_user():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()) - 301,
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    private_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "submit",
        "content": "private ac after cd",
        "result": "correct",
        "reason": "ok",
        "reply": "",
        "problem": PID,
    }

    with _all_patches(), patch("kouhai_bot.config._config", _starred_config_for_user()), patch(
        "kouhai_bot.handlers.cmd.sync._reveal_problem_source",
        AsyncMock(return_value=""),
    ):
        from kouhai_bot.private_judge import save_private_submission
        from kouhai_bot.handlers.cmd.sync import handle

        save_private_submission(UID, private_record)
        asyncio.run(handle(**_kwargs(_make_event("/sync"))))

    with open(os.path.join(_data_dir(), "groups", str(GID), "scoreboard.json")) as f:
        saved = json.load(f)
    assert saved["solves"] and str(saved["solves"][0]["user_id"]) == str(UID), saved
    assert saved["user_submissions"][str(UID)][0]["content"] == "private ac after cd"
    text = "\n".join(_last_text_item(item) for item in _sent)
    assert "本题 +" in text, text
    assert "CD 内" not in text and "仅同步 clarify" not in text, text
    assert ("msg_001", "128076") in _reacted, _reacted
    _cleanup()
    print("✅ sync: starred user after CD scores like normal user")


def test_private_sync_for_starred_user_within_cd_only_syncs_clarify():
    _reset_state()
    _setup_problem_for(GID, PID)
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory", "dp"],
        "date": "2026-05-14",
        "posted_at": int(time.time()),
    })
    group_clarify = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "clarify",
        "content": "group clarify",
        "result": "clarify",
        "reply": "group reply",
        "problem": PID,
    }
    group_submit = {
        "timestamp": "2026-05-14T12:01:00+08:00",
        "type": "submit",
        "content": "group ac should not sync during cd",
        "result": "correct",
        "reason": "ok",
        "reply": "",
        "problem": PID,
    }
    _write_scoreboard(GID, {"solves": [], "user_submissions": {str(UID): [group_clarify, group_submit]}})

    with _all_patches(), patch("kouhai_bot.config._config", _starred_config_for_user()):
        from kouhai_bot.private_judge import load_private_state, set_private_current_problem
        from kouhai_bot.handlers.cmd.sync import handle

        set_private_current_problem(UID, {
            "today": PID,
            "contestId": 542,
            "index": "D",
            "name": "Superhero's Job",
            "rating": 2600,
            "tags": [],
        })
        asyncio.run(handle(**_kwargs(_make_private_event("/sync"))))
        private_state = load_private_state(UID)

    records = [item for item in private_state["user_submissions"] if item.get("problem") == PID]
    assert len(records) == 1 and records[0]["type"] == "clarify", records
    assert records[0]["content"] == "group clarify", records
    private_text = "\n".join(_last_text_item(item) for item in _private_sent)
    assert "打星用户" in private_text and "CD 内" in private_text and "仅同步 clarify" in private_text, private_text
    assert "group ac should not sync" not in private_text, private_text
    _cleanup()
    print("✅ private sync: starred user in CD only syncs clarify")


def test_sync_history_card_uses_friendly_visible_format():
    _reset_state()
    _setup_problem_for(GID, PID)
    _group_members[GID][0]["card"] = "AliceCard"
    private_records = [
        {
            "timestamp": "2026-05-14T12:00:00+08:00",
            "type": "submit",
            "content": "first line\nsecond line",
            "result": "incorrect",
            "reason": "secret reason",
            "reply": "bot reply\nvisible",
            "problem": PID,
        },
        {
            "timestamp": "2026-05-14T12:01:00+08:00",
            "type": "clarify",
            "content": "what is n?",
            "result": "clarify",
            "reason": "hidden clarify reason",
            "reply": "n is input",
            "problem": PID,
        },
    ]

    with _all_patches():
        from kouhai_bot.private_judge import save_private_submission
        from kouhai_bot.handlers.cmd.sync import handle

        for record in private_records:
            save_private_submission(UID, record)
        asyncio.run(handle(**_kwargs(_make_event("/sync"))))

    history_text = "\n".join(
        _last_text_item(item)
        for item in _private_sent
        if item["user_id"] == 1
    )
    assert "AliceCard在当前的历史记录如下：" in history_text, history_text
    assert "👤：first line second line\n🤖：bot reply visible" in history_text, history_text
    assert "👤：what is n?\n🤖：n is input" in history_text, history_text
    assert "secret reason" not in history_text and "hidden clarify reason" not in history_text
    assert "submit" not in history_text and "incorrect" not in history_text
    assert _forwarded, "Expected history to be forwarded to group"
    assert not any("已同步完成" in _last_text_item(item) for item in _sent), _sent
    assert ("msg_001", "128076") in _reacted, _reacted
    _cleanup()
    print("✅ sync: history card uses friendly visible format")


def test_sync_history_card_chunks_long_history():
    _reset_state()
    _setup_problem_for(GID, PID)
    long_text = "x" * 6500

    with _all_patches():
        from kouhai_bot.private_judge import save_private_submission
        from kouhai_bot.handlers.cmd.sync import handle

        save_private_submission(UID, {
            "timestamp": "2026-05-14T12:00:00+08:00",
            "type": "submit",
            "content": long_text,
            "result": "incorrect",
            "reason": "hidden",
            "reply": "try again",
            "problem": PID,
        })
        asyncio.run(handle(**_kwargs(_make_event("/sync"))))

    assert _forwarded, "Expected history forward card"
    assert len(_forwarded[0]["messages"]) >= 3, _forwarded
    chunk_text = "".join(
        _last_text_item(item)
        for item in _private_sent
        if item["user_id"] == 1
    )
    assert long_text in chunk_text, "Long history should not be truncated"
    _cleanup()
    print("✅ sync: long history card is chunked")


def test_private_sync_from_solved_group_marks_private_review_state():
    _reset_state()
    _setup_problem_for(GID, PID)
    group_record = {
        "timestamp": "2026-05-14T12:00:00+08:00",
        "type": "clarify",
        "content": "group clarify",
        "result": "clarify",
        "reply": "group reply",
        "problem": PID,
    }
    _write_scoreboard(GID, {
        "solves": [{"user_id": OTHER_UID, "nickname": "Bob", "problem": PID, "timestamp": time.time()}],
        "user_submissions": {str(UID): [group_record]},
    })

    with _all_patches():
        from kouhai_bot.private_judge import get_private_review_pid, set_private_current_problem
        from kouhai_bot.handlers.cmd.sync import handle

        set_private_current_problem(UID, {
            "today": PID,
            "contestId": 542,
            "index": "D",
            "name": "Superhero's Job",
            "rating": 2600,
            "tags": [],
        })
        asyncio.run(handle(**_kwargs(_make_private_event("/sync"))))
        set_private_current_problem(UID, {
            "today": PID2,
            "contestId": 100,
            "index": "A",
            "name": "Other Problem",
            "rating": 2000,
            "tags": [],
        })
        review_pid = get_private_review_pid(UID, GID)

    assert review_pid == PID
    private_text = "\n".join(_last_text_item(item) for item in _private_sent)
    assert "已同步完成" not in private_text, private_text
    _cleanup()
    print("✅ sync: solved group state is persisted for private review")


def _last_text_item(item: dict) -> str:
    msg = item.get("message", [])
    if isinstance(msg, list):
        return " ".join(
            seg.get("data", {}).get("text", "")
            for seg in msg if isinstance(seg, dict) and seg.get("type") == "text"
        )
    return str(msg)


if __name__ == "__main__":
    test_submit_correct()
    test_submit_incorrect()
    test_submit_llm_failure_shows_admin_message()
    test_submit_timeout_is_saved_as_context()
    test_submit_already_solved()
    test_review_uses_latest_solved_problem()
    test_review_alias_dispatches_to_review_handler()
    test_review_requires_previous_solve()
    test_review_uses_referenced_history_card_even_if_unsolved()
    test_review_rejects_current_problem_card()
    test_review_allows_current_problem_card_after_solve()
    test_review_unknown_referenced_card_is_friendly()
    test_review_long_reply_is_chunked_into_one_forward_card()
    test_review_parallel_same_group()
    test_review_same_user_runs_in_parallel_with_pending_archive_context()
    test_review_after_pending_submit_uses_snapshotted_solved_problem()
    test_submit_off_topic()
    test_submit_operation_not_blocked()
    test_private_submit_off_topic_sends_face_123()
    test_private_submit_sends_face_ack_instead_of_text_or_reaction()
    test_private_clear_invalid_usage_sends_text_ack_instead_of_face()
    test_submit_no_problem()
    test_clarify_with_problem()
    test_private_submit_correct_message_includes_problem_id()
    test_clarify_no_problem()
    test_clarify_alias_dispatches_to_clarify_handler()
    test_problem_with_data()
    test_problem_rebuilds_forward_card_when_node_ids_are_stale()
    test_problem_ignores_stale_daily_msg_pid()
    test_problem_solved_resend_shows_next_problem_hint()
    test_problem_unsolved_resend_does_not_show_next_problem_hint()
    test_problem_high_difficulty_resend_warns_after_card()
    test_problem_alias_dispatches_to_problem_handler()
    test_tag_with_tags()
    test_scoreboard_with_data()
    test_scoreboard_splits_default_and_starred_groups()
    test_help_shows_short_aliases_and_configured_newproblem_cooldown()
    test_private_help_only_shows_private_judge_commands()
    test_private_testcd_allows_submit_when_no_cooldown_and_dispatches()
    test_private_testcd_shows_remaining_for_starred_user()
    test_private_testcd_formats_multi_unit_remaining()
    test_private_testcd_allows_after_cooldown_expires()
    test_newproblem_cooldown()
    test_newproblem_busy_rejects_concurrent_force()
    test_newproblem_unsolved_rejects()
    test_newproblem_force_posts_when_unsolved()
    test_newproblem_alias_force_posts_when_unsolved()
    test_newproblem_force_requires_space()
    test_newproblem_solved_posts()
    test_newproblem_fallback_direct_saves_daily_msg_for_current_pid()
    test_do_daily_post_does_not_switch_state_when_send_fails()
    test_status_ignores_other_groups_and_reports_idle()
    test_status_reports_newproblem_busy()
    test_submit_parallel_replies_follow_completion_order()
    test_submit_parallel_late_wrong_results_are_reused_after_first_solve()
    test_submit_parallel_late_correct_result_does_not_update_scoreboard_twice()
    test_submit_same_user_includes_previous_submit_history()
    test_submit_same_user_later_submit_drops_unanswered_previous_submit()
    test_clear_drops_unanswered_same_user_submit()
    test_dropping_unanswered_submit_unblocks_score_resolution()
    test_review_history_formats_types_and_submit_numbers()
    test_user_submission_history_is_unbounded_and_upserts_by_request_id()
    test_clarify_prompt_hides_original_problem_identity()
    test_private_problem_card_high_difficulty_warns_after_card()
    test_submit_same_user_includes_previous_clarify_history()
    test_submit_parallel_different_groups_do_not_block()
    test_submit_starred_user_shows_own_group_top5()
    test_submit_alias_dispatches_to_submit_handler()
    test_submit_user_group_blocked_within_window()
    test_sync_aborts_when_source_has_no_history_without_clearing_target()
    test_sync_private_correct_current_problem_scores_for_normal_user()
    test_starred_sync_within_cd_only_syncs_clarify_and_rejects_empty_source()
    test_starred_sync_after_cd_scores_like_normal_user()
    test_private_sync_for_starred_user_within_cd_only_syncs_clarify()
    test_sync_history_card_uses_friendly_visible_format()
    test_sync_history_card_chunks_long_history()
    test_private_sync_from_solved_group_marks_private_review_state()
    test_parse_problem_ref_accepts_loose_codeforces_links()
    print(f"\n🎉 E2E tests passed")
