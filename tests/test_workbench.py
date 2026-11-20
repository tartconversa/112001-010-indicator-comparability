"""Behavioural tests for the green-transition comparison workbench."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

from greenbench import cli
from greenbench.compare import Comparator, impact_report, result_markdown, snapshot
from greenbench.models import (
    CONVERTED,
    DIRECT,
    DISCUSSION_ONLY,
    EXCLUDED,
    MISSING,
    SIDE_BY_SIDE,
    ConflictError,
    DataError,
    MappingProposal,
    MetricDefinition,
    Observation,
    Project,
    ProjectTarget,
    ReviewRecord,
    Target,
)
from greenbench.store import Store


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.json"
        self.store = Store.load(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- builders ----------------------------------------------------------

    def add_target(
        self,
        name: str = "clean_energy_share",
        unit: str = "percent",
        boundary: str = "electricity_consumed",
        direction: str = "higher_is_better",
    ) -> None:
        self.store.upsert_target(
            Target(
                target_metric=name,
                description="测试目标",
                canonical_unit=unit,
                canonical_boundary=boundary,
                direction=direction,
            )
        )

    def add_metric(
        self,
        metric_id: str = "M01",
        region: str = "Harbor-City",
        unit: str = "percent",
        boundary: str = "electricity_consumed",
        version: str = "v1",
        frequency: str = "annual",
        valid_from: str = "2024-01-01",
        valid_to: str = "",
    ) -> None:
        self.store.add_metric(
            MetricDefinition(
                metric_id=metric_id,
                region=region,
                definition="renewables_share",
                unit=unit,
                boundary=boundary,
                frequency=frequency,
                valid_from=valid_from,
                valid_to=valid_to,
                source_institution=f"{region} Office",
                definition_version=version,
            )
        )

    def add_obs(
        self,
        value: str,
        metric_id: str = "M01",
        region: str = "Harbor-City",
        period: str = "2025",
        coverage: str = "100",
        source_version: str = "v1",
        definition_version: str = "",
    ) -> None:
        self.store.add_observation(
            Observation(
                metric_id=metric_id,
                region=region,
                period=period,
                value=Decimal(value),
                coverage_pct=Decimal(coverage),
                source_version=source_version,
                definition_version=definition_version,
            )
        )

    def add_mapping(
        self,
        mapping_id: str,
        source_metric: str = "M01",
        target_metric: str = "clean_energy_share",
        transform: str = "identity",
        region: str = "",
        factor: str | None = None,
        target_unit: str = "",
        definition_version: str = "",
    ) -> None:
        self.store.add_proposal(
            MappingProposal(
                mapping_id=mapping_id,
                source_metric=source_metric,
                target_metric=target_metric,
                transform=transform,
                proposed_by="team_a",
                region=region,
                target_unit=target_unit,
                conversion_factor=Decimal(factor) if factor is not None else None,
                rationale="测试依据",
                source_definition_version=definition_version,
            )
        )

    def review(self, mapping_id: str, decision: str, region: str = "") -> None:
        self.store.add_review(
            ReviewRecord(
                mapping_id=mapping_id,
                decision=decision,
                reviewer_region=region,
                reviewed_at="2026-06-01",
            )
        )

    def project(self, *regions: str, threshold: str = "90", min_coverage: str = "80") -> Project:
        if not regions:
            regions = ("Harbor-City", "Hill-Region")
        return Project(
            project_id="PRJ-1",
            title="测试项目",
            regions=tuple(regions),
            targets=(ProjectTarget("clean_energy_share", "2025"),),
            coverage_threshold=Decimal(threshold),
            min_source_coverage=Decimal(min_coverage),
        )

    def compare(self, project: Project | None = None) -> dict:
        project = project or self.project()
        return Comparator(self.store).compare(project)

    # -- direct comparison -------------------------------------------------

    def test_direct_comparison_ranks_two_comparable_regions(self):
        self.add_target()
        for region, value in (("Harbor-City", "41.2"), ("Hill-Region", "48.6")):
            self.add_metric(region=region)
            self.add_obs(value, region=region)
            self.add_mapping(f"X-{region}", region=region)
            self.review(f"X-{region}", "accepted", region)

        result = self.compare()
        target = result["targets"]["clean_energy_share"]
        self.assertEqual(target["cells"]["Harbor-City"]["evidence"], DIRECT)
        self.assertEqual(target["cells"]["Hill-Region"]["evidence"], DIRECT)
        self.assertEqual([item["region"] for item in target["ranking"]], ["Hill-Region", "Harbor-City"])
        self.assertEqual(target["ranking"][0]["value"], "48.6")
        # Only one target is incomplete (Hill-Region side-by-side? no: both
        # direct), so the composite ranking is allowed but coverage is 100%.
        self.assertEqual(result["coverage"]["pct"], "100.00")
        self.assertEqual(result["overall"]["status"], "ranked")

    def test_lower_is_better_reverses_order(self):
        self.add_target(direction="lower_is_better")
        for region, value in (("Harbor-City", "41.2"), ("Hill-Region", "48.6")):
            self.add_metric(region=region)
            self.add_obs(value, region=region)
            self.add_mapping(f"X-{region}", region=region)
            self.review(f"X-{region}", "accepted", region)
        ranking = self.compare()["targets"]["clean_energy_share"]["ranking"]
        self.assertEqual([item["region"] for item in ranking], ["Harbor-City", "Hill-Region"])

    def test_ties_share_rank(self):
        self.add_target()
        for region, value in (("Harbor-City", "48.6"), ("Hill-Region", "48.6")):
            self.add_metric(region=region)
            self.add_obs(value, region=region)
            self.add_mapping(f"X-{region}", region=region)
            self.review(f"X-{region}", "accepted", region)
        ranking = self.compare()["targets"]["clean_energy_share"]["ranking"]
        self.assertEqual({item["rank"] for item in ranking}, {1})
        self.assertTrue(all(item["tied"] for item in ranking))

    # -- conversion vs boundary -------------------------------------------

    def test_unit_conversion_is_comparable_converted(self):
        self.add_target(unit="percent")
        self.add_metric(region="Harbor-City", unit="percent")
        self.add_metric(region="Hill-Region", unit="fraction")
        self.add_obs("0.412", region="Harbor-City")
        self.add_obs("0.486", region="Hill-Region")
        self.add_mapping("XH", region="Harbor-City")
        self.add_mapping("XG", region="Hill-Region", transform="unit_only", factor="100")
        self.review("XH", "accepted")
        self.review("XG", "accepted")

        cells = self.compare()["targets"]["clean_energy_share"]["cells"]
        self.assertEqual(cells["Harbor-City"]["evidence"], DIRECT)
        self.assertEqual(cells["Hill-Region"]["evidence"], CONVERTED)
        self.assertEqual(cells["Hill-Region"]["value"], "48.6")
        self.assertEqual(cells["Hill-Region"]["unit"], "percent")

    def test_scope_adjustment_never_becomes_comparable(self):
        self.add_target()
        self.add_metric(region="Harbor-City", boundary="electricity_generated")
        self.add_obs("48.6", region="Harbor-City")
        self.add_mapping(
            "X1",
            region="Harbor-City",
            transform="scope_adjustment",
            factor="1",
        )
        self.review("X1", "accepted")
        cell = self.compare()["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], SIDE_BY_SIDE)
        self.assertFalse(cell["scored"])
        self.assertTrue(any("边界" in reason for reason in cell["reasons"]))

    def test_unit_conversion_cannot_repair_boundary_mismatch(self):
        self.add_target()
        self.add_metric(region="Harbor-City", unit="fraction", boundary="electricity_generated")
        self.add_obs("0.486", region="Harbor-City")
        self.add_mapping("X1", region="Harbor-City", transform="unit_only", factor="100")
        self.review("X1", "accepted")
        cell = self.compare()["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], SIDE_BY_SIDE)
        self.assertFalse(cell["scored"])
        # The raw value stays visible for side-by-side discussion, carrying
        # its own source unit and boundary.
        self.assertEqual(cell["value"], "0.486")
        self.assertEqual(cell["unit"], "fraction")
        self.assertEqual(cell["boundary"], "electricity_generated")

    def test_estimate_is_discussion_only_and_not_ranked(self):
        self.add_target()
        self.add_metric(region="Harbor-City")
        self.add_metric(region="Hill-Region")
        self.add_obs("41.2", region="Harbor-City")
        self.add_obs("48.6", region="Hill-Region")
        self.add_mapping("XH", region="Harbor-City", transform="estimate")
        self.add_mapping("XG", region="Hill-Region")
        self.review("XH", "accepted")
        self.review("XG", "accepted")
        result = self.compare()
        cells = result["targets"]["clean_energy_share"]["cells"]
        self.assertEqual(cells["Harbor-City"]["evidence"], DISCUSSION_ONLY)
        self.assertEqual(cells["Harbor-City"]["value"], "41.2")
        self.assertEqual([item["region"] for item in result["targets"]["clean_energy_share"]["ranking"]], ["Hill-Region"])

    # -- missing periods and revisions ------------------------------------

    def test_missing_period_is_never_interpolated(self):
        self.add_target()
        self.add_metric()
        self.add_obs("40.0", period="2024")
        self.add_obs("44.0", period="2026")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        cell = self.compare()["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], MISSING)
        self.assertIsNone(cell["value"])
        self.assertTrue(any("不插值" in reason for reason in cell["reasons"]))

    def test_source_revision_requires_new_version_and_uses_latest(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2", source_version="v3")
        with self.assertRaises(ConflictError):
            self.add_obs("99.9", source_version="v3")
        self.add_obs("43.0", source_version="v4")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        cell = self.compare()["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["observation"]["source_version"], "v4")
        self.assertEqual(cell["observation"]["value"], "43")
        self.assertEqual(len(cell["trace"][1]["source_versions"]), 2)

    def test_definition_revision_requires_new_version(self):
        self.add_target()
        self.add_metric(version="v1")
        self.add_metric(version="v2", boundary="electricity_generated")
        self.assertEqual(len(self.store.definitions_for("M01", "Harbor-City")), 2)
        with self.assertRaises(ConflictError):
            self.add_metric(version="v1", boundary="electricity_generated")

    def test_observation_bound_to_unknown_definition_version_rejected(self):
        self.add_target()
        self.add_metric(version="v1")
        with self.assertRaises(DataError):
            self.add_obs("41.2", definition_version="v9")

    # -- reviews -----------------------------------------------------------

    def test_rejected_mapping_is_excluded_not_ranked(self):
        self.add_target()
        for region, value in (("Harbor-City", "41.2"), ("Hill-Region", "48.6")):
            self.add_metric(region=region)
            self.add_obs(value, region=region)
            self.add_mapping(f"X-{region}", region=region)
        self.review("X-Harbor-City", "rejected")
        self.review("X-Hill-Region", "accepted")
        result = self.compare()
        cells = result["targets"]["clean_energy_share"]["cells"]
        self.assertEqual(cells["Harbor-City"]["evidence"], EXCLUDED)
        self.assertFalse(cells["Harbor-City"]["scored"])
        self.assertEqual(result["targets"]["clean_energy_share"]["coverage"]["pct"], "50.00")
        self.assertEqual(result["overall"]["status"], "insufficient_coverage")

    def test_regional_rejection_only_affects_that_regions_cell(self):
        self.add_target()
        for region, value in (("Harbor-City", "41.2"), ("Hill-Region", "48.6")):
            self.add_metric(region=region)
            self.add_obs(value, region=region)
        # One shared mapping, reviewed by each partner separately.
        self.add_mapping("X1")
        self.review("X1", "accepted", region="Harbor-City")
        self.review("X1", "rejected", region="Hill-Region")
        cells = self.compare()["targets"]["clean_energy_share"]["cells"]
        self.assertEqual(cells["Harbor-City"]["evidence"], DIRECT)
        self.assertEqual(cells["Hill-Region"]["evidence"], EXCLUDED)

    def test_global_rejection_affects_every_region(self):
        self.add_target()
        for region in ("Harbor-City", "Hill-Region"):
            self.add_metric(region=region)
            self.add_obs("41.2", region=region)
        self.add_mapping("X1")
        self.review("X1", "accepted")
        self.review("X1", "rejected")
        cells = self.compare()["targets"]["clean_energy_share"]["cells"]
        self.assertEqual({cell["evidence"] for cell in cells.values()}, {EXCLUDED})

    def test_pending_mapping_is_not_used(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2")
        self.add_mapping("X1")
        cell = self.compare(Project("PRJ-1", "t", ("Harbor-City",), (ProjectTarget("clean_energy_share", "2025"),)))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], EXCLUDED)

    def test_revising_proposal_invalidates_earlier_review(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2")
        self.add_mapping("X1", transform="identity")
        self.review("X1", "accepted")
        self.assertEqual(self.store.effective_review("X1")[0], "accepted")
        self.add_mapping("X1", transform="unit_only", factor="100")
        status, _, stale = self.store.effective_review("X1")
        self.assertEqual(status, "pending")
        self.assertTrue(stale)
        cell = self.compare(self.project("Harbor-City"))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], EXCLUDED)
        self.assertTrue(any("旧版映射提案" in reason for reason in cell["reasons"]))

    # -- coverage and combined conclusions --------------------------------

    def _two_target_two_region_store(self) -> None:
        self.add_target()
        self.add_target(name="active_mobility", unit="journeys", boundary="person_trip")
        for region in ("Harbor-City", "Hill-Region"):
            self.add_metric(metric_id="M01", region=region)
            self.add_metric(metric_id="M02", region=region, unit="journeys", boundary="person_trip")
            self.add_obs("41.2", metric_id="M01", region=region)
            self.add_obs("1000", metric_id="M02", region=region, period="2025")
            self.add_mapping(f"XE-{region}", region=region)
            self.add_mapping(f"XM-{region}", source_metric="M02", target_metric="active_mobility", region=region)
            self.review(f"XE-{region}", "accepted")
            self.review(f"XM-{region}", "accepted")

    def _two_target_project(self) -> Project:
        return Project(
            project_id="PRJ-2",
            title="双指标项目",
            regions=("Harbor-City", "Hill-Region"),
            targets=(
                ProjectTarget("clean_energy_share", "2025"),
                ProjectTarget("active_mobility", "2025"),
            ),
        )

    def test_partner_rejection_changes_coverage_without_silent_old_ranking(self):
        self._two_target_two_region_store()
        before = self.compare(self._two_target_project())
        self.assertEqual(before["overall"]["status"], "ranked")
        shared = snapshot(before)
        shared["shared_result_id"] = "RST-1"

        self.review("XE-Harbor-City", "rejected")
        after = self.compare(self._two_target_project())

        # The other accepted indicator still works for the region.
        self.assertEqual(
            after["targets"]["active_mobility"]["cells"]["Harbor-City"]["evidence"],
            DIRECT,
        )
        # Overall conclusion is withdrawn, coverage falls steeply, and the
        # previous ordering is not carried over.
        self.assertEqual(after["overall"]["status"], "insufficient_coverage")
        self.assertEqual(after["overall"]["ranking"], [])
        self.assertLess(Decimal(after["coverage"]["pct"]), Decimal(before["coverage"]["pct"]))
        report = impact_report(shared, after)
        self.assertTrue(report["coverage_change"]["changed"])
        self.assertTrue(report["conclusion_status_change"]["changed"])
        self.assertTrue(report["affected_cells"])

    def test_shared_snapshot_survives_source_revision(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2", source_version="v3")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        project = self.project("Harbor-City")
        result = self.compare(project)
        shared = snapshot(result)
        shared["shared_result_id"] = "RST-1"
        self.store.save_share(shared)
        self.store.save()
        with self.assertRaises(ConflictError):
            self.store.save_share(shared)

        # A later source revision changes the live result but not the share.
        self.add_obs("45.0", source_version="v4")
        current = self.compare(project)
        self.assertEqual(current["targets"]["clean_energy_share"]["cells"]["Harbor-City"]["value"], "45")
        frozen = self.store.share("RST-1")
        self.assertEqual(frozen["cells"]["clean_energy_share|Harbor-City"]["value"], "41.2")
        reloaded = Store.load(self.db)
        self.assertEqual(
            reloaded.share("RST-1")["cells"]["clean_energy_share|Harbor-City"]["value"],
            "41.2",
        )

        impact = impact_report(frozen, current)
        changed = {item["cell"]: item for item in impact["affected_cells"]}
        self.assertIn("clean_energy_share|Harbor-City", changed)
        self.assertEqual(
            changed["clean_energy_share|Harbor-City"]["fields"]["source_version"],
            {"shared": "v3", "current": "v4"},
        )

    def test_partner_definition_revision_shows_up_in_impact_report(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2", definition_version="v1")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        project = self.project("Harbor-City")
        shared = snapshot(self.compare(project))
        shared["shared_result_id"] = "RST-1"

        # Partner supplies a revised local definition plus a matching revision
        # of the observation; the new definition uses a different boundary.
        self.add_metric(version="v2", boundary="electricity_generated")
        self.add_obs("48.0", source_version="v2", definition_version="v2")
        current = self.compare(project)
        cell = current["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], SIDE_BY_SIDE)
        self.assertEqual(cell["definition"]["definition_version"], "v2")
        report = impact_report(shared, current)
        changed = {item["cell"]: item for item in report["affected_cells"]}
        self.assertEqual(
            changed["clean_energy_share|Harbor-City"]["fields"]["definition_version"],
            {"shared": "v1", "current": "v2"},
        )
        self.assertEqual(
            changed["clean_energy_share|Harbor-City"]["fields"]["evidence"],
            {"shared": DIRECT, "current": SIDE_BY_SIDE},
        )

    def test_observation_definition_version_selection(self):
        self.add_target()
        self.add_metric(version="v1")
        self.add_metric(version="v2")
        # v1 observation must not be paired with the newer v2 definition.
        self.add_obs("41.2", definition_version="v1", source_version="v1")
        self.add_obs("43.0", definition_version="v2", source_version="v2")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        cell = self.compare(self.project("Harbor-City"))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["definition"]["definition_version"], "v2")
        self.assertEqual(cell["observation"]["source_version"], "v2")
        self.assertEqual(cell["observation"]["value"], "43")

    def test_project_mapping_pin_is_honoured(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2")
        # Two eligible mappings; the project explicitly adopts the second.
        self.add_mapping("X-A")
        self.add_mapping("X-B", transform="unit_only", factor="1")
        for mapping_id in ("X-A", "X-B"):
            self.review(mapping_id, "accepted")
        project = Project(
            project_id="PRJ-PIN",
            title="固定映射",
            regions=("Harbor-City",),
            targets=(
                ProjectTarget(
                    "clean_energy_share",
                    "2025",
                    mappings={"Harbor-City": "X-B"},
                ),
            ),
        )
        cell = self.compare(project)["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["mapping"]["mapping_id"], "X-B")
        self.assertEqual([alt["mapping_id"] for alt in cell["alternatives"]], ["X-A"])

    def test_duplicate_target_names_rejected(self):
        with self.assertRaises(DataError):
            Project.from_dict(
                {
                    "project_id": "P",
                    "title": "t",
                    "regions": ["A"],
                    "targets": [
                        {"target_metric": "x", "period": "2025"},
                        {"target_metric": "x", "period": "2026"},
                    ],
                }
            )

    def test_duplicate_project_regions_rejected(self):
        with self.assertRaises(DataError):
            Project.from_dict(
                {
                    "project_id": "P",
                    "title": "t",
                    "regions": ["A", "A"],
                    "targets": [{"target_metric": "x", "period": "2025"}],
                }
            )

    def test_project_revision_ignores_bookkeeping_timestamps(self):
        project = self.project()
        saved = self.store.save_project(project)
        first = saved.revision()
        saved_again = self.store.save_project(saved)
        self.assertEqual(first, saved_again.revision())
        changed = Project(
            **{
                **project.__dict__,
                "coverage_threshold": Decimal("50"),
            }
        )
        self.assertNotEqual(first, changed.revision())

    # -- traceability ------------------------------------------------------

    def test_cell_trace_contains_definition_mapping_and_review(self):
        self.add_target()
        self.add_metric()
        self.add_obs("41.2", source_version="v3", definition_version="v1")
        self.add_mapping("X1", transform="unit_only", factor="1", definition_version="v1")
        self.review("X1", "accepted", region="Harbor-City")
        cell = self.compare(self.project("Harbor-City"))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        kinds = [item["kind"] for item in cell["trace"]]
        self.assertEqual(kinds, ["metric_definition", "observation", "mapping", "reviews", "classification"])
        definition = cell["trace"][0]
        self.assertEqual(definition["record"]["definition_version"], "v1")
        self.assertEqual(definition["versions"][0]["definition_version"], "v1")
        self.assertEqual(cell["trace"][1]["record"]["source_version"], "v3")
        self.assertEqual(cell["trace"][2]["record"]["conversion_factor"], "1")
        self.assertEqual(cell["trace"][3]["records"][0]["decision"], "accepted")

    def test_markdown_separates_comparable_and_discussion_only(self):
        self.add_target()
        self.add_target(name="active_mobility", unit="journeys", boundary="person_trip")
        self.add_metric()
        self.add_metric(metric_id="M02", unit="journeys", boundary="person_trip")
        self.add_obs("41.2")
        self.add_obs("1000", metric_id="M02")
        self.add_mapping("X1", transform="estimate")
        self.add_mapping("X2", source_metric="M02", target_metric="active_mobility")
        self.review("X1", "accepted")
        self.review("X2", "accepted")
        project = Project(
            project_id="PRJ-3",
            title="方法示例",
            regions=("Harbor-City",),
            targets=(
                ProjectTarget("clean_energy_share", "2025"),
                ProjectTarget("active_mobility", "2025"),
            ),
        )
        text = result_markdown(self.compare(project))
        self.assertIn("仅供讨论的估算", text)
        self.assertIn("可比覆盖", text)
        self.assertIn("不输出综合排名", text)

    def test_target_unit_cannot_differ_across_regions(self):
        # The target defines one canonical unit; a mapping cannot change it.
        self.add_target(unit="percent")
        self.add_metric(region="Harbor-City", unit="percent")
        self.add_metric(region="Hill-Region", unit="fraction")
        self.add_obs("41.2", region="Harbor-City")
        self.add_obs("0.486", region="Hill-Region")
        self.add_mapping("XH", region="Harbor-City")
        self.add_mapping("XG", region="Hill-Region", transform="unit_only", factor="1", target_unit="fraction")
        self.review("XH", "accepted")
        self.review("XG", "accepted")
        cells = self.compare()["targets"]["clean_energy_share"]["cells"]
        self.assertEqual(cells["Harbor-City"]["evidence"], DIRECT)
        self.assertEqual(cells["Hill-Region"]["evidence"], SIDE_BY_SIDE)
        self.assertEqual(
            [entry["region"] for entry in self.compare()["targets"]["clean_energy_share"]["ranking"]],
            ["Harbor-City"],
        )

    def test_unregistered_target_is_rejected(self):
        project = Project(
            project_id="PRJ-4",
            title="未登记目标",
            regions=("Harbor-City",),
            targets=(ProjectTarget("unknown_target", "2025"),),
        )
        with self.assertRaises(DataError):
            self.compare(project)

    def test_period_frequency_mismatch_is_not_compared(self):
        self.add_target()
        self.add_metric(frequency="monthly")
        self.add_obs("420000", period="2025")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        cell = self.compare(self.project("Harbor-City"))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], SIDE_BY_SIDE)
        self.assertFalse(cell["scored"])
        self.assertTrue(any("频率" in reason for reason in cell["reasons"]))

    def test_target_without_boundary_cannot_be_comparable(self):
        self.store.upsert_target(Target("clean_energy_share", canonical_unit="percent"))
        self.add_metric()
        self.add_obs("41.2")
        self.add_mapping("X1")
        self.review("X1", "accepted")
        cell = self.compare(self.project("Harbor-City"))["targets"]["clean_energy_share"]["cells"]["Harbor-City"]
        self.assertEqual(cell["evidence"], SIDE_BY_SIDE)


class FixtureEndToEndTests(unittest.TestCase):
    def test_repository_fixtures_flow_through_cli(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "db.json")
            out = Path(tmp) / "report.md"

            def run(*args: str) -> int:
                with redirect_stdout(io.StringIO()):
                    return cli.main(["--db", db, *args])

            self.assertEqual(run("import", "targets", str(root / "data/targets.csv")), 0)
            self.assertEqual(run("import", "metrics", str(root / "data/metrics.csv")), 0)
            self.assertEqual(run("import", "observations", str(root / "data/observations.csv")), 0)
            self.assertEqual(run("import", "mappings_reviews", str(root / "data/mappings_reviews.csv")), 0)

            project_file = Path(tmp) / "project.json"
            project_file.write_text(
                json.dumps(
                    {
                        "project_id": "PRJ-E2E",
                        "title": "端到端检查",
                        "regions": ["Harbor-City", "Hill-Region", "Delta-Port"],
                        "coverage_threshold": "90",
                        "targets": [{"target_metric": "clean_energy_share", "period": "2025"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(run("project-create", str(project_file)), 0)
            self.assertEqual(run("compare", "PRJ-E2E", "--output", str(out)), 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("Delta-Port", report)
            self.assertIn("并列呈现", report)

            self.assertEqual(run("share", "PRJ-E2E", "RST-E2E"), 0)
            self.assertEqual(run("review", "X01", "rejected", "--region", "Harbor-City"), 0)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(cli.main(["--db", db, "impact", "RST-E2E"]), 0)
            impact = json.loads(buffer.getvalue())
            self.assertTrue(impact["coverage_change"]["changed"])
            self.assertEqual(
                impact["coverage_change"]["shared"], "66.67"
            )
            self.assertEqual(impact["coverage_change"]["current"], "33.33")


if __name__ == "__main__":
    unittest.main()
