"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.explain import (
    KIND_ACADEMIC_DAY,
    KIND_EVENT,
    KIND_PROGRESS,
    KIND_TOTAL,
    VIEWER_FIELD_POLICY,
    build_student_explanation,
    export_package,
    graph_to_dict,
    node_to_dict,
    trace_node,
    verify_graph_dict,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    insert_events,
    insert_freeze,
    load_events,
    load_events_up_to,
    max_event_id,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class UnknownViewerRoleError(Exception):
    pass


class InvalidExportRequestError(Exception):
    pass


class NodeNotFoundError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = _require_freeze(db, plan_version, freeze_id)
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


def _require_freeze(db: Session, plan_version: str, freeze_id: str):
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return row


def _validate_viewer_role(viewer_role: str) -> None:
    if viewer_role not in VIEWER_FIELD_POLICY:
        raise UnknownViewerRoleError(f"unknown viewer role '{viewer_role}'")


def _frozen_events(db: Session, plan_version: str, cutoff: str | None) -> list:
    """冻结边界内的事件；截止点为空表示冻结时还没有任何事件。"""
    if cutoff is None:
        return []
    return load_events_up_to(db, plan_version, cutoff)


def _build_student_graph(
    db: Session, plan, student_id: str, cutoff: str | None, *, frozen: bool
):
    if frozen:
        events = _frozen_events(db, plan.plan_version, cutoff)
    else:
        events = load_events(db, plan.plan_version)
    return build_student_explanation(
        events,
        student_id,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id=cutoff,
    )


def student_explanation(
    db: Session,
    plan_version: str,
    student_id: str,
    viewer_role: str = "staff",
) -> dict[str, Any] | None:
    """实时解释查询：当前全部事件导出的最小因果子图。"""
    plan = _require_plan(db, plan_version)
    _validate_viewer_role(viewer_role)
    graph = _build_student_graph(db, plan, student_id, None, frozen=False)
    if graph is None:
        return None
    data = graph_to_dict(graph, viewer_role)
    data["freeze_id"] = None
    return data


def frozen_student_explanation(
    db: Session,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    viewer_role: str = "staff",
) -> dict[str, Any] | None:
    """冻结解释查询：按冻结截止点重放得到的最小因果子图。"""
    plan = _require_plan(db, plan_version)
    _validate_viewer_role(viewer_role)
    row = _require_freeze(db, plan_version, freeze_id)
    graph = _build_student_graph(
        db, plan, student_id, row.event_cutoff_id, frozen=True
    )
    if graph is None:
        return None
    data = graph_to_dict(graph, viewer_role)
    data["freeze_id"] = freeze_id
    return data


def trace_frozen_explanation_node(
    db: Session,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    node_id: str,
    viewer_role: str = "staff",
) -> dict[str, Any] | None:
    """节点追溯：返回节点的因果祖先链与受影响后代。"""
    plan = _require_plan(db, plan_version)
    _validate_viewer_role(viewer_role)
    row = _require_freeze(db, plan_version, freeze_id)
    graph = _build_student_graph(
        db, plan, student_id, row.event_cutoff_id, frozen=True
    )
    if graph is None:
        return None
    traced = trace_node(graph, node_id)
    if traced is None:
        raise NodeNotFoundError(
            f"node '{node_id}' is not part of the explanation graph"
        )
    masked = VIEWER_FIELD_POLICY[viewer_role]
    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "student_id": student_id,
        "viewer_role": viewer_role,
        "node": node_to_dict(traced["node"], masked),
        "ancestors": [node_to_dict(n, masked) for n in traced["ancestors"]],
        "descendants": [node_to_dict(n, masked) for n in traced["descendants"]],
    }


