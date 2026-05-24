import asyncio
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

GID = 123456
UID = 42
PID = "542D"

_temp_dir = None
_sent = []
_reacted = []
_deepseek_response = None
_group_members = {}


def _reset_state():
    global _temp_dir, _sent, _reacted, _deepseek_response, _group_members
    _sent = []
    _reacted = []
    _deepseek_response = None
    _group_members = {
        GID: [{"user_id": UID, "nickname": "Alice", "card": ""}],
    }
    _temp_dir = tempfile.mkdtemp(prefix="xcpc_annotations_")
    data_dir = os.path.join(_temp_dir, "data")
    os.makedirs(os.path.join(data_dir, "groups", str(GID)), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "statements"), exist_ok=True)


def _cleanup():
    global _temp_dir
    if _temp_dir and os.path.exists(_temp_dir):
        shutil.rmtree(_temp_dir)
    _temp_dir = None
    for name in [
        "kouhai_bot.annotations",
        "kouhai_bot.annotations.exporter",
        "kouhai_bot.annotations.store",
        "kouhai_bot.handlers.cmd.submit",
        "kouhai_bot.handlers.cmd.review",
        "kouhai_bot.handlers.cmd.clarify",
    ]:
        sys.modules.pop(name, None)


def _data_dir() -> str:
    return os.path.join(_temp_dir, "data")


def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_statement(pid: str, data: dict):
    _write_json(os.path.join(_data_dir(), "statements", f"{pid}.json"), data)


def _write_state(group_id: int, data: dict):
    _write_json(os.path.join(_data_dir(), "groups", str(group_id), "state.json"), data)


def _write_scoreboard(group_id: int, data: dict):
    _write_json(os.path.join(_data_dir(), "groups", str(group_id), "scoreboard.json"), data)


class _LazyConfig:
    _config = {
        "bot_qq": 1234567890,
        "napcat_ws_port": 8095,
        "napcat_http_host": "127.0.0.1",
        "napcat_http_port": 3000,
        "deepseek_api_key": "sk-test",
        "deepseek_base_url": "https://api.deepseek.com",
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
        "qwen_api_key": "",
        "qwen_model": "qwen-vl-max",
        "current_group": GID,
        "min_rating": 2000,
        "max_rating": 3000,
        "newproblem_cooldown": 300,
        "submit_ac_backdoor": "",
        "max_context_per_session": 100,
    }

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


async def _mock_send_group(group_id, message):
    _sent.append({"group_id": group_id, "message": message})
    return True


async def _mock_react(message_id, emoji_id):
    _reacted.append((message_id, emoji_id))


async def _mock_deepseek(messages, model="", task="", temperature=0.7, timeout=120,
                         response_format=None, thinking=None):
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
    from kouhai_bot.llm import ChatCompletionResult
    if result is None:
        return ChatCompletionResult(text=None, failure_kind="service_unavailable")
    return ChatCompletionResult(text=result, failure_kind=None)


async def _mock_judge_result(problem_text, submission, history=None):
    return await _mock_chat_completion_result(
        [{}, {"content": submission}],
        task="judge",
        timeout=1200,
        response_format={"type": "json_object"},
        thinking={"type": "enabled"},
    )


async def _mock_send_private(user_id, message):
    return 1000


async def _mock_send_group_forward(group_id, messages):
    return 2000


async def _mock_http_post(action, data):
    if action == "get_group_member_list":
        return {"status": "ok", "data": list(_group_members.get(int(data["group_id"]), []))}
    return {"status": "failed", "data": {}}


def _all_patches():
    stack = ExitStack()
    stack.enter_context(patch("kouhai_bot.config._config", _LazyConfig()))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.napcat.client.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.napcat.client.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.napcat.client._http_post", _mock_http_post))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_group_msg", _mock_send_group))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.react_emoji", _mock_react))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_private_msg", _mock_send_private))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.send_group_forward_msg", _mock_send_group_forward))
    stack.enter_context(patch("kouhai_bot.handlers.shared.call_chat_completion_result", _mock_chat_completion_result))
    stack.enter_context(patch("kouhai_bot.handlers.shared.judge_submission_result", _mock_judge_result))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.call_chat_completion_result", _mock_chat_completion_result))
    stack.enter_context(patch("kouhai_bot.handlers.cmd.submit.judge_submission_result", _mock_judge_result))
    return stack


