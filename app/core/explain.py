"""学时解释的确定性因果图。

重放内核只输出最终数字；本模块在相同输入上为单个学生重建最小因果子图，
解释总学时由哪些事件、合并区间、确认与修正步骤得出，供申诉复核使用。

不变量：
- 相同输入多次计算结果一致（节点、边、指纹、图摘要均确定性生成）；
- 节点带来源版本（培养方案版本 + 规则版本）与输入指纹；
- 隐私角色只裁剪可见字段，节点标识、输入指纹与图摘要永不被裁剪。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable

from .clock import elapsed_seconds, merge_intervals, split_by_academic_day
from .replay import (
    RULE_VERSION,
    CheckinStatus,
    Event,
    EventType,
    parse_checkin,
    replay,
)

GRAPH_VERSION = "explanation/v1"
REDACTED_VALUE = "[redacted]"

# 可见性策略：角色 -> 被裁剪的属性名。节点标识、输入指纹与图摘要不属于
# “字段”，永不被裁剪，保证不同角色看到同一张图的同一批节点。
VIEWER_FIELD_POLICY: dict[str, frozenset[str]] = {
    "staff": frozenset(),
    "mentor": frozenset({"reason"}),
    "student": frozenset({"reason"}),
}

# 节点类型。
KIND_EVENT = "event"
KIND_MERGED_INTERVAL = "merged_interval"
KIND_ACADEMIC_DAY = "academic_day"
KIND_TOTAL = "total"
KIND_PROGRESS = "progress"

# 边类型（方向与数据流一致：source 是 target 的因果输入）。
EDGE_CONFIRMS = "confirms"
EDGE_CONTRIBUTES = "contributes"
EDGE_AGGREGATES = "aggregates"
EDGE_ADJUSTS = "adjusts"
EDGE_SPLITS_INTO = "splits_into"
EDGE_SUMMARIZES = "summarizes"


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _iso_z(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


def compute_node_fingerprint(
    node_id: str,
    kind: str,
    source_version: dict[str, str],
    inputs: Iterable[str],
    attributes: dict[str, Any],
) -> str:
    """计算节点输入指纹：覆盖标识、来源版本、直接输入与属性。"""
    return _sha256_text(
        _canonical(
            {
                "node_id": node_id,
                "kind": kind,
                "source_version": source_version,
                "inputs": sorted(inputs),
                "attributes": attributes,
            }
        )
    )


@dataclass(frozen=True)
class ExplanationNode:
    """解释图节点：标识稳定，带来源版本与输入指纹。"""

    node_id: str
    kind: str
    source_version: dict[str, str]
    inputs: tuple[str, ...]
    attributes: dict[str, Any]
    input_fingerprint: str


@dataclass(frozen=True)
class ExplanationEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class ExplanationGraph:
    """按学生与冻结边界生成的最小因果子图。"""

    graph_version: str
    plan_version: str
    student_id: str
    rule_version: str
    timezone: str
    required_seconds: int
    event_cutoff_id: str | None
    nodes: tuple[ExplanationNode, ...]
    edges: tuple[ExplanationEdge, ...]
    digest: str


def _make_node(
    node_id: str,
    kind: str,
    source_version: dict[str, str],
    inputs: Iterable[str],
    attributes: dict[str, Any],
) -> ExplanationNode:
    ordered_inputs = tuple(sorted(inputs))
    fingerprint = compute_node_fingerprint(
        node_id, kind, source_version, ordered_inputs, attributes
    )
    return ExplanationNode(
        node_id=node_id,
        kind=kind,
        source_version=dict(source_version),
        inputs=ordered_inputs,
        attributes=dict(attributes),
        input_fingerprint=fingerprint,
    )


def compute_graph_digest(
    graph_version: str,
    nodes: Iterable[ExplanationNode],
    edges: Iterable[ExplanationEdge],
) -> str:
    """图摘要：只依赖节点标识、指纹与边，与字段裁剪无关。"""
    node_fingerprints = {n.node_id: n.input_fingerprint for n in nodes}
    edge_rows = sorted([e.source, e.target, e.kind] for e in edges)
    return _sha256_text(
        _canonical(
            {
                "graph_version": graph_version,
                "nodes": node_fingerprints,
                "edges": edge_rows,
            }
        )
    )


def build_student_explanation(
    events: Iterable[Event],
    student_id: str,
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    rule_version: str = RULE_VERSION,
    up_to_event_id: str | None = None,
) -> ExplanationGraph | None:
    """按学生重建最小因果子图；学生无相关事件时返回 None。

    只纳入对总学时有因果贡献的节点：已确认签到、真正把签到从
    PENDING 转为 CONFIRMED 的导师确认、请假修正，以及由它们派生的
    合并区间、教学日、总学时与进度根节点。其他学生的事件、未生效的
    确认和待确认签到不会进入子图。
    """
    relevant = [
        e
        for e in events
        if e.student_id == student_id and e.plan_version == plan_version
    ]
    state = replay(
        relevant,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        up_to_event_id=up_to_event_id,
    )
    progress = state.students.get(student_id)
    if progress is None:
        return None

    ordered_events = sorted(
        (
            e
            for e in relevant
            if up_to_event_id is None or e.event_id <= up_to_event_id
        ),
        key=lambda e: e.event_id,
    )
    source_version = {"plan_version": plan_version, "rule_version": rule_version}

    # 与内核相同的增量扫描，找出真正把签到从 PENDING 转为 CONFIRMED
    # 的确认事件；只有它们对总学时有因果贡献。
    local_records: dict[str, Any] = {}
    confirming: dict[str, list[str]] = {}
    confirm_events: dict[str, Event] = {}
    for event in ordered_events:
        if event.event_type == EventType.CHECKIN:
            local_records[event.event_id] = parse_checkin(event, timezone_name)
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = local_records.get(target_id)
            if target is not None and target.status == CheckinStatus.PENDING:
                target.status = CheckinStatus.CONFIRMED
                confirming.setdefault(target_id, []).append(event.event_id)
                confirm_events[event.event_id] = event

    nodes: list[ExplanationNode] = []
    edges: list[ExplanationEdge] = []

    # 生效的导师确认事件节点。
    for confirm_id in sorted(confirm_events):
        event = confirm_events[confirm_id]
        target_id = event.payload["checkin_event_id"]
        nodes.append(
            _make_node(
                node_id=f"evt:{confirm_id}",
                kind=KIND_EVENT,
                source_version=source_version,
                inputs=(),
                attributes={
                    "event_id": confirm_id,
                    "event_type": EventType.MENTOR_CONFIRM.value,
                    "student_id": event.student_id,
                    "checkin_event_id": target_id,
                    "applied": True,
                },
            )
        )
        edges.append(
            ExplanationEdge(f"evt:{confirm_id}", f"evt:{target_id}", EDGE_CONFIRMS)
        )

    # 计入学时的签到事件节点；生效确认是其直接输入。
    confirmed_records = sorted(
        (r for r in progress.checkins if r.counts), key=lambda r: r.event_id
    )
    for record in confirmed_records:
        confirm_inputs = tuple(
            f"evt:{cid}" for cid in confirming.get(record.event_id, ())
        )
        nodes.append(
            _make_node(
                node_id=f"evt:{record.event_id}",
                kind=KIND_EVENT,
                source_version=source_version,
                inputs=confirm_inputs,
                attributes={
                    "event_id": record.event_id,
                    "event_type": EventType.CHECKIN.value,
                    "student_id": record.student_id,
                    "activity_id": record.activity_id,
                    "activity_type": record.activity_type,
                    "status": record.status.value,
                    "check_in_at_utc": _iso_z(record.start_utc),
                    "check_out_at_utc": _iso_z(record.end_utc),
                    "raw_seconds": record.seconds,
                },
            )
        )

    # 请假修正事件节点。
    for adjustment in progress.adjustments:
        nodes.append(
            _make_node(
                node_id=f"evt:{adjustment.event_id}",
                kind=KIND_EVENT,
                source_version=source_version,
                inputs=(),
                attributes={
                    "event_id": adjustment.event_id,
                    "event_type": EventType.LEAVE_CORRECTION.value,
                    "student_id": adjustment.student_id,
                    "adjustment_seconds": adjustment.seconds,
                    "reason": adjustment.reason,
                },
            )
        )

    # 合并区间节点：重叠或相接的已确认签到并为一个区间。
    intervals = merge_intervals(
        [(r.start_utc, r.end_utc) for r in confirmed_records]
    )
    merge_ids: list[str] = []
    for start, end in intervals:
        contributors = sorted(
            r.event_id
            for r in confirmed_records
            if start <= r.start_utc and r.end_utc <= end
        )
        merge_id = "mrg:" + _sha256_text(
            _canonical(
                {
                    "student_id": student_id,
                    "start_utc": _iso_z(start),
                    "end_utc": _iso_z(end),
                    "contributors": contributors,
                }
            )
        )[:16]
        merge_ids.append(merge_id)
        merge_inputs = tuple(f"evt:{eid}" for eid in contributors)
        nodes.append(
            _make_node(
                node_id=merge_id,
                kind=KIND_MERGED_INTERVAL,
                source_version=source_version,
                inputs=merge_inputs,
                attributes={
                    "student_id": student_id,
                    "start_utc": _iso_z(start),
                    "end_utc": _iso_z(end),
                    "seconds": elapsed_seconds(start, end),
                },
            )
        )
        for event_id in contributors:
            edges.append(
                ExplanationEdge(f"evt:{event_id}", merge_id, EDGE_CONTRIBUTES)
            )

    # 教学日节点：合并区间按培养方案时区切分到教学日。
    day_seconds: dict[str, int] = {}
    day_inputs: dict[str, set[str]] = {}
    for (start, end), merge_id in zip(intervals, merge_ids):
        for day, seg_start, seg_end in split_by_academic_day(
            start, end, timezone_name
        ):
            key = day.isoformat()
            day_seconds[key] = day_seconds.get(key, 0) + elapsed_seconds(
                seg_start, seg_end
            )
            day_inputs.setdefault(key, set()).add(merge_id)
    day_ids: list[str] = []
    for key in sorted(day_seconds):
        day_id = f"day:{student_id}:{key}"
        day_ids.append(day_id)
        inputs = tuple(sorted(day_inputs[key]))
        nodes.append(
            _make_node(
                node_id=day_id,
                kind=KIND_ACADEMIC_DAY,
                source_version=source_version,
                inputs=inputs,
                attributes={
                    "student_id": student_id,
                    "academic_day": key,
                    "seconds": day_seconds[key],
                },
            )
        )
        for merge_id in inputs:
            edges.append(ExplanationEdge(merge_id, day_id, EDGE_SPLITS_INTO))

    # 总学时节点：合并区间求和 + 修正累加，负值钳零。
    total_id = f"sum:{student_id}"
    raw_total = progress.confirmed_seconds + progress.adjustment_seconds
    adjustment_ids = [f"evt:{a.event_id}" for a in progress.adjustments]
    total_inputs = tuple(sorted(merge_ids + adjustment_ids))
    nodes.append(
        _make_node(
            node_id=total_id,
            kind=KIND_TOTAL,
            source_version=source_version,
            inputs=total_inputs,
            attributes={
                "student_id": student_id,
                "confirmed_seconds": progress.confirmed_seconds,
                "adjustment_seconds": progress.adjustment_seconds,
                "raw_total_seconds": raw_total,
                "total_seconds": progress.total_seconds,
                "clamped": raw_total < 0,
            },
        )
    )
    for merge_id in merge_ids:
        edges.append(ExplanationEdge(merge_id, total_id, EDGE_AGGREGATES))
    for adjustment_id in adjustment_ids:
        edges.append(ExplanationEdge(adjustment_id, total_id, EDGE_ADJUSTS))

    # 进度根节点：课时换算与达标判定。
    progress_id = f"prog:{student_id}"
    progress_inputs = tuple(sorted([total_id, *day_ids]))
    nodes.append(
        _make_node(
            node_id=progress_id,
            kind=KIND_PROGRESS,
            source_version=source_version,
            inputs=progress_inputs,
            attributes={
                "student_id": student_id,
                "required_seconds": required_seconds,
                "total_seconds": progress.total_seconds,
                "lesson_units": progress.lesson_units,
                "meets_requirement": progress.meets_requirement,
            },
        )
    )
    edges.append(ExplanationEdge(total_id, progress_id, EDGE_SUMMARIZES))
    for day_id in day_ids:
        edges.append(ExplanationEdge(day_id, progress_id, EDGE_SUMMARIZES))

    ordered_nodes = tuple(sorted(nodes, key=lambda n: n.node_id))
    ordered_edges = tuple(
        sorted(set(edges), key=lambda e: (e.source, e.target, e.kind))
    )
    digest = compute_graph_digest(GRAPH_VERSION, ordered_nodes, ordered_edges)
    return ExplanationGraph(
        graph_version=GRAPH_VERSION,
        plan_version=plan_version,
        student_id=student_id,
        rule_version=rule_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        event_cutoff_id=up_to_event_id,
        nodes=ordered_nodes,
        edges=ordered_edges,
        digest=digest,
    )


def node_to_dict(
    node: ExplanationNode, masked_fields: Iterable[str] = ()
) -> dict[str, Any]:
    """序列化节点；被裁剪字段替换为占位符，标识与指纹保持不变。"""
    masked = set(masked_fields)
    attributes = {
        key: (REDACTED_VALUE if key in masked else value)
        for key, value in node.attributes.items()
    }
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "source_version": dict(node.source_version),
        "inputs": list(node.inputs),
        "input_fingerprint": node.input_fingerprint,
        "attributes": attributes,
    }


def graph_to_dict(
    graph: ExplanationGraph, viewer_role: str = "staff"
) -> dict[str, Any]:
    """按角色序列化图；未知角色抛 ValueError。"""
    if viewer_role not in VIEWER_FIELD_POLICY:
        raise ValueError(f"unknown viewer role '{viewer_role}'")
    masked = VIEWER_FIELD_POLICY[viewer_role]
    return {
        "graph_version": graph.graph_version,
        "plan_version": graph.plan_version,
        "student_id": graph.student_id,
        "rule_version": graph.rule_version,
        "timezone": graph.timezone,
        "required_seconds": graph.required_seconds,
        "event_cutoff_id": graph.event_cutoff_id,
        "viewer_role": viewer_role,
        "digest": graph.digest,
        "redacted_fields": sorted(masked),
        "nodes": [node_to_dict(n, masked) for n in graph.nodes],
        "edges": [
            {"source": e.source, "target": e.target, "kind": e.kind}
            for e in graph.edges
        ],
    }


def trace_node(graph: ExplanationGraph, node_id: str) -> dict[str, Any] | None:
    """追溯节点：返回该节点、全部祖先（因果来源）与全部后代（受影响节点）。"""
    node_map = {n.node_id: n for n in graph.nodes}
    node = node_map.get(node_id)
    if node is None:
        return None

    ancestors: dict[str, ExplanationNode] = {}
    stack = list(node.inputs)
    while stack:
        current = stack.pop()
        if current in ancestors or current == node_id:
            continue
        source = node_map.get(current)
        if source is None:
            continue
        ancestors[current] = source
        stack.extend(source.inputs)

    children: dict[str, list[str]] = {}
    for edge in graph.edges:
        children.setdefault(edge.source, []).append(edge.target)
    descendants: dict[str, ExplanationNode] = {}
    stack = list(children.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in descendants or current == node_id:
            continue
        target = node_map.get(current)
        if target is None:
            continue
        descendants[current] = target
        stack.extend(children.get(current, ()))

    return {
        "node": node,
        "ancestors": sorted(ancestors.values(), key=lambda n: n.node_id),
        "descendants": sorted(descendants.values(), key=lambda n: n.node_id),
    }


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def verify_graph_dict(data: dict[str, Any]) -> dict[str, Any]:
    """核验（未裁剪的）图字典的内部一致性：指纹、边端点、输入与摘要。"""
    checks: list[dict[str, Any]] = []
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []

    ids = [n.get("node_id") for n in nodes]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    checks.append(
        _check(
            "unique_node_ids",
            not duplicates,
            "node ids are unique"
            if not duplicates
            else f"duplicate node ids: {duplicates}",
        )
    )

    bad_fingerprints = []
    for node in nodes:
        expected = compute_node_fingerprint(
            node["node_id"],
            node["kind"],
            node["source_version"],
            node.get("inputs", []),
            node["attributes"],
        )
        if expected != node.get("input_fingerprint"):
            bad_fingerprints.append(node["node_id"])
    checks.append(
        _check(
            "node_fingerprints",
            not bad_fingerprints,
            "all node fingerprints recompute from their inputs"
            if not bad_fingerprints
            else f"fingerprint mismatch: {sorted(bad_fingerprints)}",
        )
    )

    id_set = set(ids)
    endpoints = {e["source"] for e in edges} | {e["target"] for e in edges}
    dangling = sorted(endpoints - id_set)
    checks.append(
        _check(
            "edge_endpoints",
            not dangling,
            "every edge endpoint resolves to a node"
            if not dangling
            else f"edges reference unknown nodes: {dangling}",
        )
    )

    incoming: dict[str, set[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["target"], set()).add(edge["source"])
    mismatched = sorted(
        node["node_id"]
        for node in nodes
        if set(node.get("inputs", [])) != incoming.get(node["node_id"], set())
    )
    checks.append(
        _check(
            "inputs_match_edges",
            not mismatched,
            "node inputs agree with the edge set"
            if not mismatched
            else f"inputs/edges mismatch: {mismatched}",
        )
    )

    edge_rows = sorted([e["source"], e["target"], e["kind"]] for e in edges)
    recomputed = _sha256_text(
        _canonical(
            {
                "graph_version": data.get("graph_version"),
                "nodes": {
                    n["node_id"]: n.get("input_fingerprint") for n in nodes
                },
                "edges": edge_rows,
            }
        )
    )
    digest_ok = recomputed == data.get("digest")
    checks.append(
        _check(
            "graph_digest",
            digest_ok,
            "graph digest recomputes from nodes and edges"
            if digest_ok
            else "graph digest mismatch",
        )
    )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "graph_digest": data.get("digest"),
    }


def export_package(
    graph_dict: dict[str, Any],
    *,
    graph_digest: str,
    viewer_role: str,
    purpose: str,
    freeze_id: str | None,
) -> dict[str, Any]:
    """把（可能已裁剪的）图字典包装为自描述的受控导出包。

    graph_digest 是完整图的摘要，与裁剪无关；content_digest 覆盖实际
    导出的内容，接收方可据此核验所收包未被篡改。
    """
    content_digest = _sha256_text(_canonical(graph_dict))
    manifest = {
        "package_type": "explanation-export",
        "package_version": GRAPH_VERSION,
        "plan_version": graph_dict.get("plan_version"),
        "freeze_id": freeze_id,
        "student_id": graph_dict.get("student_id"),
        "viewer_role": viewer_role,
        "purpose": purpose,
        "graph_digest": graph_digest,
        "content_digest": content_digest,
        "node_count": len(graph_dict.get("nodes") or []),
        "edge_count": len(graph_dict.get("edges") or []),
        "redacted_fields": sorted(graph_dict.get("redacted_fields") or []),
    }
    return {"manifest": manifest, "graph": graph_dict}
