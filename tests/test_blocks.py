"""Snapshot tests for app.blocks.processing_plan_blocks."""

from __future__ import annotations

import pytest

import app.native_blocks_compat as compat
from app.blocks import PIPELINE_STAGES, _STAGE_TITLES, processing_plan_blocks


def _all_pending() -> list[dict]:
    return [
        {"task_id": k, "title": _STAGE_TITLES[k], "status": "pending", "output": None}
        for k in PIPELINE_STAGES
    ]


def _mixed() -> list[dict]:
    stages = _all_pending()
    stages[0]["status"] = "complete"
    stages[0]["output"] = "Recognized as invoice"
    stages[1]["status"] = "in_progress"
    return stages


def _all_complete() -> list[dict]:
    stages = _all_pending()
    for s in stages:
        s["status"] = "complete"
    return stages


def _with_failed(key: str) -> list[dict]:
    stages = _all_pending()
    for s in stages:
        if s["task_id"] == key:
            s["status"] = "failed"
            break
    return stages


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    compat._reset_for_tests()
    yield
    compat._reset_for_tests()


class TestNativePath:

    @pytest.fixture(autouse=True)
    def _force_native(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "1")

    def test_all_pending_shape(self):
        blocks = processing_plan_blocks("invoice.pdf", stages=_all_pending())
        assert len(blocks) == 1
        b = blocks[0]
        assert b["type"] == "plan"
        assert b["title"] == "Processing invoice.pdf"
        assert len(b["tasks"]) == len(PIPELINE_STAGES)
        for task in b["tasks"]:
            assert task["status"] == "pending"
            assert "output" not in task

    def test_mixed_stage_output_in_rich_text(self):
        blocks = processing_plan_blocks("doc.pdf", stages=_mixed())
        tasks = blocks[0]["tasks"]
        # first stage complete with output
        assert tasks[0]["status"] == "complete"
        rt = tasks[0]["output"]
        assert rt["type"] == "rich_text"
        assert rt["elements"][0]["type"] == "rich_text_section"
        assert rt["elements"][0]["elements"][0]["text"] == "Recognized as invoice"
        # second stage in_progress, no output
        assert tasks[1]["status"] == "in_progress"
        assert "output" not in tasks[1]
        # remainder pending
        for task in tasks[2:]:
            assert task["status"] == "pending"

    def test_all_complete(self):
        blocks = processing_plan_blocks("stmt.pdf", stages=_all_complete())
        tasks = blocks[0]["tasks"]
        assert all(t["status"] == "complete" for t in tasks)

    def test_failed_maps_to_complete_with_x_prefix(self):
        stages = _with_failed("policy")
        blocks = processing_plan_blocks("r.pdf", stages=stages)
        tasks = blocks[0]["tasks"]
        policy_task = next(t for t in tasks if t["task_id"] == "policy")
        assert policy_task["status"] == "complete"
        assert policy_task["title"].startswith(":x:")

    def test_output_none_omits_key(self):
        stages = _all_pending()
        blocks = processing_plan_blocks("f.pdf", stages=stages)
        for task in blocks[0]["tasks"]:
            assert "output" not in task

    def test_plan_title_is_bare_string_not_plain_text_object(self):
        blocks = processing_plan_blocks("f.pdf", stages=_all_pending())
        assert isinstance(blocks[0]["title"], str)

    def test_task_title_is_bare_string_not_plain_text_object(self):
        blocks = processing_plan_blocks("f.pdf", stages=_all_pending())
        for task in blocks[0]["tasks"]:
            assert isinstance(task["title"], str)


class TestFallbackPath:

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setenv("LEDGR_NATIVE_BLOCKS", "0")

    def test_returns_section_and_context(self):
        blocks = processing_plan_blocks("inv.pdf", stages=_all_pending())
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "context"

    def test_header_contains_file_label(self):
        blocks = processing_plan_blocks("my-doc.pdf", stages=_all_pending())
        text = blocks[0]["text"]["text"]
        assert "my-doc.pdf" in text

    def test_pending_uses_white_circle(self):
        blocks = processing_plan_blocks("f.pdf", stages=_all_pending())
        ctx = blocks[1]["elements"][0]["text"]
        assert ":white_circle:" in ctx

    def test_complete_uses_check_mark(self):
        stages = _all_complete()
        blocks = processing_plan_blocks("f.pdf", stages=stages)
        ctx = blocks[1]["elements"][0]["text"]
        assert ":white_check_mark:" in ctx

    def test_in_progress_uses_blue_circle(self):
        stages = _mixed()
        blocks = processing_plan_blocks("f.pdf", stages=stages)
        ctx = blocks[1]["elements"][0]["text"]
        assert ":large_blue_circle:" in ctx

    def test_failed_uses_x(self):
        stages = _with_failed("understand")
        blocks = processing_plan_blocks("f.pdf", stages=stages)
        ctx = blocks[1]["elements"][0]["text"]
        assert ":x:" in ctx

    def test_all_stage_titles_present(self):
        blocks = processing_plan_blocks("f.pdf", stages=_all_pending())
        ctx = blocks[1]["elements"][0]["text"]
        for title in _STAGE_TITLES.values():
            assert title in ctx

    def test_channel_id_env_ignored_when_forced_off(self):
        blocks = processing_plan_blocks("f.pdf", stages=_all_pending(), channel_id="C123")
        assert blocks[0]["type"] == "section"
