"""Core records for the green-transition comparison workbench.

Everything here is deliberately small and value-oriented so that the CSV
fixtures, the on-disk store and the comparison reports all speak the same
vocabulary.  Records are immutable: revisions are represented as new records
with a new version identifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------


class DataError(ValueError):
    """A record is malformed or internally inconsistent."""


class ConflictError(RuntimeError):
    """Two different records tried to claim the same version key."""


TRANSFORMS = ("identity", "unit_only", "scope_adjustment", "estimate")
REVIEW_STATUSES = ("accepted", "rejected", "pending")
DIRECTIONS = ("higher_is_better", "lower_is_better")

# Evidence classes.  These are deliberately explicit so that a ranking can
# never silently absorb a side-by-side value or a discussion-only estimate.
DIRECT = "comparable_direct"
CONVERTED = "comparable_converted"
SIDE_BY_SIDE = "side_by_side"
DISCUSSION_ONLY = "discussion_only"
EXCLUDED = "excluded"
MISSING = "missing"

COMPARABLE_CLASSES = (DIRECT, CONVERTED)

EVIDENCE_LABELS = {
    DIRECT: "可直接比较",
    CONVERTED: "有依据换算后可比",
    SIDE_BY_SIDE: "只能并列呈现",
    DISCUSSION_ONLY: "仅供讨论的估算",
    EXCLUDED: "未纳入比较",
    MISSING: "缺少观测",
}


def now_iso() -> str:
    """UTC timestamp with second precision, used for audit entries."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_decimal(
    value: Any,
    *,
    field_name: str,
    allow_blank: bool = False,
) -> Decimal | None:
    raw = text(value)
    if raw == "":
        if allow_blank:
            return None
        raise DataError(f"{field_name} 不能为空")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise DataError(f"{field_name} 不是有效数字: {raw!r}") from exc
    if not number.is_finite():
        raise DataError(f"{field_name} 必须是有限数字: {raw!r}")
    return number


