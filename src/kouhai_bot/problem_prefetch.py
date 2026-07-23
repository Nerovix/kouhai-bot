"""Durable single-slot prefetch coordinator for the next group problem."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .config import get_config
from .editorial_followup import ensure_editorial_prefetch
from .handlers.shared import get_today_problem, load_scoreboard, statement_images
from .problem_preparation import (
    PreparedProblem,
    effective_rating_range,
    load_statement_json,
    prepare_problem,
)

logger = logging.getLogger("kouhai-bot.problem_prefetch")

SLOT_FORMAT_VERSION = 1
DEFAULT_RETRY_INTERVAL_SEC = 60.0


@dataclass(frozen=True)
class PrefetchedProblem:
    """A durable READY slot claimed by one /newproblem publication."""

    slot_id: str
    problem: PreparedProblem

    def to_json(self) -> dict:
        return {
            "format_version": SLOT_FORMAT_VERSION,
            "slot_id": self.slot_id,
            "problem": self.problem.to_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> PrefetchedProblem | None:
        if not isinstance(value, dict):
            return None
        if value.get("format_version") != SLOT_FORMAT_VERSION:
            return None
        slot_id = str(value.get("slot_id", "") or "")
        problem = PreparedProblem.from_json(value.get("problem"))
        if not slot_id or problem is None:
            return None
        return cls(slot_id=slot_id, problem=problem)


class NextProblemPrefetcher:
    """Maintain exactly one READY problem with single-flight preparation.

    The post lock remains owned by the command handler.  This coordinator owns
    only preparation and the READY/CLAIMED handoff, so slow work never runs
    while its internal lock is held.
    """

    def __init__(
        self,
        group_id: int,
        *,
        prepare: Callable[[int], Awaitable[PreparedProblem]] = prepare_problem,
        retry_interval_sec: float = DEFAULT_RETRY_INTERVAL_SEC,
    ) -> None:
        self.group_id = int(group_id)
        self._prepare = prepare
        self._retry_interval_sec = float(retry_interval_sec)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._build_task: asyncio.Task[PrefetchedProblem] | None = None
        self._ready_slot: PrefetchedProblem | None = None
        self._claimed_slot_id: str | None = None

    @property
    def slot_path(self) -> Path:
        return (
            Path(get_config().data_dir)
            / "groups"
            / str(self.group_id)
            / "next_problem.json"
        )

    def _write_slot_locked(self, slot: PrefetchedProblem) -> None:
        path = self.slot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{slot.slot_id}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(slot.to_json(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            with suppress(FileNotFoundError):
                tmp_path.unlink()

    def _remove_slot_locked(self) -> None:
        self._ready_slot = None
        try:
            self.slot_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Keep the in-memory state authoritative for availability.  A stale
            # disk slot is revalidated on the next process start.
            logger.warning(
                "[group_%s] failed to remove next-problem slot: %s",
                self.group_id,
                exc,
            )

    def _slot_valid_locked(self, slot: PrefetchedProblem) -> tuple[bool, str]:
        prepared = slot.problem
        current_min, current_max = effective_rating_range(self.group_id)
        if (prepared.min_rating, prepared.max_rating) != (current_min, current_max):
            return False, "rating_range_changed"

        pid = prepared.pid
        current = get_today_problem(self.group_id)
        current_pid = (
            str(current.get("today", "") or "")
            if isinstance(current, dict)
            else ""
        )
        if pid == current_pid:
            return False, "already_current"

        scoreboard = load_scoreboard(self.group_id)
        solves = scoreboard.get("solves", []) if isinstance(scoreboard, dict) else []
        if any(
            isinstance(item, dict)
            and str(item.get("problem", "") or "") == pid
            for item in solves
        ):
            return False, "already_solved"

        statement = load_statement_json(self.group_id, pid)
        if not statement:
            return False, "statement_missing"
        if statement_images(statement) and not get_config().llm_multimodal_providers:
            return False, "multimodal_unavailable"
        return True, ""

    def _load_slot_locked(self) -> PrefetchedProblem | None:
        if self._ready_slot is not None:
            valid, reason = self._slot_valid_locked(self._ready_slot)
            if valid:
                return self._ready_slot
            logger.info(
                "[group_%s] next-problem slot %s invalidated: %s",
                self.group_id,
                self._ready_slot.problem.pid,
                reason,
            )
            self._remove_slot_locked()

        path = self.slot_path
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[group_%s] next-problem slot unreadable, rebuilding: %s",
                self.group_id,
                exc,
            )
            self._remove_slot_locked()
            return None

        slot = PrefetchedProblem.from_json(raw)
        if slot is None:
            logger.info(
                "[group_%s] next-problem slot format invalid, rebuilding",
                self.group_id,
            )
            self._remove_slot_locked()
            return None

        valid, reason = self._slot_valid_locked(slot)
        if not valid:
            logger.info(
                "[group_%s] next-problem slot %s invalidated: %s",
                self.group_id,
                slot.problem.pid,
                reason,
            )
            self._remove_slot_locked()
            return None
        self._ready_slot = slot
        return slot

    async def _build_and_store(self) -> PrefetchedProblem:
        started = asyncio.get_running_loop().time()
        prepared = await self._prepare(self.group_id)
        slot = PrefetchedProblem(slot_id=uuid.uuid4().hex, problem=prepared)
        async with self._lock:
            existing = self._load_slot_locked()
            if existing is not None:
                return existing
            if self._claimed_slot_id is not None:
                raise RuntimeError("cannot store next problem while another slot is claimed")
            self._ready_slot = slot
            try:
                self._write_slot_locked(slot)
            except OSError as exc:
                # A foreground /newproblem can still use the in-memory slot.
                # The maintenance loop will rebuild after a process restart.
                logger.warning(
                    "[group_%s] failed to persist next problem %s: %s",
                    self.group_id,
                    prepared.pid,
                    exc,
                )
        logger.info(
            "[group_%s] next problem READY pid=%s elapsed=%.1fs",
            self.group_id,
            prepared.pid,
            asyncio.get_running_loop().time() - started,
        )
        return slot

    def _ensure_build_task_locked(self) -> asyncio.Task[PrefetchedProblem]:
        task = self._build_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._build_and_store(),
                name=f"next_problem_build_{self.group_id}",
            )
            # A foreground claim can be cancelled while the shielded build
            # continues.  Observe its terminal exception even if no waiter is
            # left; existing awaiters still receive the same exception.
            task.add_done_callback(self._observe_build_result)
            self._build_task = task
        return task

    @staticmethod
    def _observe_build_result(task: asyncio.Task[PrefetchedProblem]) -> None:
        if task.cancelled():
            return
        with suppress(Exception):
            task.exception()

    async def peek(self) -> PrefetchedProblem | None:
        async with self._lock:
            return self._load_slot_locked()

    async def claim(self) -> PrefetchedProblem:
        """Claim READY, waiting for the shared build task on a cold path."""
        while True:
            async with self._lock:
                if self._claimed_slot_id is not None:
                    raise RuntimeError("next-problem slot is already claimed")
                slot = self._load_slot_locked()
                if slot is not None:
                    self._remove_slot_locked()
                    self._claimed_slot_id = slot.slot_id
                    logger.info(
                        "[group_%s] next problem claimed pid=%s age=%ss",
                        self.group_id,
                        slot.problem.pid,
                        max(0, int(time.time()) - slot.problem.prepared_at),
                    )
                    return slot
                task = self._ensure_build_task_locked()
            await asyncio.shield(task)

    async def release(self, slot_id: str) -> None:
        """Finish one publication attempt and allow the next refill."""
        async with self._lock:
            if self._claimed_slot_id != slot_id:
                logger.warning(
                    "[group_%s] ignored stale next-problem release token %s",
                    self.group_id,
                    slot_id,
                )
                return
            self._claimed_slot_id = None
            self._wake.set()

    async def _ensure_ready(self) -> PrefetchedProblem | None:
        while True:
            async with self._lock:
                if self._claimed_slot_id is not None:
                    return None
                slot = self._load_slot_locked()
                if slot is not None:
                    return slot
                task = self._ensure_build_task_locked()
            # Re-enter through validation instead of trusting the task result:
            # group state or rating overrides may have changed during a slow build.
            await asyncio.shield(task)

    def _maintain_editorial_prefetch(self, slot: PrefetchedProblem) -> None:
        # This is deliberately fire-and-forget: editorial readiness must not
        # become part of the /newproblem claim latency.  Repeated calls are
        # idempotent and let a rehydrated READY slot restart work interrupted
        # by a process exit.
        ensure_editorial_prefetch(slot.problem.pid)

    async def _wait_for_wake_or_stop(self, stop_event: asyncio.Event) -> None:
        wake_task = asyncio.create_task(self._wake.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {wake_task, stop_task},
                timeout=self._retry_interval_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (wake_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wake_task, stop_task, return_exceptions=True)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        """Worker-owned maintenance loop."""
        logger.info("[group_%s] next-problem prefetcher started", self.group_id)
        try:
            while not stop_event.is_set():
                # Clear before inspecting state so a release racing with the
                # inspection cannot be lost before the wait.
                async with self._lock:
                    self._wake.clear()
                try:
                    slot = await self._ensure_ready()
                    if slot is not None:
                        self._maintain_editorial_prefetch(slot)
                except asyncio.CancelledError:
                    if stop_event.is_set():
                        break
                    raise
                except Exception as exc:
                    logger.warning(
                        "[group_%s] next-problem prefetch failed: %s",
                        self.group_id,
                        exc,
                        exc_info=True,
                    )
                if not stop_event.is_set():
                    await self._wait_for_wake_or_stop(stop_event)
        finally:
            await self.shutdown()
            logger.info("[group_%s] next-problem prefetcher stopped", self.group_id)

    async def shutdown(self) -> None:
        """Cancel the coordinator-owned build task, if any."""
        self._wake.set()
        async with self._lock:
            task = self._build_task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await task


_prefetchers: dict[int, NextProblemPrefetcher] = {}


def get_next_problem_prefetcher(group_id: int) -> NextProblemPrefetcher:
    group_id = int(group_id)
    prefetcher = _prefetchers.get(group_id)
    if prefetcher is None:
        prefetcher = NextProblemPrefetcher(group_id)
        _prefetchers[group_id] = prefetcher
    return prefetcher


def reset_prefetchers_for_tests() -> None:
    """Drop idle module state between event-loop-isolated tests."""
    _prefetchers.clear()
