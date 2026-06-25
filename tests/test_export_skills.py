"""Tests for the data-driven ERP export "skills" loader.

Skills live under ``ledgr_agent/skills/`` (one YAML per ERP) and are loaded —
cached, fail-loud — by ``load_export_skill``. These tests guard:
  - every registered ERP skill loads and declares the right system,
  - QBS/Xero columns + logical-field maps came through the skill intact,
  - a missing or malformed skill fails LOUD (never silently emits wrong cols).
"""

from __future__ import annotations

import pytest

from invoice_processing.export import exporters
from invoice_processing.export.exporters import (
    ExportSkillError,
    QbsLedgerExporter,
    XeroLedgerExporter,
    load_export_skill,
)


class TestSkillsLoad:
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
        monkeypatch.setattr(exporters, "_SKILLS_DIR", tmp_path)
        monkeypatch.setitem(exporters._SKILL_FILES, "ghost", "ghost.yaml")
        with pytest.raises(ExportSkillError, match="missing"):
            load_export_skill("ghost")

    def test_malformed_yaml_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "broken.yaml"
        bad.write_text("software_name: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr(exporters, "_SKILLS_DIR", tmp_path)
        monkeypatch.setitem(exporters._SKILL_FILES, "broken", "broken.yaml")
        with pytest.raises(ExportSkillError):
            load_export_skill("broken")

    def test_missing_required_key_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "partial.yaml"
        # No purchase_cols / sales_cols.
        bad.write_text("software_name: Partial\nsystem: partial\n", encoding="utf-8")
        monkeypatch.setattr(exporters, "_SKILLS_DIR", tmp_path)
        monkeypatch.setitem(exporters._SKILL_FILES, "partial", "partial.yaml")
        with pytest.raises(ExportSkillError, match="missing required key"):
            load_export_skill("partial")

    def test_system_mismatch_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "mismatch.yaml"
        bad.write_text(
            "software_name: X\nsystem: other\npurchase_cols: [A]\nsales_cols: [B]\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(exporters, "_SKILLS_DIR", tmp_path)
        monkeypatch.setitem(exporters._SKILL_FILES, "mismatch", "mismatch.yaml")
        with pytest.raises(ExportSkillError, match="declares system"):
            load_export_skill("mismatch")
