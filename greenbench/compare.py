"""Comparison engine.

The engine refuses to collapse every input into one score.  Each region/target
cell is classified into an evidence class first, and only cells that are
*actually* comparable enter a ranking.  Missing periods are never filled by
interpolation, and boundary differences survive a unit conversion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import models
from .store import Store
from .models import (
    COMPARABLE_CLASSES,
    CONVERTED,
    DIRECT,
    DISCUSSION_ONLY,
    EVIDENCE_LABELS,
    EXCLUDED,
    MISSING,
    SIDE_BY_SIDE,
    MappingProposal,
    MetricDefinition,
    Observation,
    Project,
    ProjectTarget,
    ReviewRecord,
    Target,
    decimal_text,
    now_iso,
    percent_text,
    valid_for_period,
    version_key,
)

_EVIDENCE_PRIORITY = {
    DIRECT: 0,
    CONVERTED: 1,
    SIDE_BY_SIDE: 2,
    DISCUSSION_ONLY: 3,
    MISSING: 4,
    EXCLUDED: 5,
}


def _compact_review(review: ReviewRecord) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "decision": review.decision,
        "reviewer_region": review.reviewer_region,
        "reviewed_at": review.reviewed_at,
        "note": review.note,
    }


def _latest_definition(
    definitions: list[MetricDefinition], period: str
) -> tuple[MetricDefinition | None, list[str]]:
    reasons: list[str] = []
    if not definitions:
        return None, ["该地区没有登记该来源指标的定义"]
    valid = [item for item in definitions if valid_for_period(item.valid_from, item.valid_to, period)]
    ordered = sorted(definitions, key=lambda item: version_key(item.definition_version))
    if not valid:
        latest = ordered[-1]
        reasons.append(
            f"指标定义 {latest.definition_version} 的适用期 "
            f"{latest.valid_from or '未注明'}–{latest.valid_to or '未注明'} 不覆盖期间 {period}"
        )
        return None, reasons
    chosen = sorted(valid, key=lambda item: version_key(item.definition_version))[-1]
    if len(valid) > 1 and len({item.record_id for item in valid}) > 1:
        reasons.append(
            "同一地区同一指标有多个覆盖该期间的定义版本，采用最新版本 "
            f"{chosen.definition_version}；其他版本见追踪"
        )
    return chosen, reasons


def _select_observation(
    store: Store,
    metric_id: str,
    region: str,
    period: str,
    *,
    definition_version: str = "",
) -> tuple[Observation | None, list[Observation], list[str]]:
    versions = store.observation_versions(metric_id, region, period)
    reasons: list[str] = []
    if not versions:
        available = sorted(
            {
                obs.period
                for obs in store.observations()
                if obs.metric_id == metric_id and obs.region == region
            }
        )
        if available:
            reasons.append(
                f"期间 {period} 无观测；系统不插值。已有期间: {', '.join(available)}"
            )
        else:
            reasons.append("该地区该来源指标没有任何观测记录")
        return None, versions, reasons
    usable = [
        obs
        for obs in versions
        if not obs.definition_version
        or not definition_version
        or obs.definition_version == definition_version
    ]
    if not usable:
        reasons.append(
            f"期间 {period} 的观测都绑定在其他定义版本上，不能与 "
            f"{definition_version} 配对"
        )
        return None, versions, reasons
    chosen = usable[-1]
    if len(usable) > 1:
        reasons.append(
            f"同一期间存在多个来源修订版本，采用最新 {chosen.source_version}；"
            "历史版本保留用于追踪与分享核对"
        )
    return chosen, versions, reasons


def _boundary_matches(definition: MetricDefinition, target: Target) -> bool:
    return (
        bool(target.canonical_boundary)
        and definition.boundary.strip().casefold()
        == target.canonical_boundary.strip().casefold()
    )


def _expected_unit(target: Target) -> str:
    """The canonical unit always comes from the target concept.

    A mapping declaring a different target unit cannot define its own
    comparison unit; that would make two "comparable" cells rank in different
    units without anyone noticing.
    """
    return target.canonical_unit.strip()


def _unit_matches(definition: MetricDefinition, target: Target) -> bool:
    expected = _expected_unit(target).casefold()
    return bool(expected) and definition.unit.strip().casefold() == expected


def _classify(
    proposal: MappingProposal,
    definition: MetricDefinition,
    target: Target,
    *,
    review_status: str,
) -> tuple[str, list[str], Decimal | None]:
    """Return ``(evidence_class, reasons, converted_value_factor)``.

    The classification deliberately ignores the partner's approval when the
    underlying definitions are not comparable: an accepted scope adjustment is
    still only side-by-side, and an estimate is still discussion-only.
    """
    reasons: list[str] = []
    factor: Decimal | None = None

    if review_status == "rejected":
        return EXCLUDED, ["当地伙伴已拒绝该映射，未纳入比较"], None
    if review_status != "accepted":
        return EXCLUDED, ["映射尚未获得当地伙伴认可，未纳入比较"], None

    if not _expected_unit(target):
        return SIDE_BY_SIDE, ["目标指标未声明单位，无法确认可比性，仅并列呈现"], None
    if not target.canonical_boundary:
        return SIDE_BY_SIDE, ["目标指标未声明统计边界，无法确认可比性，仅并列呈现"], None
    if proposal.target_unit and proposal.target_unit.strip().casefold() != _expected_unit(target).casefold():
        return SIDE_BY_SIDE, [
            f"映射声明的目标单位「{proposal.target_unit}」与目标指标规范单位"
            f"「{target.canonical_unit}」不一致，仅并列呈现"
        ], None

    if proposal.transform == "estimate":
        reasons.append("该映射为估算，仅可与伙伴讨论，不进入排名")
        return DISCUSSION_ONLY, reasons, None

    if proposal.transform == "scope_adjustment":
        reasons.append(
            "统计边界不同；单位换算或范围调整不能修复边界差异，因此仅并列呈现"
        )
        return SIDE_BY_SIDE, reasons, None

    if not _boundary_matches(definition, target):
        reasons.append(
            f"统计边界不同（来源「{definition.boundary}」，"
            f"目标「{target.canonical_boundary}」），仅并列呈现"
        )
        return SIDE_BY_SIDE, reasons, None

    if proposal.transform == "unit_only":
        if proposal.conversion_factor is None:
            reasons.append("换算映射缺少 conversion_factor，不能直接比较")
            return SIDE_BY_SIDE, reasons, None
        factor = proposal.conversion_factor
        if _unit_matches(definition, target):
            if factor == 1:
                reasons.append("来源单位与目标单位相同且换算系数为 1，可直接比较")
                return DIRECT, reasons, None
            reasons.append(
                "来源单位与目标单位相同，但映射声明了非 1 的换算系数；"
                "转换依据不足，仅并列呈现"
            )
            return SIDE_BY_SIDE, reasons, None
        reasons.append(
            f"单位换算：{definition.unit} × {decimal_text(factor)} = "
            f"{target.canonical_unit}"
        )
        return CONVERTED, reasons, factor

    # identity
    if not _unit_matches(definition, target):
        reasons.append(
            f"单位不同（来源「{definition.unit}」，"
            f"目标「{target.canonical_unit}」），仅并列呈现"
        )
        return SIDE_BY_SIDE, reasons, None
    reasons.append("定义、单位与统计边界一致，可直接比较")
    return DIRECT, reasons, None


class Comparator:
    def __init__(self, store: Store):
        self.store = store

    def compare(self, project: Project) -> dict[str, Any]:
        targets = [self._target_concept(item.target_metric) for item in project.targets]
        missing_targets = [
            item.target_metric for item, concept in zip(project.targets, targets) if concept is None
        ]
        if missing_targets:
            raise models.DataError(
                "项目引用了未登记的目标指标: " + ", ".join(missing_targets)
            )

        cell_index: dict[tuple[str, str], dict[str, Any]] = {}
        target_reports: dict[str, Any] = {}
        caveats: list[str] = []

        for project_target, concept in zip(project.targets, targets):
            cells: dict[str, Any] = {}
            for region in project.regions:
                cell = self._cell(project, project_target, concept, region)
                cells[region] = cell
                cell_index[(project_target.target_metric, region)] = cell

            comparable = [
                cell for cell in cells.values() if cell["evidence"] in COMPARABLE_CLASSES
            ]
            ranking = self._rank(comparable, concept.direction)
            not_ranked = [
                {
                    "region": cell["region"],
                    "evidence": cell["evidence"],
                    "evidence_label": EVIDENCE_LABELS[cell["evidence"]],
                    "reason": "；".join(cell["reasons"]) or EVIDENCE_LABELS[cell["evidence"]],
                }
                for cell in cells.values()
                if cell["evidence"] in (EXCLUDED, MISSING)
            ]
            per_target_coverage = {
                "comparable_regions": len(comparable),
                "total_regions": len(project.regions),
                "pct": percent_text(len(comparable), len(project.regions)),
                "threshold": decimal_text(project.coverage_threshold),
                "sufficient": (
                    len(comparable) == len(project.regions)
                    or Decimal(len(comparable) * 100) / Decimal(len(project.regions))
                    >= project.coverage_threshold
                ),
            }
            if len(comparable) == 0:
                caveats.append(
                    f"{project_target.target_metric} / {project_target.period}: "
                    "没有任何地区可比，不能形成结论"
                )
            elif len(comparable) == 1:
                caveats.append(
                    f"{project_target.target_metric} / {project_target.period}: "
                    "仅 1 个地区可比，不构成排名"
                )
            if not per_target_coverage["sufficient"]:
                caveats.append(
                    f"{project_target.target_metric}: 可比覆盖 {per_target_coverage['pct']}% "
                    f"低于 {per_target_coverage['threshold']}%，该指标结论不完整"
                )
            target_reports[project_target.target_metric] = {
                "description": concept.description,
                "direction": concept.direction,
                "period": project_target.period,
                "canonical_unit": concept.canonical_unit,
                "canonical_boundary": concept.canonical_boundary,
                "coverage": per_target_coverage,
                "ranking": ranking,
                "not_ranked": not_ranked,
                "cells": cells,
            }

        total_cells = len(project.regions) * len(project.targets)
        comparable_cells = sum(
            1
            for cell in cell_index.values()
            if cell["evidence"] in COMPARABLE_CLASSES
        )
        coverage_pct = (
            Decimal(comparable_cells * 100) / Decimal(total_cells)
            if total_cells
            else Decimal(0)
        )
        coverage = {
            "project_id": project.project_id,
            "comparable_cells": comparable_cells,
            "total_cells": total_cells,
            "pct": percent_text(comparable_cells, total_cells),
            "threshold": decimal_text(project.coverage_threshold),
            "min_source_coverage": decimal_text(project.min_source_coverage),
            "sufficient": coverage_pct >= project.coverage_threshold,
            "regions_comparable": sorted(
                {
                    region
                    for (target_name, region), cell in cell_index.items()
                    if cell["evidence"] in COMPARABLE_CLASSES
                }
            ),
        }

        overall = self._overall(project, target_reports, coverage)
        excluded_cells = [
            {"target_metric": name, "region": region}
            for (name, region), cell in cell_index.items()
            if cell["evidence"] == EXCLUDED
        ]
        if excluded_cells:
            coverage["excluded_cells"] = excluded_cells
        result = {
            "project_id": project.project_id,
            "project_title": project.title,
            "generated_at": now_iso(),
            "project_revision": project.revision(),
            "regions": list(project.regions),
            "coverage": coverage,
            "targets": target_reports,
            "overall": overall,
            "caveats": caveats,
            "project_notes": project.notes,
        }
        return result

    # -- internals ---------------------------------------------------------

    def _target_concept(self, target_metric: str) -> Target | None:
        # A comparison target must be registered with its canonical unit and
        # boundary; otherwise "comparable" would be an unchecked assumption.
        return self.store.target(target_metric)

    def _cell(
        self,
        project: Project,
        project_target: ProjectTarget,
        concept: Target,
        region: str,
    ) -> dict[str, Any]:
        period = project_target.period
        reasons: list[str] = []

        proposals = [
            item
            for item in self.store.proposals_for_target(project_target.target_metric)
            if not item.region or item.region == region
        ]
        pin = project_target.sources.get(region)
        mapping_pin = project_target.mappings.get(region)
        unselected: list[MappingProposal] = []
        if pin:
            kept = [item for item in proposals if item.source_metric == pin]
            unselected = [item for item in proposals if item not in kept]
            proposals = kept
            if not proposals:
                reasons.append(
                    f"项目指定来源指标 {pin}，但没有登记到目标 "
                    f"{project_target.target_metric} 的映射"
                )
        if mapping_pin:
            kept = [item for item in proposals if item.mapping_id == mapping_pin]
            unselected.extend(item for item in proposals if item not in kept)
            proposals = kept
            if not proposals:
                reasons.append(
                    f"项目指定映射 {mapping_pin}，但没有在目标 "
                    f"{project_target.target_metric} 下登记该映射"
                )

        candidates: list[dict[str, Any]] = []
        for proposal in proposals:
            definitions = self.store.definitions_for(proposal.source_metric, region)
            candidate: dict[str, Any] = {
                "mapping_id": proposal.mapping_id,
                "prop_rev": self.store.current_proposal_rev(proposal.mapping_id),
                "source_metric": proposal.source_metric,
                "region": proposal.region,
                "transform": proposal.transform,
                "proposed_by": proposal.proposed_by,
                "shared_result_id": proposal.shared_result_id,
                "target_unit": proposal.target_unit,
                "conversion_factor": decimal_text(proposal.conversion_factor),
                "rationale": proposal.rationale,
                "limitations": proposal.limitations,
                "source_definition_version": proposal.source_definition_version,
                "reviews": [],
                "definition": None,
                "observation": None,
                "evidence": EXCLUDED,
                "reasons": [],
            }
            if not definitions:
                candidate["reasons"].append("该地区没有登记该来源指标，映射不适用")
                candidates.append(candidate)
                continue

            if proposal.source_definition_version:
                pinned = [
                    item
                    for item in definitions
                    if item.definition_version == proposal.source_definition_version
                ]
                if not pinned:
                    candidate["reasons"].append(
                        f"映射指定定义版本 {proposal.source_definition_version}，"
                        "但该地区未登记此版本"
                    )
                    candidates.append(candidate)
                    continue
                definition = pinned[-1]
                def_reasons = []
                if not valid_for_period(definition.valid_from, definition.valid_to, period):
                    def_reasons.append(
                        f"映射指定的定义版本 {definition.definition_version} 不覆盖期间 {period}"
                    )
                    definition = None
            else:
                definition, def_reasons = _latest_definition(definitions, period)
            candidate["reasons"].extend(def_reasons)
            if definition is None:
                candidates.append(candidate)
                continue
            candidate["definition_versions"] = [
                {
                    "definition_version": item.definition_version,
                    "valid_from": item.valid_from,
                    "valid_to": item.valid_to,
                    "unit": item.unit,
                    "boundary": item.boundary,
                    "selected": item.record_id == definition.record_id,
                }
                for item in definitions
            ]
            candidate["definition"] = {
                "metric_id": definition.metric_id,
                "region": definition.region,
                "definition": definition.definition,
                "definition_version": definition.definition_version,
                "unit": definition.unit,
                "boundary": definition.boundary,
                "frequency": definition.frequency,
                "valid_from": definition.valid_from,
                "valid_to": definition.valid_to,
                "source_institution": definition.source_institution,
                "notes": definition.notes,
            }

            status, latest_review, stale = self.store.effective_review(
                proposal.mapping_id, region
            )
            reviews = [_compact_review(item) for item in self.store.reviews(proposal.mapping_id)]
            candidate["reviews"] = reviews
            candidate["review_status"] = status
            if stale:
                candidate["reasons"].append(
                    "最新审核针对旧版映射提案，不适用于当前修订；需重新确认"
                )
            if latest_review is not None and not stale:
                candidate["review_id"] = latest_review.review_id

            evidence, class_reasons, factor = _classify(
                proposal, definition, concept, review_status=status
            )
            candidate["evidence"] = evidence
            candidate["reasons"].extend(class_reasons)

            if definition.frequency and not models.period_matches_frequency(
                period, definition.frequency
            ):
                candidate["reasons"].append(
                    f"项目期间 {period} 与来源指标频率 {definition.frequency} 不匹配；"
                    "不能用期间汇总值代替原始频率观测"
                )
                candidate["evidence"] = SIDE_BY_SIDE
                candidates.append(candidate)
                continue
            observation, versions, obs_reasons = _select_observation(
                self.store,
                proposal.source_metric,
                region,
                period,
                definition_version=definition.definition_version,
            )
            candidate["source_versions"] = [
                {
                    "source_version": item.source_version,
                    "value": decimal_text(item.value),
                    "coverage_pct": decimal_text(item.coverage_pct),
                    "definition_version": item.definition_version,
                    "selected": observation is not None and item is observation,
                }
                for item in versions
            ]
            candidate["reasons"].extend(obs_reasons)
            if observation is None:
                # A rejected mapping stays rejected even if data is missing;
                # otherwise the report would imply the only problem was data.
                if candidate["evidence"] != EXCLUDED:
                    candidate["evidence"] = MISSING
            else:
                candidate["observation"] = {
                    "period": observation.period,
                    "source_version": observation.source_version,
                    "value": decimal_text(observation.value),
                    "coverage_pct": decimal_text(observation.coverage_pct),
                    "definition_version": observation.definition_version,
                    "source_institution": observation.source_institution
                    or definition.source_institution,
                    "observed_at": observation.observed_at,
                }
                if observation.coverage_pct < project.min_source_coverage:
                    if evidence in COMPARABLE_CLASSES:
                        candidate["evidence"] = SIDE_BY_SIDE
                    candidate["reasons"].append(
                        f"来源覆盖率 {decimal_text(observation.coverage_pct)}% 低于最低门槛 "
                        f"{decimal_text(project.min_source_coverage)}%，仅并列呈现"
                    )
                # Display values are produced for comparable cells and for
                # side-by-side / discussion-only cells; rejected or missing
                # cells keep the raw observation in the audit trail only.
                if candidate["evidence"] in (DIRECT, CONVERTED, SIDE_BY_SIDE, DISCUSSION_ONLY):
                    value = observation.value
                    unit = definition.unit
                    boundary = definition.boundary
                    if candidate["evidence"] == CONVERTED:
                        if factor is None:
                            candidate["evidence"] = SIDE_BY_SIDE
                            candidate["reasons"].append("缺少换算系数，无法计算目标值")
                        else:
                            value = value * factor
                            unit = concept.canonical_unit
                            boundary = concept.canonical_boundary
                    candidate["value"] = decimal_text(value)
                    candidate["unit"] = unit
                    candidate["boundary"] = boundary
            candidates.append(candidate)

        chosen, alternatives = self._choose(candidates)
        if chosen is None:
            if not proposals:
                reasons.append("没有可用于该目标指标的已登记映射")
            else:
                reasons.append("该地区没有公认可用的映射")
            for item in candidates:
                for reason in item.get("reasons", []):
                    if reason not in reasons:
                        reasons.append(reason)
            return {
                "region": region,
                "target_metric": project_target.target_metric,
                "period": period,
                "evidence": MISSING,
                "evidence_label": EVIDENCE_LABELS[MISSING],
                "scored": False,
                "value": None,
                "unit": None,
                "boundary": None,
                "definition": None,
                "observation": None,
                "mapping": None,
                "reviews": [],
                "reasons": reasons,
                "alternatives": [self._summary(item) for item in candidates],
                "trace": [],
            }

        cell = {
            "region": region,
            "target_metric": project_target.target_metric,
            "period": period,
            "evidence": chosen["evidence"],
            "evidence_label": EVIDENCE_LABELS[chosen["evidence"]],
            "scored": chosen["evidence"] in COMPARABLE_CLASSES,
            "value": chosen.get("value"),
            "unit": chosen.get("unit"),
            "boundary": chosen.get("boundary"),
            "definition": chosen.get("definition"),
            "observation": chosen.get("observation"),
            "mapping": {
                key: chosen.get(key)
                for key in (
                    "mapping_id",
                    "prop_rev",
                    "source_metric",
                    "transform",
                    "proposed_by",
                    "shared_result_id",
                    "target_unit",
                    "conversion_factor",
                    "rationale",
                    "limitations",
                    "source_definition_version",
                    "review_status",
                )
            },
            "reviews": chosen.get("reviews", []),
            "reasons": list(
                dict.fromkeys(
                    chosen.get("reasons", [])
                    + reasons
                    + (
                        [
                            "该地区存在其他候选映射，已按证据等级选择 "
                            f"{chosen['mapping_id']}；其余候选见 alternatives"
                        ]
                        if any(item is not chosen for item in candidates)
                        else []
                    )
                )
            ),
            "alternatives": [self._summary(item) for item in candidates if item is not chosen]
            + [
                {
                    "mapping_id": item.mapping_id,
                    "source_metric": item.source_metric,
                    "transform": item.transform,
                    "evidence": EXCLUDED,
                    "review_status": "not_selected",
                    "value": None,
                    "reasons": ["项目已固定采用其他映射或来源指标，未参与本次选择"],
                }
                for item in unselected
            ],
            "trace": [
                {
                    "kind": "metric_definition",
                    "record": chosen.get("definition"),
                    "versions": chosen.get("definition_versions", []),
                },
                {
                    "kind": "observation",
                    "record": chosen.get("observation"),
                    "source_versions": chosen.get("source_versions", []),
                },
                {
                    "kind": "mapping",
                    "record": {
                        "mapping_id": chosen.get("mapping_id"),
                        "proposal_rev": chosen.get("prop_rev"),
                        "transform": chosen.get("transform"),
                        "conversion_factor": chosen.get("conversion_factor"),
                        "rationale": chosen.get("rationale"),
                        "limitations": chosen.get("limitations"),
                    },
                },
                {
                    "kind": "reviews",
                    "records": chosen.get("reviews", []),
                },
                {
                    "kind": "classification",
                    "evidence": chosen["evidence"],
                    "reasons": chosen.get("reasons", []),
                },
            ],
        }
        return cell

    @staticmethod
    def _summary(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "mapping_id": candidate["mapping_id"],
            "source_metric": candidate["source_metric"],
            "transform": candidate["transform"],
            "evidence": candidate["evidence"],
            "review_status": candidate.get("review_status", "pending"),
            "value": candidate.get("value"),
            "reasons": list(dict.fromkeys(candidate.get("reasons", []))),
        }

    @staticmethod
    def _choose(
        candidates: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if not candidates:
            return None, []
        # The best evidence wins; rejected mappings are reported explicitly
        # rather than being mislabelled as missing data.
        ordered = sorted(
            candidates,
            key=lambda item: (
                _EVIDENCE_PRIORITY[item["evidence"]],
                0 if item.get("region") else 1,
                item["mapping_id"],
            ),
        )
        chosen = ordered[0]
        return chosen, [item for item in candidates if item is not chosen]

    @staticmethod
    def _rank(cells: list[dict[str, Any]], direction: str) -> list[dict[str, Any]]:
        if not cells:
            return []
        sign = -1 if direction == "higher_is_better" else 1
        ordered = sorted(
            cells,
            key=lambda cell: (sign * Decimal(cell["value"]), cell["region"]),
        )
        ranking: list[dict[str, Any]] = []
        previous_value: Decimal | None = None
        previous_rank = 0
        group: list[dict[str, Any]] = []
        for position, cell in enumerate(ordered, start=1):
            value = Decimal(cell["value"])
            if previous_value is not None and value == previous_value:
                rank = previous_rank
            else:
                rank = position
                previous_rank = position
            previous_value = value
            entry = {
                "rank": rank,
                "tied": False,
                "region": cell["region"],
                "value": cell["value"],
                "unit": cell["unit"],
                "evidence": cell["evidence"],
                "evidence_label": cell["evidence_label"],
                "mapping_id": cell["mapping"]["mapping_id"],
            }
            ranking.append(entry)
            group.append(entry)
            if position == len(ordered) or Decimal(ordered[position]["value"]) != value:
                if len(group) > 1:
                    for member in group:
                        member["tied"] = True
                group = []
        if len(ranking) == 1:
            ranking[0]["note"] = "只有一个地区可比，不构成排序"
        return ranking

    def _overall(
        self,
        project: Project,
        target_reports: dict[str, Any],
        coverage: dict[str, Any],
    ) -> dict[str, Any]:
        target_names = list(target_reports)
        complete_regions: list[str] = []
        for region in project.regions:
            cells = [target_reports[name]["cells"][region] for name in target_names]
            if all(cell["evidence"] in COMPARABLE_CLASSES for cell in cells):
                complete_regions.append(region)

        every_target_has_two = all(
            len(report["ranking"]) >= 2 for report in target_reports.values()
        )
        if (
            not coverage["sufficient"]
            or len(complete_regions) < 2
            or not every_target_has_two
        ):
            return {
                "status": "insufficient_coverage",
                "ranking": [],
                "coverage": coverage,
                "note": (
                    "可比覆盖不足或存在地区缺项，系统不输出综合排名。"
                    "请查看各指标的局部比较与覆盖数字；补齐资料或确认映射后重新生成。"
                ),
            }

        # Rank-sum among regions that are comparable in every target.  A
        # missing target disqualifies a region from the composite instead of
        # being imputed.
        rank_sums: dict[str, int] = {region: 0 for region in complete_regions}
        for name, report in target_reports.items():
            ranking = report["ranking"]
            for entry in ranking:
                if entry["region"] in rank_sums:
                    rank_sums[entry["region"]] += entry["rank"]
        ordered = sorted(rank_sums.items(), key=lambda item: (item[1], item[0]))
        ranking = []
        previous_total: int | None = None
        previous_rank = 0
        group: list[dict[str, Any]] = []
        for position, (region, total) in enumerate(ordered, start=1):
            if previous_total is not None and total == previous_total:
                rank = previous_rank
            else:
                rank = position
                previous_rank = position
                previous_total = total
            entry = {
                "rank": rank,
                "tied": False,
                "region": region,
                "rank_sum": total,
                "targets_compared": len(target_names),
            }
            ranking.append(entry)
            group.append(entry)
            if position == len(ordered) or ordered[position][1] != total:
                if len(group) > 1:
                    for member in group:
                        member["tied"] = True
                group = []
        all_regions_complete = len(complete_regions) == len(project.regions)
        mixed_evidence = any(
            entry["evidence"] == CONVERTED
            for report in target_reports.values()
            for entry in report["ranking"]
        )
        return {
            "status": "ranked",
            "ranking": ranking,
            "coverage": coverage,
            "note": (
                "综合排序仅使用在所有目标指标上均可比的地区；缺项地区不插补。"
                + (
                    " 部分单元经过有依据的单位换算，换算依据见逐项追踪。"
                    if mixed_evidence
                    else ""
                )
                if all_regions_complete
                else "部分地区存在缺项，已从综合排序中排除；覆盖变化见 coverage。"
            ),
        }


def result_markdown(result: dict[str, Any]) -> str:
    """Render a partner-facing review packet.

    The packet separates comparable conclusions from discussion-only material
    and always prints coverage next to any ordering.
    """
    lines: list[str] = []
    lines.append(f"# {result.get('project_title') or result['project_id']}")
    lines.append("")
    lines.append(f"- 项目编号：`{result['project_id']}`")
    lines.append(f"- 生成时间：{result['generated_at']}")
    lines.append(f"- 地区：{', '.join(result['regions'])}")
    coverage = result["coverage"]
    lines.append(
        f"- 可比覆盖：{coverage['comparable_cells']}/{coverage['total_cells']} "
        f"（{coverage['pct']}%，门槛 {coverage['threshold']}%）"
    )
    if result.get("project_notes"):
        lines.append(f"- 项目说明：{result['project_notes']}")
    lines.append("")
    lines.append("## 总体结论")
    lines.append("")
    overall = result["overall"]
    if overall["status"] == "ranked":
        lines.append(overall["note"])
        lines.append("")
        lines.append("| 排名 | 地区 | 名次和 | 覆盖指标数 |")
        lines.append("| --- | --- | --- | --- |")
        for item in overall["ranking"]:
            lines.append(
                f"| {item['rank']} | {item['region']} | {item['rank_sum']} | "
                f"{item['targets_compared']} |"
            )
    else:
        lines.append(f"**不输出综合排名。** {overall['note']}")
    lines.append("")

    for name, report in result["targets"].items():
        lines.append(f"## {name}（{report['period']}）")
        lines.append("")
        if report.get("description"):
            lines.append(report["description"])
            lines.append("")
        tcoverage = report["coverage"]
        lines.append(
            f"可比覆盖：{tcoverage['comparable_regions']}/{tcoverage['total_regions']} "
            f"（{tcoverage['pct']}%）；方向：{report['direction']}"
        )
        lines.append("")
        if len(report["ranking"]) >= 2:
            lines.append("| 排名 | 地区 | 值 | 单位 | 证据等级 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in report["ranking"]:
                rank = "并列" if item.get("tied") else item["rank"]
                lines.append(
                    f"| {rank} | {item['region']} | {item['value']} | "
                    f"{item['unit'] or ''} | {item['evidence_label']} |"
                )
        elif report["ranking"]:
            item = report["ranking"][0]
            lines.append(
                f"仅一个地区可比（{item['region']}：{item['value']} "
                f"{item['unit'] or ''}，{item['evidence_label']}），**不构成排序**。"
            )
            lines.append("")
        else:
            lines.append("_没有足够的可比观测形成排序。_")
        lines.append("")
        side_by_side = [
            cell
            for cell in report["cells"].values()
            if cell["evidence"] in (SIDE_BY_SIDE, DISCUSSION_ONLY)
        ]
        if side_by_side:
            heading = (
                "并列呈现（不得与原排名合并）"
                if any(cell["evidence"] == SIDE_BY_SIDE for cell in side_by_side)
                else "仅供讨论的估算"
            )
            lines.append(f"### {heading}")
            lines.append("")
            lines.append("| 地区 | 值 | 单位 | 统计边界 | 证据等级 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for cell in side_by_side:
                lines.append(
                    f"| {cell['region']} | {cell['value']} | {cell['unit'] or ''} | "
                    f"{cell['boundary'] or ''} | {cell['evidence_label']} | "
                    f"{'；'.join(cell['reasons'])} |"
                )
            lines.append("")
        if report["not_ranked"]:
            lines.append("未纳入比较（映射被拒或资料不足）：")
            lines.append("")
            for item in report["not_ranked"]:
                lines.append(f"- **{item['region']}**（{item['evidence_label']}）：{item['reason']}")
            lines.append("")
        lines.append("### 逐项追踪")
        lines.append("")
        for region, cell in report["cells"].items():
            mapping = cell["mapping"] or {}
            definition = cell["definition"] or {}
            observation = cell["observation"] or {}
            lines.append(f"#### {region} — {cell['evidence_label']}")
            lines.append("")
            lines.append(
                f"- 来源指标：`{mapping.get('source_metric', '—')}` "
                f"定义「{definition.get('definition', '—')}」"
                f"版本 `{definition.get('definition_version', '—')}`"
            )
            lines.append(
                f"- 来源机构：{definition.get('source_institution') or '未注明'}；"
                f"单位 {definition.get('unit') or '—'}；边界 {definition.get('boundary') or '—'}"
            )
            lines.append(
                f"- 观测：{observation.get('value', '—')} "
                f"（期间 {observation.get('period', '—')}，"
                f"来源版本 {observation.get('source_version', '—')}，"
                f"覆盖率 {observation.get('coverage_pct', '—')}%）"
            )
            lines.append(
                f"- 映射：`{mapping.get('mapping_id', '—')}` / {mapping.get('transform', '—')}"
                f"；审核状态 {mapping.get('review_status', '—')}"
            )
            if mapping.get("rationale"):
                lines.append(f"- 转换依据：{mapping['rationale']}")
            if mapping.get("limitations"):
                lines.append(f"- 限制：{mapping['limitations']}")
            if cell["reasons"]:
                lines.append("- 说明：" + "；".join(cell["reasons"]))
            if cell["alternatives"]:
                lines.append("- 其他候选映射：")
                for alt in cell["alternatives"]:
                    lines.append(
                        f"  - `{alt['mapping_id']}`（{alt['transform']}，"
                        f"{EVIDENCE_LABELS[alt['evidence']]}，审核 {alt['review_status']}）"
                    )
            lines.append("")
    if result["caveats"]:
        lines.append("## 注意事项")
        lines.append("")
        for caveat in result["caveats"]:
            lines.append(f"- {caveat}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Freeze the parts of a result that must not move when sources revise."""
    cells: dict[str, Any] = {}
    for name, report in result["targets"].items():
        for region, cell in report["cells"].items():
            cells[f"{name}|{region}"] = snapshot_cell(cell)
    targets = {
        name: {
            "coverage": report["coverage"],
            "ranking": _ranking_view(report["ranking"]),
        }
        for name, report in result["targets"].items()
    }
    return {
        "project_id": result["project_id"],
        "project_title": result["project_title"],
        "project_revision": result["project_revision"],
        "generated_at": result["generated_at"],
        "coverage": result["coverage"],
        "overall": result["overall"],
        "targets": targets,
        "cells": cells,
        "project_notes": result.get("project_notes", ""),
        "caveats": result["caveats"],
    }


