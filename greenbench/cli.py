"""Command line interface for the workbench.

The CLI exposes registration, import, comparison, sharing, revision impact and
per-cell tracing so analysts and local partners can work from the same files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import models
from .compare import (
    Comparator,
    conclusion_label,
    evidence_label,
    impact_report,
    result_markdown,
    snapshot,
)
from .models import (
    DataError,
    Project,
    ReviewRecord,
    Target,
    to_primitive,
)
from .store import Store


def _store(args: argparse.Namespace) -> Store:
    return Store.load(args.db)


def _print_json(value: Any) -> None:
    json.dump(to_primitive(value), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def cmd_target_add(args: argparse.Namespace) -> int:
    store = _store(args)
    target = Target(
        target_metric=args.target_metric,
        description=args.description or "",
        canonical_unit=args.unit or "",
        canonical_boundary=args.boundary or "",
        direction=args.direction,
        goal=args.goal or "",
    )
    store.upsert_target(target)
    store.save()
    _print_json({"target_metric": target.target_metric, "status": "saved"})
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    store = _store(args)
    summary = store.import_csv(args.kind, args.path)
    store.save()
    _print_json(summary)
    return 1 if summary.get("errors") else 0


def cmd_project_create(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    project = Project.from_dict(payload)
    if not project.project_id:
        raise DataError("项目文件缺少 project_id")
    store.save_project(project)
    store.save()
    _print_json({"project_id": project.project_id, "status": "saved"})
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    store = _store(args)
    project = store.project(args.project)
    if project is None:
        raise DataError(f"找不到项目 {args.project}")
    result = Comparator(store).compare(project)
    if args.output:
        Path(args.output).write_text(result_markdown(result), encoding="utf-8")
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(to_primitive(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if not args.output and not args.json_output:
        sys.stdout.write(result_markdown(result))
    else:
        _print_json(
            {
                "project_id": project.project_id,
                "coverage": result["coverage"],
                "overall_status": result["overall"]["status"],
                "output": args.output,
                "json_output": args.json_output,
            }
        )
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    store = _store(args)
    project = store.project(args.project)
    if project is None:
        raise DataError(f"找不到项目 {args.project}")
    result = Comparator(store).compare(project)
    payload = snapshot(result)
    payload["shared_result_id"] = args.result_id
    payload["shared_at"] = models.now_iso()
    payload["shared_with"] = args.with_ or ""
    store.save_share(payload)
    store.save()
    if args.output:
        Path(args.output).write_text(result_markdown(result), encoding="utf-8")
    _print_json(
        {
            "shared_result_id": args.result_id,
            "project_id": project.project_id,
            "coverage": payload["coverage"],
        }
    )
    return 0


def cmd_impact(args: argparse.Namespace) -> int:
    store = _store(args)
    shared = store.share(args.result_id)
    if shared is None:
        raise DataError(f"找不到分享结果 {args.result_id}")
    project = store.project(shared["project_id"])
    if project is None:
        raise DataError(f"分享结果引用的项目 {shared['project_id']} 不存在")
    current = Comparator(store).compare(project)
    report = impact_report(shared, current)
    if args.output:
        Path(args.output).write_text(
            json.dumps(to_primitive(report), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        Path(args.markdown).write_text(_impact_markdown(report), encoding="utf-8")
    _print_json(report)
    return 0


def _impact_markdown(report: dict[str, Any]) -> str:
    lines = [f"# 修订影响报告：{report.get('project_id')}", ""]
    lines.append(f"- 分享快照：`{report.get('shared_result_id')}`（{report.get('shared_at') or '时间未注明'}）")
    lines.append(
        f"- 可比覆盖：{report['coverage_change']['shared']}% → "
        f"{report['coverage_change']['current']}%"
    )
    lines.append(
        f"- 结论状态：{conclusion_label(report['conclusion_status_change']['shared'])} → "
        f"{conclusion_label(report['conclusion_status_change']['current'])}"
    )
    lines.append("")
    if report.get("affected_targets"):
        lines.append("## 受影响的指标")
        lines.append("")
        for name, change in report["affected_targets"].items():
            lines.append(
                f"- **{name}**：可比覆盖 {change['coverage']['shared']}% → "
                f"{change['coverage']['current']}%；排名条目 "
                f"{len(change['ranking']['shared'])} → {len(change['ranking']['current'])}"
            )
        lines.append("")
    lines.append("## 受影响的比较")
    lines.append("")
    if not report["affected_cells"]:
        lines.append("_没有单元发生变化。_")
        lines.append("")
    for item in report["affected_cells"]:
        lines.append(f"### {item['cell']}")
        if item["change"] != "changed":
            lines.append(f"- 变化类型：{item['change']}")
            lines.append("")
            continue
        for field, change in item["fields"].items():
            if field == "evidence":
                lines.append(
                    f"- 证据等级：{evidence_label(change['shared'])} → "
                    f"{evidence_label(change['current'])}"
                )
            elif field == "value":
                lines.append(
                    f"- 值：{change['shared'] or '—'} → {change['current'] or '—'}"
                )
            elif field == "unit":
                lines.append(
                    f"- 单位：{change['shared'] or '—'} → {change['current'] or '—'}"
                )
            elif field == "review_status":
                lines.append(
                    f"- 审核状态：{change['shared'] or '—'} → {change['current'] or '—'}"
                )
            else:
                lines.append(
                    f"- {field}：{change['shared'] or '—'} → {change['current'] or '—'}"
                )
        if item["current"].get("reasons"):
            lines.append("- 当前说明：" + "；".join(item["current"]["reasons"]))
        lines.append("")
    lines.append("## 项目说明与注意事项")
    lines.append("")
    if report["project_notes"].get("changed"):
        lines.append(
            f"- 项目说明：{report['project_notes']['shared']!r} → "
            f"{report['project_notes']['current']!r}"
        )
    for caveat in report["caveats"].get("added", []):
        lines.append(f"- 新增注意事项：{caveat}")
    for caveat in report["caveats"].get("removed", []):
        lines.append(f"- 已解除注意事项：{caveat}")
    return "\n".join(lines).rstrip() + "\n"


def cmd_trace(args: argparse.Namespace) -> int:
    store = _store(args)
    project = store.project(args.project)
    if project is None:
        raise DataError(f"找不到项目 {args.project}")
    result = Comparator(store).compare(project)
    report = result["targets"].get(args.target)
    if report is None:
        raise DataError(f"项目不包含目标指标 {args.target}")
    cell = report["cells"].get(args.region)
    if cell is None:
        raise DataError(f"项目不包含地区 {args.region}")
    _print_json(cell)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    store = _store(args)
    review = ReviewRecord(
        mapping_id=args.mapping_id,
        decision=args.decision,
        reviewer_region=args.region or "",
        reviewed_at=args.at or "",
        note=args.note or "",
    )
    store.add_review(review)
    store.save()
    _print_json({"mapping_id": args.mapping_id, "decision": args.decision, "status": "recorded"})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Summarize effective review state and revision staleness."""
    store = _store(args)
    rows = []
    for proposal in store.proposals():
        status, review, stale = store.effective_review(proposal.mapping_id)
        rows.append(
            {
                "mapping_id": proposal.mapping_id,
                "region": proposal.region,
                "source_metric": proposal.source_metric,
                "target_metric": proposal.target_metric,
                "transform": proposal.transform,
                "proposal_rev": store.current_proposal_rev(proposal.mapping_id),
                "review_status": status,
                "stale_review": stale,
                "latest_review": None if review is None else review.review_id,
            }
        )
    _print_json({"mappings": rows, "shares": sorted(store.data["shares"])})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.what == "targets":
        _print_json(store.targets())
    elif args.what == "metrics":
        _print_json(store.metrics())
    elif args.what == "observations":
        _print_json(store.observations())
    elif args.what == "proposals":
        _print_json(store.proposals(latest_only=False))
    elif args.what == "reviews":
        _print_json(store.reviews())
    elif args.what == "projects":
        _print_json(store.projects())
    elif args.what == "shares":
        _print_json(store.shares())
    else:
        raise DataError(f"未知查询对象: {args.what}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="greenbench",
        description="绿色转型指标可比性工作台",
    )
    parser.add_argument("--db", default="greenbench.json", help="工作台数据文件")
    sub = parser.add_subparsers(dest="command", required=True)

    target = sub.add_parser("target-add", help="登记目标指标概念")
    target.add_argument("target_metric")
    target.add_argument("--description", default="")
    target.add_argument("--unit", default="")
    target.add_argument("--boundary", default="")
    target.add_argument("--direction", choices=models.DIRECTIONS, default="higher_is_better")
    target.add_argument("--goal", default="")
    target.set_defaults(func=cmd_target_add)

    importer = sub.add_parser("import", help="导入 CSV（指标定义、观测、映射与审核）")
    importer.add_argument("kind", choices=("targets", "metrics", "observations", "mappings_reviews"))
    importer.add_argument("path")
    importer.set_defaults(func=cmd_import)

    project = sub.add_parser("project-create", help="从 JSON 文件创建或更新项目")
    project.add_argument("file")
    project.set_defaults(func=cmd_project_create)

    compare = sub.add_parser("compare", help="生成比较结果")
    compare.add_argument("project")
    compare.add_argument("--output", help="写入 Markdown 复核包")
    compare.add_argument("--json-output", help="写入完整 JSON 结果")
    compare.set_defaults(func=cmd_compare)

    share = sub.add_parser("share", help="冻结并登记一次对外分享")
    share.add_argument("project")
    share.add_argument("result_id")
    share.add_argument("--with", dest="with_", default="", help="接收方")
    share.add_argument("--output", help="同时写出 Markdown 复核包")
    share.set_defaults(func=cmd_share)

    impact = sub.add_parser("impact", help="比较分享快照与当前结果")
    impact.add_argument("result_id")
    impact.add_argument("--output", help="写入 JSON 影响报告")
    impact.add_argument("--markdown", help="写入 Markdown 影响报告")
    impact.set_defaults(func=cmd_impact)

    trace = sub.add_parser("trace", help="追踪单个地区/指标的完整链路")
    trace.add_argument("project")
    trace.add_argument("target")
    trace.add_argument("region")
    trace.set_defaults(func=cmd_trace)

    review = sub.add_parser("review", help="记录一次伙伴审核意见")
    review.add_argument("mapping_id")
    review.add_argument("decision", choices=models.REVIEW_STATUSES)
    review.add_argument("--region", help="审核方地区")
    review.add_argument("--at", help="审核日期")
    review.add_argument("--note", default="")
    review.set_defaults(func=cmd_review)

    status = sub.add_parser("status", help="汇总映射审核状态与分享编号")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show", help="查看已登记记录")
    show.add_argument("what", choices=("targets", "metrics", "observations", "proposals", "reviews", "projects", "shares"))
    show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (DataError, models.ConflictError) as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