def test_collect_problem_annotation_bundle_includes_history():
    _reset_state()
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "description": "Test statement",
        "input": "A",
    })
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-14",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(UID): [
                {
                    "timestamp": "2026-05-14T12:00:00+08:00",
                    "content": "first wrong",
                    "result": "incorrect",
                    "reason": "wrong",
                    "reply": "try again",
                    "problem": PID,
                },
                {
                    "timestamp": "2026-05-14T12:03:00+08:00",
                    "content": "what does input mean",
                    "result": "clarify",
                    "reason": "",
                    "reply": "input is A",
                    "problem": PID,
                },
                {
                    "timestamp": "2026-05-14T12:05:00+08:00",
                    "content": "second correct",
                    "result": "correct",
                    "reason": "good",
                    "reply": "",
                    "problem": PID,
                },
            ],
            "99": [{
                "timestamp": "2026-05-14T12:04:00+08:00",
                "content": "other wrong",
                "result": "incorrect",
                "reason": "bad",
                "reply": "no",
                "problem": PID,
            }],
        },
    })

    with _all_patches():
        from kouhai_bot.annotations.exporter import collect_problem_annotation_bundle
        bundle = collect_problem_annotation_bundle(GID, PID, source="test")

    assert bundle is not None
    assert bundle["problem_id"] == PID
    assert len(bundle["rounds"]) == 3
    alice_correct = next(item for item in bundle["rounds"] if item["model_verdict"] == "correct")
    assert len(alice_correct["history_before"]) == 2
    assert alice_correct["history_before"][1]["result"] == "clarify"
    _cleanup()


def test_sync_annotation_bundles_is_idempotent():
    _reset_state()
    _write_statement(PID, {"name": "D. Superhero's Job", "description": "Test statement"})
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-14",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(UID): [{
                "timestamp": "2026-05-14T12:00:00+08:00",
                "content": "accepted",
                "result": "correct",
                "reason": "ok",
                "reply": "",
                "problem": PID,
            }],
        },
    })

    with _all_patches():
        from kouhai_bot.annotations.exporter import sync_annotation_bundles
        from kouhai_bot.annotations.store import list_bundle_summaries
        first = sync_annotation_bundles(group_id=GID)
        second = sync_annotation_bundles(group_id=GID)
        summaries = list_bundle_summaries(status="pending", group_id=GID)

    assert len(first) == 1
    assert len(second) == 0
    assert len(summaries) == 1
    assert summaries[0]["problem_id"] == PID
    _cleanup()


def test_collect_bundle_prefers_saved_group_summary():
    _reset_state()
    _write_statement(PID, {"name": "D. Superhero's Job", "description": "Test statement"})
    _write_scoreboard(GID, {
        "solves": [{
            "user_id": UID,
            "nickname": "Alice",
            "date": "2026-05-14",
            "problem": PID,
            "order": 1,
        }],
        "user_submissions": {
            str(UID): [{
                "timestamp": "2026-05-14T12:00:00+08:00",
                "content": "accepted",
                "result": "correct",
                "reason": "ok",
                "reply": "",
                "problem": PID,
            }],
        },
    })

    with _all_patches():
        from kouhai_bot.annotations.exporter import collect_problem_annotation_bundle
        from kouhai_bot.handlers.shared import save_problem_summary
        save_problem_summary(GID, PID, "这是群里发题时生成的中文题意。")
        bundle = collect_problem_annotation_bundle(GID, PID, source="test")

    assert bundle is not None
    assert bundle["statement"]["summary_zh"] == "这是群里发题时生成的中文题意。"
    _cleanup()


def test_submit_correct_exports_pending_annotation_bundle():
    _reset_state()
    _write_state(GID, {
        "today": PID,
        "contestId": 542,
        "index": "D",
        "name": "Superhero's Job",
        "rating": 2600,
        "tags": ["number theory"],
        "date": "2026-05-14",
    })
    _write_statement(PID, {
        "name": "D. Superhero's Job",
        "description": "Statement",
        "input": "A",
    })
    _write_scoreboard(GID, {"solves": [], "user_submissions": {}})
    global _deepseek_response
    _deepseek_response = {"correct": True, "reason": "Correct", "reply": ""}

    event = {
        "type": "message",
        "message_type": "group",
        "group_id": GID,
        "user_id": UID,
        "sender": {"nickname": "Alice", "card": "", "user_id": UID},
        "message_id": "msg_001",
        "raw_message": "/submit valid idea",
        "message": [{"type": "text", "data": {"text": "/submit valid idea"}}],
    }

    with _all_patches():
        from kouhai_bot.handlers.cmd.submit import handle
        with patch("kouhai_bot.handlers.cmd.submit._reveal_problem_source", AsyncMock(return_value="")):
            asyncio.run(handle(
                group_id=GID,
                user_id=UID,
                sender=event["sender"],
                message_id=event["message_id"],
                raw_text=event["raw_message"],
                segments=event["message"],
                event=event,
            ))

    pending_path = os.path.join(_data_dir(), "annotations", "pending", str(GID), f"{PID}.json")
    assert os.path.exists(pending_path), f"Missing pending annotation bundle: {pending_path}"
    with open(pending_path, encoding="utf-8") as f:
        bundle = json.load(f)
    assert bundle["problem_id"] == PID
    assert len(bundle["rounds"]) == 1
    assert bundle["rounds"][0]["model_verdict"] == "correct"
    _cleanup()