def impact_report(
    shared: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Compare a frozen share against the current comparison.

    The frozen values are reported as ``shared_*`` fields and are never
    overwritten; the current values sit next to them so a partner can see what
    a local definition or revision would change.
    """
    affected: list[dict[str, Any]] = []
    old_cells = shared.get("cells", {})
    new_cells = {
        f"{name}|{region}": snapshot_cell(cell)
        for name, report in current["targets"].items()
        for region, cell in report["cells"].items()
    }
    for key in sorted(set(old_cells) | set(new_cells)):
        old = old_cells.get(key)
        new = new_cells.get(key)
        if old is None:
            affected.append(
                {"cell": key, "change": "new_cell", "current": _impact_cell(new)}
            )
            continue
        if new is None:
            affected.append(
                {"cell": key, "change": "removed_cell", "shared": _impact_cell(old)}
            )
            continue
        fields = (
            "evidence",
            "value",
            "unit",
            "definition_version",
            "source_version",
            "mapping_id",
            "proposal_rev",
            "review_status",
        )
        changes = {
            field: {"shared": old.get(field), "current": new.get(field)}
            for field in fields
            if old.get(field) != new.get(field)
        }
        if changes:
            affected.append(
                {
                    "cell": key,
                    "change": "changed",
                    "fields": changes,
                    "shared": _impact_cell(old),
                    "current": _impact_cell(new),
                }
            )
    coverage_changed = shared.get("coverage", {}).get("pct") != current["coverage"]["pct"]
    status_changed = shared.get("overall", {}).get("status") != current["overall"]["status"]
    shared_ranking = _ranking_view(shared.get("overall", {}).get("ranking", []))
    current_ranking = _ranking_view(current["overall"].get("ranking", []))
    shared_targets = shared.get("targets", {})
    current_targets = {
        name: {
            "coverage": report["coverage"],
            "ranking": _ranking_view(report["ranking"]),
        }
        for name, report in current["targets"].items()
    }
    target_changes: dict[str, Any] = {}
    for name in sorted(set(shared_targets) | set(current_targets)):
        old = shared_targets.get(name) or {}
        new = current_targets.get(name) or {}
        old_pct = (old.get("coverage") or {}).get("pct")
        new_pct = (new.get("coverage") or {}).get("pct")
        old_rank = old.get("ranking", [])
        new_rank = new.get("ranking", [])
        if old_pct != new_pct or old_rank != new_rank:
            target_changes[name] = {
                "coverage": {"shared": old_pct, "current": new_pct},
                "ranking": {"shared": old_rank, "current": new_rank},
            }
    return {
        "project_id": shared.get("project_id"),
        "shared_result_id": shared.get("shared_result_id"),
        "shared_at": shared.get("shared_at"),
        "shared_revision": shared.get("project_revision"),
        "current_revision": current["project_revision"],
        "coverage_change": {
            "shared": shared.get("coverage", {}).get("pct"),
            "current": current["coverage"]["pct"],
            "changed": coverage_changed,
        },
        "conclusion_status_change": {
            "shared": shared.get("overall", {}).get("status"),
            "current": current["overall"]["status"],
            "changed": status_changed,
        },
        "overall_ranking_change": {
            "shared": shared_ranking,
            "current": current_ranking,
            "changed": shared_ranking != current_ranking,
        },
        "affected_targets": target_changes,
        "affected_cells": affected,
        "projects_affected": (
            [shared.get("project_id")] if affected else []
        ),
        "project_notes": {
            "shared": shared.get("project_notes", ""),
            "current": current.get("project_notes", ""),
            "changed": shared.get("project_notes", "") != current.get("project_notes", ""),
        },
        "caveats": {
            "shared": list(shared.get("caveats", [])),
            "current": list(current.get("caveats", [])),
            "added": sorted(set(current.get("caveats", [])) - set(shared.get("caveats", []))),
            "removed": sorted(set(shared.get("caveats", [])) - set(current.get("caveats", []))),
        },
    }


def snapshot_cell(cell: dict[str, Any]) -> dict[str, Any]:
    """Compact, comparable projection of one result cell."""
    mapping = cell.get("mapping") or {}
    return {
        "evidence": cell.get("evidence"),
        "value": cell.get("value"),
        "unit": cell.get("unit"),
        "definition_version": (cell.get("definition") or {}).get("definition_version"),
        "source_version": (cell.get("observation") or {}).get("source_version"),
        "mapping_id": mapping.get("mapping_id"),
        "proposal_rev": mapping.get("prop_rev"),
        "review_status": mapping.get("review_status"),
        "reasons": cell.get("reasons", []),
    }


def evidence_label(evidence: str | None) -> str:
    return EVIDENCE_LABELS.get(evidence or "", evidence or "")


def conclusion_label(status: str | None) -> str:
    return {
        "ranked": "可以给出综合排序",
        "insufficient_coverage": "不输出综合排名（覆盖不足或存在缺项）",
    }.get(status or "", status or "")


def _ranking_view(ranking: Any) -> list[dict[str, Any]]:
    return [
        {
            "rank": item.get("rank"),
            "region": item.get("region"),
            "rank_sum": item.get("rank_sum"),
        }
        for item in ranking or []
    ]


def _impact_cell(cell: dict[str, Any] | None) -> dict[str, Any] | None:
    if cell is None:
        return None
    keys = (
        "evidence",
        "value",
        "unit",
        "definition_version",
        "source_version",
        "mapping_id",
        "proposal_rev",
        "review_status",
        "reasons",
    )
    return {key: cell.get(key) for key in keys}


__all__ = [
    "Comparator",
    "result_markdown",
    "snapshot",
    "snapshot_cell",
    "impact_report",
    "evidence_label",
    "conclusion_label",
]
