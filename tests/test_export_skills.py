"""Tests for the data-driven ERP export "skills" loader.

Skills live under ``ledgr_agent/skills/erp-*/assets/profile.yaml`` and are loaded —
cached, fail-loud — by ``load_export_skill``. These tests guard:
  - every registered ERP skill loads and declares the right system,
  - QBS/Xero columns + logical-field maps came through the skill intact,
  - a missing or malformed skill fails LOUD (never silently emits wrong cols).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_processing.export.exporters import (
    ExportSkillError,
    QbsLedgerExporter,
    XeroLedgerExporter,
    load_export_skill,
)
from ledgr_agent.internal import skill_profiles


def _profile_path(tmp_path: Path, system: str, *, dir_name: str | None = None) -> Path:
    folder = dir_name or skill_profiles._SYSTEM_DIRS[system]
    path = tmp_path / folder / "assets" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestSkillsLoad:
    def test_skill_asset_path_resolves_adk_layout(self):
        path = skill_profiles.skill_asset_path("qbs")
        assert path.name == "profile.yaml"
        assert path.parent.name == "assets"
        assert path.parent.parent.name == "erp-qbs"
        skill = load_export_skill("qbs")
        assert skill["system"] == "qbs"

    def test_every_registered_skill_loads(self):
        for system in ("qbs", "xero", "autocount", "sql_account"):
            skill = load_export_skill(system)
            assert skill["system"] == system
            assert isinstance(skill["purchase_cols"], list) and skill["purchase_cols"]
            assert isinstance(skill["sales_cols"], list) and skill["sales_cols"]

    def test_load_is_cached(self):
        assert load_export_skill("qbs") is load_export_skill("qbs")

    def test_qbs_columns_come_from_skill(self):
        skill = load_export_skill("qbs")
        assert QbsLedgerExporter.purchase_cols == list(skill["purchase_cols"])
        assert QbsLedgerExporter.sales_cols == list(skill["sales_cols"])
        assert QbsLedgerExporter._LOGICAL_FIELDS == dict(skill["logical_fields"])
        # QBS sales "Amount" carries the line net → sub_total (key quirk preserved).
        assert QbsLedgerExporter._LOGICAL_FIELDS["Amount"] == "sub_total"

    def test_xero_columns_come_from_skill(self):
        skill = load_export_skill("xero")
        assert XeroLedgerExporter.purchase_cols == list(skill["purchase_cols"])
        assert XeroLedgerExporter.sales_cols == list(skill["sales_cols"])
        # Xero *UnitAmount is per-unit, not a line net.
        assert XeroLedgerExporter._LOGICAL_FIELDS["*UnitAmount"] == "unit_amount"


class TestSkillsFailLoud:
    def test_unknown_system_raises(self):
        with pytest.raises(ExportSkillError):
            load_export_skill("wave")

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_profiles, "_SKILL_ROOT", tmp_path)
        monkeypatch.setitem(skill_profiles._SYSTEM_DIRS, "ghost", "erp-ghost")
        monkeypatch.setattr(skill_profiles, "_SKILL_CACHE", {})
        (tmp_path / "erp-ghost" / "assets").mkdir(parents=True)
        with pytest.raises(ExportSkillError, match="missing"):
            load_export_skill("ghost")

    def test_malformed_yaml_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_profiles, "_SKILL_ROOT", tmp_path)
        monkeypatch.setitem(skill_profiles._SYSTEM_DIRS, "broken", "erp-broken")
        monkeypatch.setattr(skill_profiles, "_SKILL_CACHE", {})
        bad = _profile_path(tmp_path, "broken", dir_name="erp-broken")
        bad.write_text("software_name: [unclosed\n", encoding="utf-8")
        with pytest.raises(ExportSkillError):
            load_export_skill("broken")

    def test_missing_required_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_profiles, "_SKILL_ROOT", tmp_path)
        monkeypatch.setitem(skill_profiles._SYSTEM_DIRS, "partial", "erp-partial")
        monkeypatch.setattr(skill_profiles, "_SKILL_CACHE", {})
        partial = _profile_path(tmp_path, "partial", dir_name="erp-partial")
        partial.write_text("software_name: Partial\nsystem: partial\n", encoding="utf-8")
        with pytest.raises(ExportSkillError, match="missing required key"):
            load_export_skill("partial")

    def test_system_mismatch_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skill_profiles, "_SKILL_ROOT", tmp_path)
        monkeypatch.setitem(skill_profiles._SYSTEM_DIRS, "mismatch", "erp-mismatch")
        monkeypatch.setattr(skill_profiles, "_SKILL_CACHE", {})
        mismatch = _profile_path(tmp_path, "mismatch", dir_name="erp-mismatch")
        mismatch.write_text(
            "software_name: X\nsystem: other\npurchase_cols: [A]\nsales_cols: [B]\n",
            encoding="utf-8",
        )
        with pytest.raises(ExportSkillError, match="declares system"):
            load_export_skill("mismatch")
