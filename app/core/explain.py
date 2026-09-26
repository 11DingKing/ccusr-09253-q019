"""服务端业务模块。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

# 派生节点的来源版本：重放规则升级时递增，旧冻结图仍可按其原始版本核验。
RULE_VERSION = "hours-rules/v1"


class NodeKind(StrEnum):
    SOURCE_EVENT = "source_event"
    CHECKIN = "checkin_interval"
    CONFIRMATION = "status_confirmation"
    ADJUSTMENT = "adjustment"
    INTERVAL_MERGE = "interval_merge"
    ADJUSTMENT_SUM = "adjustment_sum"
    TOTAL = "total"
    DAILY = "daily_total"
    RESULT = "student_result"


class ViewerRole(StrEnum):
    AUDITOR = "auditor"
    STUDENT = "student"
    STAFF = "staff"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_fingerprint(value: Any) -> str:
    """执行确定性的业务处理。"""
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExplanationNode:
    """解释图节点：标识稳定，指纹覆盖来源版本与全部因果输入。"""

    node_id: str
    kind: NodeKind
    student_id: str
    source_version: str
    inputs: tuple[str, ...]
    attributes: dict[str, Any]

    @property
    def input_fingerprint(self) -> str:
        return content_fingerprint(
            {
                "node_id": self.node_id,
                "kind": self.kind.value,
                "source_version": self.source_version,
                "inputs": list(self.inputs),
                "attributes": self.attributes,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "student_id": self.student_id,
            "source_version": self.source_version,
            "input_fingerprint": self.input_fingerprint,
            "inputs": list(self.inputs),
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExplanationNode":
        return cls(
            node_id=data["node_id"],
            kind=NodeKind(data["kind"]),
            student_id=data["student_id"],
            source_version=data["source_version"],
            inputs=tuple(sorted(data.get("inputs", []))),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass(frozen=True)
class ExplanationGraph:
    """一次重放得到的完整因果解释图，可序列化并随冻结持久化。"""

    plan_version: str
    timezone: str
    required_seconds: int
    rule_version: str
    event_cutoff_id: str | None
    nodes: dict[str, ExplanationNode]

    @property
    def digest(self) -> str:
        """执行确定性的业务处理。"""
        return content_fingerprint(
            {
                "plan_version": self.plan_version,
                "timezone": self.timezone,
                "required_seconds": self.required_seconds,
                "rule_version": self.rule_version,
                "event_cutoff_id": self.event_cutoff_id,
                "nodes": sorted(
                    node.input_fingerprint for node in self.nodes.values()
                ),
            }
        )

    @staticmethod
    def root_for_student(student_id: str) -> str:
        return f"result:{student_id}"

    def sorted_nodes(self) -> list[ExplanationNode]:
        return [self.nodes[key] for key in sorted(self.nodes)]

    def ancestors_of(self, node_id: str) -> dict[str, ExplanationNode]:
        """执行确定性的业务处理。"""
        closure: dict[str, ExplanationNode] = {}
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in closure:
                continue
            node = self.nodes.get(current)
            if node is None:
                continue
            closure[current] = node
            stack.extend(node.inputs)
        return closure

    def subgraph_for_student(self, student_id: str) -> "ExplanationGraph | None":
        """按学生抽取最小因果子图：仅包含其结果节点的全部因果祖先。"""
        root = self.root_for_student(student_id)
        if root not in self.nodes:
            return None
        return ExplanationGraph(
            plan_version=self.plan_version,
            timezone=self.timezone,
            required_seconds=self.required_seconds,
            rule_version=self.rule_version,
            event_cutoff_id=self.event_cutoff_id,
            nodes=self.ancestors_of(root),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "timezone": self.timezone,
            "required_seconds": self.required_seconds,
            "rule_version": self.rule_version,
            "event_cutoff_id": self.event_cutoff_id,
            "graph_digest": self.digest,
            "node_count": len(self.nodes),
            "nodes": [node.to_dict() for node in self.sorted_nodes()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExplanationGraph":
        nodes = [ExplanationNode.from_dict(raw) for raw in data.get("nodes", [])]
        return cls(
            plan_version=data["plan_version"],
            timezone=data["timezone"],
            required_seconds=data["required_seconds"],
            rule_version=data["rule_version"],
            event_cutoff_id=data.get("event_cutoff_id"),
            nodes={node.node_id: node for node in nodes},
        )


def verify_graph_document(document: dict[str, Any]) -> list[str]:
    """核验序列化解释图的完整性，返回发现的问题列表（空列表表示通过）。"""
    problems: list[str] = []
    raw_nodes = document.get("nodes", [])
    if not isinstance(raw_nodes, list):
        return ["解释图文档的 nodes 字段不是列表"]

    ids: set[str] = set()
    for raw in raw_nodes:
        node_id = raw.get("node_id") if isinstance(raw, dict) else None
        if not isinstance(node_id, str) or not node_id:
            problems.append("发现缺少 node_id 的节点")
            continue
        if node_id in ids:
            problems.append(f"节点 '{node_id}' 重复定义")
        ids.add(node_id)

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        try:
            node = ExplanationNode.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"节点 '{node_id}' 结构无效: {exc}")
            continue
        if raw.get("input_fingerprint") != node.input_fingerprint:
            problems.append(f"节点 '{node_id}' 的输入指纹与内容不一致")
        for ref in node.inputs:
            if ref not in ids:
                problems.append(f"节点 '{node_id}' 引用了不存在的输入 '{ref}'")

    try:
        graph = ExplanationGraph.from_dict(document)
    except (KeyError, TypeError, ValueError) as exc:
        problems.append(f"解释图文档结构无效: {exc}")
        return problems
    if document.get("graph_digest") != graph.digest:
        problems.append("解释图摘要与节点内容不一致")
    node_count = document.get("node_count")
    if node_count is not None and node_count != len(raw_nodes):
        problems.append("节点计数与实际节点数不一致")
    return problems


# 字段可见级别：public 对所有角色开放，internal 对学生与工作人员开放，
# restricted 仅工作人员可见。未列出的属性一律按 restricted 处理。
_FIELD_LEVELS: dict[NodeKind, dict[str, str]] = {
    NodeKind.SOURCE_EVENT: {
        "event_id": "public",
        "event_type": "public",
        "plan_version": "public",
        "payload": "restricted",
    },
    NodeKind.CHECKIN: {
        "activity_id": "internal",
        "activity_type": "internal",
        "start_utc": "internal",
        "end_utc": "internal",
        "raw_seconds": "public",
        "initial_status": "public",
        "effective_status": "public",
        "counts": "public",
    },
    NodeKind.CONFIRMATION: {
        "checkin_event_id": "public",
    },
    NodeKind.ADJUSTMENT: {
        "seconds": "public",
        "reason": "restricted",
    },
    NodeKind.INTERVAL_MERGE: {
        "bucket": "public",
        "seconds": "public",
        "contributor_count": "public",
        "merged_intervals": "internal",
    },
    NodeKind.ADJUSTMENT_SUM: {
        "seconds": "public",
        "contributor_count": "public",
    },
    NodeKind.TOTAL: {
        "confirmed_seconds": "public",
        "adjustment_seconds": "public",
        "raw_total_seconds": "public",
        "clamped": "public",
        "total_seconds": "public",
    },
    NodeKind.DAILY: {
        "academic_day": "public",
        "seconds": "public",
    },
    NodeKind.RESULT: {
        "confirmed_seconds": "public",
        "pending_seconds": "public",
        "adjustment_seconds": "public",
        "total_seconds": "public",
        "lesson_units": "public",
        "pending_lesson_units": "public",
        "meets_requirement": "public",
        "required_seconds": "public",
        "plan_version": "public",
        "timezone": "internal",
    },
}

_LEVEL_RANK = {"public": 0, "internal": 1, "restricted": 2}
_ROLE_RANK = {
    ViewerRole.AUDITOR: 0,
    ViewerRole.STUDENT: 1,
    ViewerRole.STAFF: 2,
}


def trim_attributes(
    kind: NodeKind, attributes: dict[str, Any], role: ViewerRole
) -> dict[str, Any]:
    """按隐私级别裁剪字段；节点标识、指纹与因果边保持不变。"""
    role = ViewerRole(role)
    allowed = _ROLE_RANK[role]
    levels = _FIELD_LEVELS.get(kind, {})
    return {
        key: value
        for key, value in attributes.items()
        if _LEVEL_RANK[levels.get(key, "restricted")] <= allowed
    }


def trim_node_dict(node: dict[str, Any], role: ViewerRole) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    trimmed = dict(node)
    trimmed["attributes"] = trim_attributes(
        NodeKind(node["kind"]), dict(node.get("attributes", {})), role
    )
    return trimmed


class ExplanationBuilder:
    """在重放过程中累积解释图节点，仅记录真正改变状态的因果步骤。"""

    def __init__(
        self,
        *,
        plan_version: str,
        timezone_name: str,
        required_seconds: int,
        rule_version: str = RULE_VERSION,
        event_cutoff_id: str | None = None,
    ) -> None:
        self._plan_version = plan_version
        self._timezone_name = timezone_name
        self._required_seconds = required_seconds
        self._rule_version = rule_version
        self._event_cutoff_id = event_cutoff_id
        self._nodes: dict[str, ExplanationNode] = {}
        self._checkins: dict[str, dict[str, Any]] = {}
        self._checkins_by_student: dict[str, list[str]] = {}
        self._adjustments_by_student: dict[str, list[str]] = {}

    def _add_event_node(
        self,
        *,
        event_id: str,
        student_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        node_id = f"event:{event_id}"
        if node_id not in self._nodes:
            self._nodes[node_id] = ExplanationNode(
                node_id=node_id,
                kind=NodeKind.SOURCE_EVENT,
                student_id=student_id,
                source_version=self._plan_version,
                inputs=(),
                attributes={
                    "event_id": event_id,
                    "event_type": event_type,
                    "plan_version": self._plan_version,
                    "payload": dict(payload),
                },
            )
        return node_id

    def add_checkin(
        self,
        *,
        event_id: str,
        student_id: str,
        event_type: str,
        payload: dict[str, Any],
        activity_id: str,
        activity_type: str,
        start_utc: str,
        end_utc: str,
        raw_seconds: int,
        initial_status: str,
    ) -> None:
        event_node = self._add_event_node(
            event_id=event_id,
            student_id=student_id,
            event_type=event_type,
            payload=payload,
        )
        self._checkins[event_id] = {
            "event_node": event_node,
            "activity_id": activity_id,
            "activity_type": activity_type,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "raw_seconds": raw_seconds,
            "initial_status": initial_status,
            "confirms": [],
        }
        self._checkins_by_student.setdefault(student_id, []).append(event_id)

    def add_confirmation(
        self,
        *,
        event_id: str,
        student_id: str,
        event_type: str,
        payload: dict[str, Any],
        checkin_event_id: str,
    ) -> None:
        event_node = self._add_event_node(
            event_id=event_id,
            student_id=student_id,
            event_type=event_type,
            payload=payload,
        )
        node_id = f"confirm:{event_id}"
        self._nodes[node_id] = ExplanationNode(
            node_id=node_id,
            kind=NodeKind.CONFIRMATION,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=(event_node,),
            attributes={"checkin_event_id": checkin_event_id},
        )
        acc = self._checkins.get(checkin_event_id)
        if acc is not None:
            acc["confirms"].append(node_id)

    def add_adjustment(
        self,
        *,
        event_id: str,
        student_id: str,
        event_type: str,
        payload: dict[str, Any],
        seconds: int,
        reason: str,
    ) -> None:
        event_node = self._add_event_node(
            event_id=event_id,
            student_id=student_id,
            event_type=event_type,
            payload=payload,
        )
        node_id = f"adjustment:{event_id}"
        self._nodes[node_id] = ExplanationNode(
            node_id=node_id,
            kind=NodeKind.ADJUSTMENT,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=(event_node,),
            attributes={"seconds": seconds, "reason": reason},
        )
        self._adjustments_by_student.setdefault(student_id, []).append(event_id)

    def close_student(
        self,
        *,
        student_id: str,
        effective_statuses: dict[str, str],
        confirmed_checkin_ids: list[str],
        pending_checkin_ids: list[str],
        confirmed_intervals: list[tuple[str, str]],
        confirmed_seconds: int,
        pending_intervals: list[tuple[str, str]],
        pending_seconds: int,
        adjustment_seconds: int,
        raw_total_seconds: int,
        clamped: bool,
        total_seconds: int,
        lesson_units: int,
        pending_lesson_units: int,
        meets_requirement: bool,
        daily: list[tuple[str, int]],
    ) -> None:
        """执行确定性的业务处理。"""
        for event_id in self._checkins_by_student.get(student_id, []):
            acc = self._checkins[event_id]
            effective = effective_statuses.get(event_id, acc["initial_status"])
            self._nodes[f"checkin:{event_id}"] = ExplanationNode(
                node_id=f"checkin:{event_id}",
                kind=NodeKind.CHECKIN,
                student_id=student_id,
                source_version=self._rule_version,
                inputs=tuple(sorted([acc["event_node"], *acc["confirms"]])),
                attributes={
                    "activity_id": acc["activity_id"],
                    "activity_type": acc["activity_type"],
                    "start_utc": acc["start_utc"],
                    "end_utc": acc["end_utc"],
                    "raw_seconds": acc["raw_seconds"],
                    "initial_status": acc["initial_status"],
                    "effective_status": effective,
                    "counts": effective == "CONFIRMED",
                },
            )

        merge_confirmed_id = f"merge:{student_id}:confirmed"
        self._nodes[merge_confirmed_id] = ExplanationNode(
            node_id=merge_confirmed_id,
            kind=NodeKind.INTERVAL_MERGE,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=tuple(sorted(f"checkin:{eid}" for eid in confirmed_checkin_ids)),
            attributes={
                "bucket": "confirmed",
                "seconds": confirmed_seconds,
                "contributor_count": len(confirmed_checkin_ids),
                "merged_intervals": [list(pair) for pair in confirmed_intervals],
            },
        )
        merge_pending_id = f"merge:{student_id}:pending"
        self._nodes[merge_pending_id] = ExplanationNode(
            node_id=merge_pending_id,
            kind=NodeKind.INTERVAL_MERGE,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=tuple(sorted(f"checkin:{eid}" for eid in pending_checkin_ids)),
            attributes={
                "bucket": "pending",
                "seconds": pending_seconds,
                "contributor_count": len(pending_checkin_ids),
                "merged_intervals": [list(pair) for pair in pending_intervals],
            },
        )

        adjustment_ids = self._adjustments_by_student.get(student_id, [])
        adjustments_id = f"adjustments:{student_id}"
        self._nodes[adjustments_id] = ExplanationNode(
            node_id=adjustments_id,
            kind=NodeKind.ADJUSTMENT_SUM,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=tuple(sorted(f"adjustment:{eid}" for eid in adjustment_ids)),
            attributes={
                "seconds": adjustment_seconds,
                "contributor_count": len(adjustment_ids),
            },
        )

        total_id = f"total:{student_id}"
        self._nodes[total_id] = ExplanationNode(
            node_id=total_id,
            kind=NodeKind.TOTAL,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=tuple(sorted([merge_confirmed_id, adjustments_id])),
            attributes={
                "confirmed_seconds": confirmed_seconds,
                "adjustment_seconds": adjustment_seconds,
                "raw_total_seconds": raw_total_seconds,
                "clamped": clamped,
                "total_seconds": total_seconds,
            },
        )

        daily_ids: list[str] = []
        for day, seconds in daily:
            daily_id = f"daily:{student_id}:{day}"
            daily_ids.append(daily_id)
            self._nodes[daily_id] = ExplanationNode(
                node_id=daily_id,
                kind=NodeKind.DAILY,
                student_id=student_id,
                source_version=self._rule_version,
                inputs=(merge_confirmed_id,),
                attributes={"academic_day": day, "seconds": seconds},
            )

        self._nodes[f"result:{student_id}"] = ExplanationNode(
            node_id=f"result:{student_id}",
            kind=NodeKind.RESULT,
            student_id=student_id,
            source_version=self._rule_version,
            inputs=tuple(sorted([total_id, merge_pending_id, *daily_ids])),
            attributes={
                "confirmed_seconds": confirmed_seconds,
                "pending_seconds": pending_seconds,
                "adjustment_seconds": adjustment_seconds,
                "total_seconds": total_seconds,
                "lesson_units": lesson_units,
                "pending_lesson_units": pending_lesson_units,
                "meets_requirement": meets_requirement,
                "required_seconds": self._required_seconds,
                "plan_version": self._plan_version,
                "timezone": self._timezone_name,
            },
        )

    def build(self) -> ExplanationGraph:
        return ExplanationGraph(
            plan_version=self._plan_version,
            timezone=self._timezone_name,
            required_seconds=self._required_seconds,
            rule_version=self._rule_version,
            event_cutoff_id=self._event_cutoff_id,
            nodes=dict(self._nodes),
        )
