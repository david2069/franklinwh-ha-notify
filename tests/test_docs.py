"""The design record stays with the code, and the contract it states is the one built."""
import pathlib

from franklinwh_ha_notify import action_id

DESIGN = (pathlib.Path(__file__).resolve().parents[1] / "docs/DESIGN.md").read_text()


def test_the_design_records_the_decisions_and_diagram():
    assert "Decisions (owner, 2026-10-31)" in DESIGN
    assert "```mermaid" in DESIGN and "sequenceDiagram" in DESIGN


def test_the_documented_button_contract_matches_the_code():
    assert "`FWH_<VERB>_<request id>`" in DESIGN
    assert action_id("YES", "0" * 16) == "FWH_YES_" + "0" * 16