def percent_text(numerator: int, denominator: int) -> str:
    """Stable two-decimal percentage without binary float noise."""
    if denominator == 0:
        return "0.00"
    value = (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return format(value, "f")


def decimal_text(value: Decimal | None) -> str | None:
    """Stable JSON/CSV representation without binary-float noise."""
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in ("", "-0"):
        return "0"
    return rendered


def version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """Order version labels deterministically.

    ``v2`` < ``v10``; non-numeric labels fall back to case-folded text.  The
    tuple always has the same shape so mixed labels never raise TypeError.
    """
    raw = text(version)
    if not raw:
        return ((-1, ""),)
    parts = re.findall(r"\d+|[^\d]+", raw)
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in parts)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(canonical_json(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


def to_primitive(value: Any) -> Any:
    """Recursively convert dataclasses / Decimals into JSON-safe values."""
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_primitive(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

_ANNUAL = re.compile(r"^\d{4}$")
_MONTHLY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_QUARTERLY = re.compile(r"^\d{4}-Q[1-4]$")
_DAILY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def period_kind(period: str) -> str | None:
    raw = text(period)
    if _ANNUAL.fullmatch(raw):
        return "annual"
    if _MONTHLY.fullmatch(raw):
        return "monthly"
    if _QUARTERLY.fullmatch(raw):
        return "quarterly"
    if _DAILY.fullmatch(raw):
        return "daily"
    return None


def period_matches_frequency(period: str, frequency: str) -> bool:
    kind = period_kind(period)
    freq = text(frequency).casefold()
    if kind is None:
        return False
    if freq in ("annual", "yearly", "年"):
        return kind == "annual"
    if freq in ("monthly", "month", "月"):
        return kind == "monthly"
    if freq in ("quarterly", "quarter", "季"):
        return kind == "quarterly"
    if freq in ("daily", "day", "日"):
        return kind == "daily"
    # Unknown frequency labels are not used to reject a period.
    return True


def _period_bounds(period: str) -> tuple[str, str] | None:
    kind = period_kind(period)
    raw = text(period)
    if kind == "annual":
        return f"{raw}-01-01", f"{raw}-12-31"
    if kind == "monthly":
        return f"{raw}-01", f"{raw}-31"
    if kind == "daily":
        return raw, raw
    if kind == "quarterly":
        year, quarter = raw.split("-Q")
        starts = {"1": "01-01", "2": "04-01", "3": "07-01", "4": "10-01"}
        ends = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
        return f"{year}-{starts[quarter]}", f"{year}-{ends[quarter]}"
    return None


def valid_for_period(valid_from: str, valid_to: str, period: str) -> bool:
    bounds = _period_bounds(period)
    if bounds is None:
        return False
    period_start, period_end = bounds
    start = text(valid_from)
    end = text(valid_to)
    if start and start > period_end:
        return False
    if end and end < period_start:
        return False
    return True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A project-level comparison concept, not a regional source metric."""

    target_metric: str
    description: str = ""
    canonical_unit: str = ""
    canonical_boundary: str = ""
    direction: str = "higher_is_better"
    goal: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Target":
        target_metric = text(row.get("target_metric"))
        if not target_metric:
            raise DataError("target_metric 不能为空")
        unit = text(row.get("canonical_unit"))
        boundary = text(row.get("canonical_boundary"))
        if not unit or not boundary:
            raise DataError(
                "目标指标必须声明 canonical_unit 与 canonical_boundary；"
                "否则无法判断可比性"
            )
        direction = text(row.get("direction")) or "higher_is_better"
        if direction not in DIRECTIONS:
            raise DataError(f"未知 direction: {direction!r}")
        return cls(
            target_metric=target_metric,
            description=text(row.get("description")),
            canonical_unit=unit,
            canonical_boundary=boundary,
            direction=direction,
            goal=text(row.get("goal")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Target":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    region: str
    definition: str
    unit: str
    boundary: str
    frequency: str
    valid_from: str
    valid_to: str = ""
    source_institution: str = ""
    definition_version: str = "v1"
    notes: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.metric_id, self.region, self.definition_version)

    @property
    def record_id(self) -> str:
        return "|".join(self.key)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MetricDefinition":
        required = ("metric_id", "region", "definition", "unit", "boundary", "frequency", "valid_from")
        missing = [name for name in required if not text(row.get(name))]
        if missing:
            raise DataError("指标定义缺少字段: " + ", ".join(missing))
        valid_from = text(row.get("valid_from"))
        valid_to = text(row.get("valid_to"))
        for field_name, value in (("valid_from", valid_from), ("valid_to", valid_to)):
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise DataError(f"{field_name} 必须是 YYYY-MM-DD 格式: {value!r}")
        if valid_to and valid_to < valid_from:
            raise DataError("valid_to 不能早于 valid_from")
        return cls(
            metric_id=text(row.get("metric_id")),
            region=text(row.get("region")),
            definition=text(row.get("definition")),
            unit=text(row.get("unit")),
            boundary=text(row.get("boundary")),
            frequency=text(row.get("frequency")),
            valid_from=valid_from,
            valid_to=valid_to,
            source_institution=text(row.get("source_institution")),
            definition_version=text(row.get("definition_version")) or "v1",
            notes=text(row.get("notes")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MetricDefinition":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass(frozen=True)
class Observation:
    metric_id: str
    region: str
    period: str
    value: Decimal
    coverage_pct: Decimal = Decimal("100")
    source_version: str = "v1"
    source_institution: str = ""
    observed_at: str = ""
    definition_version: str = ""

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.metric_id,
            self.region,
            self.period,
            self.source_version,
            self.definition_version,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Observation":
        metric_id = text(row.get("metric_id"))
        region = text(row.get("region"))
        period = text(row.get("period"))
        if not metric_id or not region or not period:
            raise DataError("观测记录必须包含 metric_id、region、period")
        if period_kind(period) is None:
            raise DataError(f"period 格式无法识别: {period!r}")
        value = parse_decimal(row.get("value"), field_name="value")
        coverage = parse_decimal(
            row.get("coverage_pct") or "100", field_name="coverage_pct"
        )
        if coverage is None or coverage < 0 or coverage > 100:
            raise DataError("coverage_pct 必须在 0 到 100 之间")
        return cls(
            metric_id=metric_id,
            region=region,
            period=period,
            value=value,
            coverage_pct=coverage,
            source_version=text(row.get("source_version")) or "v1",
            source_institution=text(row.get("source_institution")),
            observed_at=text(row.get("observed_at")),
            definition_version=text(row.get("definition_version")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Observation":
        payload = dict(data)
        payload["value"] = parse_decimal(payload["value"], field_name="value")
        payload["coverage_pct"] = parse_decimal(
            payload.get("coverage_pct", "100"), field_name="coverage_pct"
        )
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass(frozen=True)
class MappingProposal:
    """A proposal to map a regional source metric onto a target concept.

    The review status is deliberately *not* stored here.  Decisions live in
    append-only :class:`ReviewRecord` rows so that partner opinions and their
    history remain visible even after a later decision.
    """

    mapping_id: str
    source_metric: str
    target_metric: str
    transform: str
    proposed_by: str = ""
    region: str = ""
    shared_result_id: str = ""
    target_unit: str = ""
    conversion_factor: Decimal | None = None
    rationale: str = ""
    limitations: str = ""
    source_definition_version: str = ""

    @property
    def key(self) -> str:
        return self.mapping_id

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MappingProposal":
        required = ("mapping_id", "source_metric", "target_metric", "transform")
        missing = [name for name in required if not text(row.get(name))]
        if missing:
            raise DataError("映射缺少字段: " + ", ".join(missing))
        transform = text(row.get("transform"))
        if transform not in TRANSFORMS:
            raise DataError(f"未知 transform: {transform!r}")
        factor = parse_decimal(
            row.get("conversion_factor"), field_name="conversion_factor", allow_blank=True
        )
        if factor is not None and factor <= 0:
            raise DataError("conversion_factor 必须大于 0")
        return cls(
            mapping_id=text(row.get("mapping_id")),
            source_metric=text(row.get("source_metric")),
            target_metric=text(row.get("target_metric")),
            transform=transform,
            proposed_by=text(row.get("proposed_by")),
            region=text(row.get("region")),
            shared_result_id=text(row.get("shared_result_id")),
            target_unit=text(row.get("target_unit")),
            conversion_factor=factor,
            rationale=text(row.get("rationale")),
            limitations=text(row.get("limitations")),
            source_definition_version=text(row.get("source_definition_version")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MappingProposal":
        payload = dict(data)
        payload["conversion_factor"] = parse_decimal(
            payload.get("conversion_factor"),
            field_name="conversion_factor",
            allow_blank=True,
        )
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass(frozen=True)
class ReviewRecord:
    mapping_id: str
    decision: str
    reviewer_region: str = ""
    reviewed_at: str = ""
    note: str = ""
    source_definition_version: str = ""
    source_version: str = ""
    proposal_rev: str = ""
    recorded_at: str = ""

    @property
    def review_id(self) -> str:
        return "REV-" + record_hash(
            self.mapping_id,
            self.proposal_rev,
            self.decision,
            self.reviewer_region,
            self.reviewed_at,
            self.note,
            self.source_definition_version,
            self.source_version,
        )

    @classmethod
    def from_mapping_row(cls, row: Mapping[str, Any]) -> "ReviewRecord | None":
        """Derive the initial review carried by the combined fixture CSV."""
        decision = text(row.get("review_status")) or "pending"
        if decision not in REVIEW_STATUSES:
            raise DataError(f"未知 review_status: {decision!r}")
        reviewed_at = text(row.get("reviewed_at"))
        note = text(row.get("reviewer_note"))
        # A bare pending row is simply "no decision yet".
        if decision == "pending" and not reviewed_at and not note:
            return None
        return cls(
            mapping_id=text(row.get("mapping_id")),
            decision=decision,
            reviewer_region=text(row.get("reviewer_region")),
            reviewed_at=reviewed_at,
            note=note,
            source_definition_version=text(row.get("source_definition_version")),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewRecord":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass(frozen=True)
class ProjectTarget:
    target_metric: str
    period: str
    # Optional explicit source-metric pins: region -> metric_id.  When a pin is
    # present it is honoured exactly; otherwise the comparator selects the most
    # defensible registered source and lists every alternative.
    sources: dict[str, str] = field(default_factory=dict)
    # Optional explicit mapping adoption: region -> mapping_id.  This records
    # which mapping the analysts chose for the project, even when several
    # accepted mappings would otherwise be eligible.
    mappings: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectTarget":
        target_metric = text(data.get("target_metric"))
        period = text(data.get("period"))
        if not target_metric or not period:
            raise DataError("项目目标必须包含 target_metric 和 period")
        if period_kind(period) is None:
            raise DataError(f"项目 period 格式无法识别: {period!r}")
        sources = {text(k): text(v) for k, v in dict(data.get("sources") or {}).items()}
        mappings = {text(k): text(v) for k, v in dict(data.get("mappings") or {}).items()}
        both = sorted(set(sources) & set(mappings))
        if both:
            raise DataError(
                "同一地区不能同时指定来源指标与映射: " + ", ".join(both)
            )
        return cls(
            target_metric=target_metric,
            period=period,
            sources=sources,
            mappings=mappings,
        )


@dataclass(frozen=True)
class Project:
    project_id: str
    title: str
    regions: tuple[str, ...]
    targets: tuple[ProjectTarget, ...]
    coverage_threshold: Decimal = Decimal("90")
    min_source_coverage: Decimal = Decimal("80")
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    def revision(self) -> str:
        """Hash of the semantic content, excluding bookkeeping timestamps.

        Re-saving an unchanged project must not look like a different project
        to a frozen share, otherwise every impact report would be noisy.
        """
        return record_hash(
            {
                "project_id": self.project_id,
                "title": self.title,
                "regions": sorted(self.regions),
                "targets": [
                    {
                        "target_metric": item.target_metric,
                        "period": item.period,
                        "sources": dict(sorted(item.sources.items())),
                        "mappings": dict(sorted(item.mappings.items())),
                    }
                    for item in sorted(self.targets, key=lambda entry: (entry.target_metric, entry.period))
                ],
                "coverage_threshold": decimal_text(self.coverage_threshold),
                "min_source_coverage": decimal_text(self.min_source_coverage),
                "notes": self.notes,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Project":
        regions = tuple(text(region) for region in data.get("regions", ()) if text(region))
        targets = tuple(ProjectTarget.from_dict(item) for item in data.get("targets", ()))
        if not regions:
            raise DataError("项目至少需要一个地区")
        if not targets:
            raise DataError("项目至少需要一个目标指标")
        threshold = parse_decimal(
            data.get("coverage_threshold", "90"), field_name="coverage_threshold"
        )
        if threshold is None or threshold < 0 or threshold > 100:
            raise DataError("coverage_threshold 必须在 0 到 100 之间")
        minimum = parse_decimal(
            data.get("min_source_coverage", "80"), field_name="min_source_coverage"
        )
        if minimum is None or minimum < 0 or minimum > 100:
            raise DataError("min_source_coverage 必须在 0 到 100 之间")
        metrics = [item.target_metric for item in targets]
        if len(metrics) != len(set(metrics)):
            raise DataError("同一项目内目标指标不能重复；请为不同期间设置不同项目")
        if len(regions) != len(set(regions)):
            raise DataError("项目地区不能重复")
        return cls(
            project_id=text(data.get("project_id")),
            title=text(data.get("title")),
            regions=regions,
            targets=targets,
            coverage_threshold=threshold,
            min_source_coverage=minimum,
            notes=text(data.get("notes")),
            created_at=text(data.get("created_at")),
            updated_at=text(data.get("updated_at")),
        )
