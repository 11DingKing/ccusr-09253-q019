"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.explain import (
    ExplanationGraph,
    ViewerRole,
    content_fingerprint,
    trim_node_dict,
    verify_graph_document,
)
from .core.replay import replay_explained
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
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
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


def _rebuild_graph(db: Session, plan_version: str) -> ExplanationGraph:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    _, graph = replay_explained(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )
    return graph


def _rebuild_graph_at_cutoff(
    db: Session,
    plan_version: str,
    event_cutoff_id: str | None,
    *,
    rule_version: str | None = None,
) -> ExplanationGraph:
    """按冻结截止事件重建解释图；cutoff 为空表示冻结时还没有任何事件。"""
    plan = _require_plan(db, plan_version)
    if event_cutoff_id is None:
        events: list[Any] = []
    else:
        events = load_events_up_to(db, plan_version, event_cutoff_id)
    kwargs: dict[str, Any] = {}
    if rule_version is not None:
        kwargs["rule_version"] = rule_version
    _, graph = replay_explained(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id=event_cutoff_id,
        **kwargs,
    )
    return graph


def _graph_for_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> ExplanationGraph:
    """执行确定性的业务处理。"""
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    if snap.explanation_graph is not None:
        return snap.explanation_graph
    # 兼容旧冻结：按冻结截止事件确定性重建同一张图。
    return _rebuild_graph_at_cutoff(db, plan_version, snap.event_cutoff_id)


def explanation_graph_for_student(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    viewer_role: str = ViewerRole.STAFF,
    freeze_id: str | None = None,
) -> dict[str, Any] | None:
    """按学生与冻结生成最小因果子图，字段按隐私角色裁剪。"""
    if freeze_id is not None:
        graph = _graph_for_freeze(db, plan_version, freeze_id)
    else:
        _require_plan(db, plan_version)
        graph = _rebuild_graph(db, plan_version)
    subgraph = graph.subgraph_for_student(student_id)
    if subgraph is None:
        return None
    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "student_id": student_id,
        "viewer_role": ViewerRole(viewer_role).value,
        "graph_digest": subgraph.digest,
        "root_node_id": ExplanationGraph.root_for_student(student_id),
        "nodes": [
            trim_node_dict(node.to_dict(), ViewerRole(viewer_role))
            for node in subgraph.sorted_nodes()
        ],
    }


def trace_explanation_node(
    db: Session,
    plan_version: str,
    node_id: str,
    *,
    viewer_role: str = ViewerRole.STAFF,
    freeze_id: str | None = None,
) -> dict[str, Any] | None:
    """追溯单个节点：返回该节点及其全部因果祖先。"""
    if freeze_id is not None:
        graph = _graph_for_freeze(db, plan_version, freeze_id)
    else:
        _require_plan(db, plan_version)
        graph = _rebuild_graph(db, plan_version)
    node = graph.nodes.get(node_id)
    if node is None:
        return None
    role = ViewerRole(viewer_role)
    ancestors = graph.ancestors_of(node_id)
    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "node_id": node_id,
        "viewer_role": role.value,
        "node": trim_node_dict(node.to_dict(), role),
        "ancestors": [
            trim_node_dict(ancestors[key].to_dict(), role)
            for key in sorted(ancestors)
            if key != node_id
        ],
    }


def verify_explanation_graph(
    db: Session, plan_version: str, freeze_id: str
) -> dict[str, Any]:
    """完整性核验：校验冻结图文档，并用事件日志重放比对摘要。"""
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    problems: list[str] = []
    stored_digest: str | None = None
    stored_rule_version: str | None = None
    # 直接核验持久化的原始文档：其中的指纹与摘要是数据而非重算值。
    document = row.snapshot.get("explanation_graph")
    if document is not None:
        problems.extend(verify_graph_document(document))
        stored_digest = document.get("graph_digest")
        stored_rule_version = document.get("rule_version")
    # 用冻结图记录的规则版本重放，规则升级不会使旧冻结被误判为不一致。
    rebuilt = _rebuild_graph_at_cutoff(
        db,
        plan_version,
        row.event_cutoff_id,
        rule_version=stored_rule_version,
    )
    rebuilt_digest = rebuilt.digest
    if stored_digest is not None and stored_digest != rebuilt_digest:
        problems.append("冻结图摘要与事件日志重放结果不一致")
    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "event_cutoff_id": row.event_cutoff_id,
        "stored_digest": stored_digest,
        "rebuilt_digest": rebuilt_digest,
        "consistent": not problems,
        "problems": problems,
    }


def export_explanation_graph(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    viewer_role: str,
    freeze_id: str | None = None,
) -> dict[str, Any] | None:
    """受控导出：受限字段仅工作人员可导出，清单记录裁剪口径与摘要。"""
    role = ViewerRole(viewer_role)
    package = explanation_graph_for_student(
        db,
        plan_version,
        student_id,
        viewer_role=role,
        freeze_id=freeze_id,
    )
    if package is None:
        return None
    manifest = {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "student_id": student_id,
        "viewer_role": role.value,
        "graph_digest": package["graph_digest"],
        "node_count": len(package["nodes"]),
        "restricted_fields_included": role == ViewerRole.STAFF,
    }
    manifest["manifest_fingerprint"] = content_fingerprint(manifest)
    return {"manifest": manifest, "graph": package}
