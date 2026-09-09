"""Unit tests for targeted history-record removal (offtopic cleanup).

An offtopic interaction is treated as if the conversation never happened:
the enqueue-time pending record is dropped by request_id, nothing else.
"""

import sys, os
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kouhai_bot.handlers.shared import (
    load_scoreboard,
    remove_user_submission,
    save_user_submission,
)
from kouhai_bot.private_judge import (
    load_private_submissions,
    remove_private_submission,
    save_private_submission,
)


def test_remove_user_submission_targets_only_request_id(tmp_path):
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    with patch("kouhai_bot.handlers.shared.get_config", return_value=cfg):
        save_user_submission(1, 2, {"request_id": "a", "result": "pending", "problem": "542D"})
        save_user_submission(1, 2, {"request_id": "b", "result": "correct", "problem": "542D"})
        remove_user_submission(1, 2, "a")
        remove_user_submission(1, 2, "")         # no-op: empty id
        remove_user_submission(1, 2, "missing")  # no-op: unknown id
        records = load_scoreboard(1)["user_submissions"]["2"]
    assert [item["request_id"] for item in records] == ["b"]


def test_remove_private_submission_targets_only_request_id(tmp_path):
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    with patch("kouhai_bot.private_judge.get_config", return_value=cfg):
        save_private_submission(7, {"request_id": "a", "result": "pending"})
        save_private_submission(7, {"request_id": "b", "result": "review"})
        remove_private_submission(7, "a")
        remove_private_submission(7, "missing")  # no-op: unknown id
        records = load_private_submissions(7)
    assert [item["request_id"] for item in records] == ["b"]
