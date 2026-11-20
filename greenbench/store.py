"""Append-only, versioned storage for the comparison workbench.

Design rules encoded here:

* metric definitions are versioned by ``(metric_id, region, definition_version)``;
  changing a definition without bumping the version is rejected;
* observations are versioned by ``source_version``; a revised value must be a
  new version, and the older version stays readable for shares;
* reviews are append-only and are bound to one mapping revision, so a changed
  proposal cannot silently inherit an old approval;
* shares are frozen snapshots and are never rewritten.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import models
from .models import (
    ConflictError,
    DataError,
    MappingProposal,
    MetricDefinition,
    Observation,
    Project,
    ReviewRecord,
    Target,
    record_hash,
    text,
    version_key,
)


SCHEMA = "greenbench-store/v1"


def _now() -> str:
    return models.now_iso()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "schema": SCHEMA,
            "targets": {},
            "metrics": [],
            "observations": [],
            "proposals": [],
            "reviews": [],
            "projects": {},
            "shares": {},
            "log": [],
        }

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Store":
        store = cls(path)
        if store.path.exists():
            with store.path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            if data.get("schema") != SCHEMA:
                raise DataError(
                    f"不支持的存储版本: {data.get('schema')!r}，期望 {SCHEMA!r}"
                )
            store.data = data
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temp_name, self.path)
        except BaseException:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise

    # -- internals ---------------------------------------------------------

    def _log(self, action: str, **details: Any) -> None:
        self.data["log"].append(
            {"at": _now(), "action": action, **models.to_primitive(details)}
        )

    # -- targets -----------------------------------------------------------

    def upsert_target(self, target: Target) -> None:
        if not target.target_metric:
            raise DataError("target_metric 不能为空")
        existing = self.data["targets"].get(target.target_metric)
        payload = models.to_primitive(target)
        if existing is not None and existing != payload:
            raise ConflictError(
                f"目标指标 {target.target_metric} 已存在且内容不同；"
                "请使用新的 target_metric 或先明确修订流程"
            )
        if existing is None:
            self.data["targets"][target.target_metric] = payload
            self._log("target.added", target_metric=target.target_metric)
        else:
            self._log("target.unchanged", target_metric=target.target_metric)

    def target(self, target_metric: str) -> Target | None:
        payload = self.data["targets"].get(target_metric)
        return Target.from_dict(payload) if payload else None

    def targets(self) -> list[Target]:
        return [Target.from_dict(item) for item in self.data["targets"].values()]

    # -- metric definitions ------------------------------------------------

    def add_metric(self, metric: MetricDefinition) -> bool:
        payload = models.to_primitive(metric)
        for existing in self.data["metrics"]:
            if (
                existing["metric_id"] == metric.metric_id
                and existing["region"] == metric.region
                and existing["definition_version"] == metric.definition_version
            ):
                if existing == payload:
                    return False
                raise ConflictError(
                    f"指标定义 {metric.metric_id}/{metric.region}/"
                    f"{metric.definition_version} 已存在但内容不同；"
                    "请提升 definition_version 后再登记修订"
                )
        self.data["metrics"].append(payload)
        self._log(
            "metric.added",
            metric_id=metric.metric_id,
            region=metric.region,
            definition_version=metric.definition_version,
        )
        return True

    def metrics(self) -> list[MetricDefinition]:
        return [MetricDefinition.from_dict(item) for item in self.data["metrics"]]

    def definitions_for(self, metric_id: str, region: str) -> list[MetricDefinition]:
        found = [
            metric
            for metric in self.metrics()
            if metric.metric_id == metric_id and metric.region == region
        ]
        return sorted(found, key=lambda item: version_key(item.definition_version))

    # -- observations ------------------------------------------------------

    def add_observation(self, observation: Observation) -> bool:
        if not self.definitions_for(observation.metric_id, observation.region):
            raise DataError(
                f"观测 {observation.metric_id}/{observation.region} 缺少指标定义，"
                "不能先于定义登记"
            )
        if observation.definition_version and not any(
            metric.definition_version == observation.definition_version
            for metric in self.definitions_for(observation.metric_id, observation.region)
        ):
            raise DataError(
                f"观测 {observation.metric_id}/{observation.region} 声明了未登记的定义版本 "
                f"{observation.definition_version}"
            )
        payload = models.to_primitive(observation)
        for existing in self.data["observations"]:
            if (
                existing["metric_id"] == observation.metric_id
                and existing["region"] == observation.region
                and existing["period"] == observation.period
                and existing["source_version"] == observation.source_version
                and existing["definition_version"] == observation.definition_version
            ):
                if existing == payload:
                    return False
                raise ConflictError(
                    f"观测 {observation.metric_id}/{observation.region}/"
                    f"{observation.period}/{observation.source_version}"
                    f"（定义版本 {observation.definition_version or '未注明'}）已存在但内容不同；"
                    "请使用新的 source_version 登记修订"
                )
        self.data["observations"].append(payload)
        self._log(
            "observation.added",
            metric_id=observation.metric_id,
            region=observation.region,
            period=observation.period,
            source_version=observation.source_version,
        )
        return True

    def observations(self) -> list[Observation]:
        return [Observation.from_dict(item) for item in self.data["observations"]]

    def observation_versions(
        self, metric_id: str, region: str, period: str
    ) -> list[Observation]:
        found = [
            obs
            for obs in self.observations()
            if obs.metric_id == metric_id and obs.region == region and obs.period == period
        ]
        return sorted(
            found,
            key=lambda item: (
                version_key(item.source_version),
                version_key(item.definition_version),
            ),
        )

    # -- mapping proposals -------------------------------------------------

    def add_proposal(self, proposal: MappingProposal) -> str:
        payload = models.to_primitive(proposal)
        payload["proposal_rev"] = "p-" + record_hash(payload)
        if self.target(proposal.target_metric) is None:
            raise DataError(f"映射 {proposal.mapping_id} 指向未登记的目标指标 {proposal.target_metric}")
        history = [item for item in self.data["proposals"] if item["mapping_id"] == proposal.mapping_id]
        if history:
            latest = dict(history[-1])
            latest.pop("supersedes", None)
            if latest == payload:
                return history[-1]["proposal_rev"]
        if history:
            previous = history[-1]
            payload["supersedes"] = previous["proposal_rev"]
            self._log(
                "proposal.revised",
                mapping_id=proposal.mapping_id,
                previous=previous["proposal_rev"],
                current=payload["proposal_rev"],
            )
        else:
            self._log("proposal.added", mapping_id=proposal.mapping_id, current=payload["proposal_rev"])
        self.data["proposals"].append(payload)
        return payload["proposal_rev"]

    def proposals(self, *, latest_only: bool = True) -> list[MappingProposal]:
        rows = self.data["proposals"]
        if not latest_only:
            return [MappingProposal.from_dict(item) for item in rows]
        latest: dict[str, MappingProposal] = {}
        for item in rows:
            proposal = MappingProposal.from_dict(item)
            latest[proposal.mapping_id] = proposal
        return list(latest.values())

    def proposals_for_target(self, target_metric: str) -> list[MappingProposal]:
        return [
            item
            for item in self.proposals()
            if item.target_metric == target_metric
        ]

    def current_proposal_rev(self, mapping_id: str) -> str:
        rows = [item for item in self.data["proposals"] if item["mapping_id"] == mapping_id]
        if not rows:
            raise KeyError(mapping_id)
        return rows[-1]["proposal_rev"]

    # -- reviews -----------------------------------------------------------

    def add_review(self, review: ReviewRecord) -> ReviewRecord:
        if not any(
            item["mapping_id"] == review.mapping_id for item in self.data["proposals"]
        ):
            raise DataError(f"审核引用了未登记的映射 {review.mapping_id}")
        if review.decision not in models.REVIEW_STATUSES:
            raise DataError(f"未知审核结论: {review.decision!r}")
        current_rev = self.current_proposal_rev(review.mapping_id)
        # The review identity includes the proposal revision: re-approving a
        # revised proposal is a new decision, not a duplicate of the old one.
        bound = replace(review, proposal_rev=current_rev)
        payload = models.to_primitive(bound)
        payload["review_id"] = bound.review_id
        payload["recorded_at"] = payload.get("recorded_at") or _now()
        for existing in self.data["reviews"]:
            if existing["review_id"] == payload["review_id"]:
                return ReviewRecord.from_dict(existing)
        self.data["reviews"].append(payload)
        self._log(
            "review.added",
            mapping_id=review.mapping_id,
            decision=review.decision,
            reviewer_region=review.reviewer_region,
        )
        return ReviewRecord.from_dict(payload)

    def reviews(self, mapping_id: str | None = None) -> list[ReviewRecord]:
        rows = self.data["reviews"]
        return [
            ReviewRecord.from_dict(item)
            for item in rows
            if mapping_id is None or item["mapping_id"] == mapping_id
        ]

    def effective_review(
        self, mapping_id: str, region: str | None = None
    ) -> tuple[str, ReviewRecord | None, bool]:
        """Return ``(status, latest_review, stale)`` for the current revision.

        Reviews are scoped by reviewer region when a region is supplied: a
        regional partner's rejection only removes that region's cell, while a
        review with no reviewer region is treated as project-wide.  A review
        recorded against an older proposal revision is retained for the audit
        trail but does not grant approval to the new revision.
        """
        current_rev = self.current_proposal_rev(mapping_id)
        relevant = [
            item
            for item in self.data["reviews"]
            if item["mapping_id"] == mapping_id
            and (region is None or not item.get("reviewer_region") or item["reviewer_region"] == region)
        ]
        if not relevant:
            return "pending", None, False
        latest_payload = relevant[-1]
        latest = ReviewRecord.from_dict(latest_payload)
        stale = latest_payload.get("proposal_rev") != current_rev
        if stale:
            return "pending", latest, True
        return latest.decision, latest, False

    # -- projects ----------------------------------------------------------

    def save_project(self, project: Project) -> Project:
        if not project.project_id:
            raise DataError("project_id 不能为空")
        payload = project.to_dict()
        previous = self.data["projects"].get(project.project_id)
        if previous is None:
            payload["created_at"] = payload.get("created_at") or _now()
        else:
            payload["created_at"] = previous.get("created_at") or payload.get("created_at")
        payload["updated_at"] = _now()
        self.data["projects"][project.project_id] = payload
        self._log("project.saved", project_id=project.project_id, revision=record_hash(payload))
        return Project.from_dict(payload)

    def project(self, project_id: str) -> Project | None:
        payload = self.data["projects"].get(project_id)
        return Project.from_dict(payload) if payload else None

    def projects(self) -> list[Project]:
        return [Project.from_dict(item) for item in self.data["projects"].values()]

    # -- shares ------------------------------------------------------------

    def save_share(self, share: Mapping[str, Any]) -> None:
        result_id = text(share.get("shared_result_id"))
        if not result_id:
            raise DataError("share 缺少 shared_result_id")
        if result_id in self.data["shares"]:
            raise ConflictError(
                f"分享结果 {result_id} 已存在；对外分享快照不可覆盖，请使用新的结果编号"
            )
        self.data["shares"][result_id] = models.to_primitive(share)
        self._log("share.created", shared_result_id=result_id)

    def share(self, result_id: str) -> dict[str, Any] | None:
        return self.data["shares"].get(result_id)

    def shares(self) -> list[dict[str, Any]]:
        return list(self.data["shares"].values())

    # -- CSV import --------------------------------------------------------

    # -- metric/observation registration -----------------------------------

    def register_mapping_row(self, row: Mapping[str, Any]) -> tuple[MappingProposal, ReviewRecord | None]:
        """Register one combined mapping/review CSV row.

        Returns the proposal and the initial review, if the row carries one.
        The review is only recorded after the proposal exists, so it is always
        bound to the current proposal revision.
        """
        proposal = MappingProposal.from_row(row)
        # Parse the review before mutating anything so a malformed review row
        # cannot leave a half-imported mapping behind.
        review = ReviewRecord.from_mapping_row(row)
        if review is not None and review.decision not in models.REVIEW_STATUSES:
            raise DataError(f"未知审核结论: {review.decision!r}")
        self.add_proposal(proposal)
        if review is not None:
            review = self.add_review(review)
        return proposal, review

    def register_observation_row(self, row: Mapping[str, Any]) -> bool:
        observation = Observation.from_row(row)
        definitions = self.definitions_for(observation.metric_id, observation.region)
        if definitions:
            matching = [
                item
                for item in definitions
                if not observation.definition_version
                or item.definition_version == observation.definition_version
            ]
            if not matching:
                raise DataError(
                    f"观测 {observation.metric_id}/{observation.region} 声明的定义版本"
                    f" {observation.definition_version} 与该地区任何已登记版本都不匹配"
                )
            latest = sorted(matching, key=lambda item: version_key(item.definition_version))[-1]
            if not models.period_matches_frequency(observation.period, latest.frequency):
                raise DataError(
                    f"period {observation.period} 与频率 {latest.frequency} 不匹配"
                )
        return self.add_observation(observation)

    def import_csv(self, kind: str, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.exists():
            raise DataError(f"找不到 CSV 文件: {path}")
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if kind == "targets":
            return self._import_targets(rows)
        if kind == "metrics":
            return self._import_metrics(rows)
        if kind == "observations":
            return self._import_observations(rows)
        if kind == "mappings_reviews":
            return self._import_mappings_reviews(rows)
        raise DataError(f"未知导入类型: {kind!r}")

    def _import_targets(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        summary = {"added": 0, "unchanged": 0, "errors": []}
        for number, row in enumerate(rows, start=2):
            try:
                target = Target.from_row(row)
                existing = self.target(target.target_metric)
                self.upsert_target(target)
                if existing is None:
                    summary["added"] += 1
                else:
                    summary["unchanged"] += 1
            except (DataError, ConflictError) as exc:
                summary["errors"].append({"row": number, "error": str(exc)})
        return summary

    def _import_metrics(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        summary = {"added": 0, "unchanged": 0, "errors": []}
        for number, row in enumerate(rows, start=2):
            try:
                added = self.add_metric(MetricDefinition.from_row(row))
                summary["added" if added else "unchanged"] += 1
            except (DataError, ConflictError) as exc:
                summary["errors"].append({"row": number, "error": str(exc)})
        return summary

    def _import_observations(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        summary = {"added": 0, "unchanged": 0, "errors": []}
        for number, row in enumerate(rows, start=2):
            try:
                added = self.register_observation_row(row)
                summary["added" if added else "unchanged"] += 1
            except (DataError, ConflictError) as exc:
                summary["errors"].append({"row": number, "error": str(exc)})
        return summary

    def _import_mappings_reviews(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        summary = {"proposals_added": 0, "reviews_added": 0, "errors": []}
        for number, row in enumerate(rows, start=2):
            try:
                if not text(row.get("target_metric")):
                    # Without a target we cannot invent the mapping direction.
                    raise DataError("映射行缺少 target_metric")
                proposals_before = len(self.data["proposals"])
                reviews_before = len(self.data["reviews"])
                self.register_mapping_row(row)
                if len(self.data["proposals"]) > proposals_before:
                    summary["proposals_added"] += 1
                if len(self.data["reviews"]) > reviews_before:
                    summary["reviews_added"] += 1
            except (DataError, ConflictError) as exc:
                summary["errors"].append({"row": number, "error": str(exc)})
        return summary