def verify_frozen_explanation(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    """完整性核验：重算图并交叉核对冻结快照中的学生行。"""
    plan = _require_plan(db, plan_version)
    row = _require_freeze(db, plan_version, freeze_id)
    cutoff = row.event_cutoff_id
    graph = _build_student_graph(db, plan, student_id, cutoff, frozen=True)
    if graph is None:
        return None

    data = graph_to_dict(graph, "staff")
    report = verify_graph_dict(data)
    checks = list(report["checks"])

    # 截止点约束：图内事件不得晚于冻结截止事件。
    if cutoff is not None:
        offenders = sorted(
            n["attributes"]["event_id"]
            for n in data["nodes"]
            if n["kind"] == KIND_EVENT and n["attributes"]["event_id"] > cutoff
        )
        checks.append(
            {
                "name": "cutoff_respected",
                "ok": not offenders,
                "detail": "all events are at or before the freeze cutoff"
                if not offenders
                else f"events past cutoff: {offenders}",
            }
        )

    # 确定性：换一种事件读取路径重建，图摘要必须一致。
    alt_graph = build_student_explanation(
        load_events(db, plan.plan_version),
        student_id,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id=cutoff,
    )
    deterministic = alt_graph is not None and alt_graph.digest == graph.digest
    checks.append(
        {
            "name": "deterministic_replay",
            "ok": deterministic,
            "detail": "recomputation from a fresh event load yields the same digest"
            if deterministic
            else "digest mismatch across recomputations",
        }
    )

    # 与冻结快照中的学生行交叉核对。
    snapshot = Snapshot.from_dict(row.snapshot)
    stored = explain_student(snapshot, student_id)
    if stored is None:
        checks.append(
            {
                "name": "matches_frozen_snapshot",
                "ok": False,
                "detail": "student is missing from the frozen snapshot",
            }
        )
    else:
        total_node = next(n for n in data["nodes"] if n["kind"] == KIND_TOTAL)
        progress_node = next(
            n for n in data["nodes"] if n["kind"] == KIND_PROGRESS
        )
        mismatches: list[str] = []
        comparisons = [
            ("total_seconds", total_node["attributes"]["total_seconds"], stored["total_seconds"]),
            ("confirmed_seconds", total_node["attributes"]["confirmed_seconds"], stored["confirmed_seconds"]),
            ("adjustment_seconds", total_node["attributes"]["adjustment_seconds"], stored["adjustment_seconds"]),
            ("lesson_units", progress_node["attributes"]["lesson_units"], stored["lesson_units"]),
            ("meets_requirement", progress_node["attributes"]["meets_requirement"], stored["meets_requirement"]),
        ]
        for field_name, graph_value, stored_value in comparisons:
            if graph_value != stored_value:
                mismatches.append(
                    f"{field_name}: graph={graph_value} snapshot={stored_value}"
                )
        graph_daily = sorted(
            (n["attributes"]["academic_day"], n["attributes"]["seconds"])
            for n in data["nodes"]
            if n["kind"] == KIND_ACADEMIC_DAY
        )
        stored_daily = sorted(
            (d["academic_day"], d["seconds"]) for d in stored["daily"]
        )
        if graph_daily != stored_daily:
            mismatches.append("daily breakdown differs")
        checks.append(
            {
                "name": "matches_frozen_snapshot",
                "ok": not mismatches,
                "detail": "graph totals match the frozen snapshot"
                if not mismatches
                else "; ".join(mismatches),
            }
        )

    return {
        "plan_version": plan.plan_version,
        "freeze_id": freeze_id,
        "student_id": student_id,
        "ok": all(c["ok"] for c in checks),
        "graph_digest": graph.digest,
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "checks": checks,
    }


def export_frozen_explanation(
    db: Session,
    plan_version: str,
    freeze_id: str,
    student_id: str,
    *,
    viewer_role: str,
    purpose: str,
) -> dict[str, Any] | None:
    """受控导出：按角色裁剪字段并附自描述清单与内容摘要。"""
    plan = _require_plan(db, plan_version)
    _validate_viewer_role(viewer_role)
    purpose = purpose.strip()
    if not purpose:
        raise InvalidExportRequestError("export purpose must not be empty")
    row = _require_freeze(db, plan_version, freeze_id)
    graph = _build_student_graph(
        db, plan, student_id, row.event_cutoff_id, frozen=True
    )
    if graph is None:
        return None
    data = graph_to_dict(graph, viewer_role)
    data["freeze_id"] = freeze_id
    return export_package(
        data,
        graph_digest=graph.digest,
        viewer_role=viewer_role,
        purpose=purpose,
        freeze_id=freeze_id,
    )
