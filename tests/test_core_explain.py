"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.explain import (
    RULE_VERSION,
    ExplanationGraph,
    NodeKind,
    ViewerRole,
    trim_node_dict,
    verify_graph_document,
)
from app.core.replay import Event, EventType, replay_explained


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    activity_id: str = "A1",
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    )


def _explain(events, **kwargs):
    params = {
        "plan_version": "P1",
        "timezone_name": "Asia/Shanghai",
        "required_seconds": 10800,
    }
    params.update(kwargs)
    return replay_explained(events, **params)


def test_overlapping_checkins_merge_into_single_interval_node():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    state, graph = _explain(events)

    merge = graph.nodes["merge:S1:confirmed"]
    assert merge.kind == NodeKind.INTERVAL_MERGE
    # 两条重叠签到合并为一个区间，学时按并集计算。
    assert merge.attributes["seconds"] == 10800
    assert merge.attributes["contributor_count"] == 2
    assert merge.attributes["merged_intervals"] == [
        ["2024-03-15T00:00:00Z", "2024-03-15T03:00:00Z"]
    ]
    # 合并节点因果上依赖两条签到节点。
    assert merge.inputs == ("checkin:E-01", "checkin:E-02")
    assert state.students["S1"].confirmed_seconds == 10800

    total = graph.nodes["total:S1"]
    assert set(total.inputs) == {"merge:S1:confirmed", "adjustments:S1"}
    assert total.attributes["total_seconds"] == 10800


def test_negative_correction_clamps_total_and_stays_traceable():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -7200, "reason": "unapproved absence"},
        ),
    ]
    state, graph = _explain(events)

    adjustment = graph.nodes["adjustment:E-02"]
    assert adjustment.attributes["seconds"] == -7200
    assert adjustment.inputs == ("event:E-02",)

    total = graph.nodes["total:S1"]
    assert total.attributes["confirmed_seconds"] == 3600
    assert total.attributes["adjustment_seconds"] == -7200
    assert total.attributes["raw_total_seconds"] == -3600
    assert total.attributes["clamped"] is True
    assert total.attributes["total_seconds"] == 0
    assert state.students["S1"].total_seconds == 0

    # 负向修正的因果链可以追溯到源事件节点。
    ancestors = graph.ancestors_of("total:S1")
    assert "adjustment:E-02" in ancestors
    assert "event:E-02" in ancestors
    assert ancestors["event:E-02"].attributes["payload"]["adjustment_seconds"] == -7200


def test_mentor_confirmation_links_checkin_to_confirm_event():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _event(
            "E-02",
            EventType.MENTOR_CONFIRM,
            "S1",
            {"checkin_event_id": "E-01"},
        ),
    ]
    _, graph = _explain(events)

    checkin = graph.nodes["checkin:E-01"]
    assert checkin.attributes["initial_status"] == "PENDING"
    assert checkin.attributes["effective_status"] == "CONFIRMED"
    assert "confirm:E-02" in checkin.inputs

    confirm = graph.nodes["confirm:E-02"]
    assert confirm.kind == NodeKind.CONFIRMATION
    assert confirm.inputs == ("event:E-02",)
    assert confirm.attributes["checkin_event_id"] == "E-01"


def test_nodes_carry_source_version_and_input_fingerprint():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    _, graph = _explain(events)

    for node in graph.nodes.values():
        assert node.source_version
        assert node.input_fingerprint
        # 事件节点以来源计划版本标记，派生节点以规则版本标记。
        if node.kind == NodeKind.SOURCE_EVENT:
            assert node.source_version == "P1"
        else:
            assert node.source_version == RULE_VERSION


def test_rule_version_bump_changes_fingerprint_but_not_node_id():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    _, graph_v1 = _explain(events, rule_version="hours-rules/v1")
    _, graph_v2 = _explain(events, rule_version="hours-rules/v2")

    # 节点标识与图结构不随规则版本变化。
    assert set(graph_v1.nodes) == set(graph_v2.nodes)
    assert graph_v1.digest != graph_v2.digest
    merge_v1 = graph_v1.nodes["merge:S1:confirmed"]
    merge_v2 = graph_v2.nodes["merge:S1:confirmed"]
    assert merge_v1.node_id == merge_v2.node_id
    assert merge_v1.source_version == "hours-rules/v1"
    assert merge_v2.source_version == "hours-rules/v2"
    assert merge_v1.input_fingerprint != merge_v2.input_fingerprint
    # 计算结果本身一致。
    assert (
        merge_v1.attributes["seconds"] == merge_v2.attributes["seconds"] == 7200
    )


def test_same_inputs_replay_to_identical_graph():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -900, "reason": "late"},
        ),
        _checkin("E-03", "S2", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    _, graph_a = _explain(events)
    _, graph_b = _explain(list(reversed(events)))
    _, graph_c = _explain(events)

    assert graph_a.digest == graph_b.digest == graph_c.digest
    assert graph_a.to_dict() == graph_b.to_dict() == graph_c.to_dict()


def test_minimal_causal_subgraph_excludes_other_students():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        _event(
            "E-03",
            EventType.LEAVE_CORRECTION,
            "S2",
            {"adjustment_seconds": 600, "reason": "make-up"},
        ),
    ]
    _, graph = _explain(events)

    sub = graph.subgraph_for_student("S1")
    assert sub is not None
    assert set(sub.nodes) < set(graph.nodes)
    assert all(node.student_id == "S1" for node in sub.nodes.values())
    # S1 的子图不含 S2 的任何事件、修正与汇总节点。
    assert not any("S2" in node_id for node_id in sub.nodes)
    assert "event:E-02" not in sub.nodes
    assert "adjustment:E-03" not in sub.nodes
    # 子图以 result 节点为根，覆盖到源事件。
    assert "result:S1" in sub.nodes
    assert "event:E-01" in sub.nodes

    assert graph.subgraph_for_student("NOBODY") is None


def test_field_trimming_hides_restricted_fields_but_keeps_identifiers():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": -900, "reason": "late arrival"},
        ),
    ]
    _, graph = _explain(events)

    for node in graph.nodes.values():
        full = node.to_dict()
        for role in (ViewerRole.AUDITOR, ViewerRole.STUDENT, ViewerRole.STAFF):
            trimmed = trim_node_dict(full, role)
            # 无论角色如何，节点标识、指纹与因果边都保持不变。
            assert trimmed["node_id"] == full["node_id"]
            assert trimmed["input_fingerprint"] == full["input_fingerprint"]
            assert trimmed["inputs"] == full["inputs"]
            assert trimmed["source_version"] == full["source_version"]

    event_node = graph.nodes["event:E-01"].to_dict()
    auditor_view = trim_node_dict(event_node, ViewerRole.AUDITOR)
    student_view = trim_node_dict(event_node, ViewerRole.STUDENT)
    staff_view = trim_node_dict(event_node, ViewerRole.STAFF)
    # 原始负载属于受限字段，仅工作人员可见。
    assert "payload" not in auditor_view["attributes"]
    assert "payload" not in student_view["attributes"]
    assert staff_view["attributes"]["payload"]["activity_id"] == "A1"
    # 公开字段对审计角色仍可见。
    assert auditor_view["attributes"]["event_id"] == "E-01"

    adjustment_node = graph.nodes["adjustment:E-02"].to_dict()
    auditor_adjustment = trim_node_dict(adjustment_node, ViewerRole.AUDITOR)
    staff_adjustment = trim_node_dict(adjustment_node, ViewerRole.STAFF)
    assert "reason" not in auditor_adjustment["attributes"]
    assert auditor_adjustment["attributes"]["seconds"] == -900
    assert staff_adjustment["attributes"]["reason"] == "late arrival"

    checkin_node = graph.nodes["checkin:E-01"].to_dict()
    auditor_checkin = trim_node_dict(checkin_node, ViewerRole.AUDITOR)
    student_checkin = trim_node_dict(checkin_node, ViewerRole.STUDENT)
    # 签到时间属于内部字段，学生可见、审计不可见。
    assert "start_utc" not in auditor_checkin["attributes"]
    assert student_checkin["attributes"]["start_utc"] == "2024-03-15T00:00:00Z"


def test_graph_document_verification_detects_tampering():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    _, graph = _explain(events)
    document = graph.to_dict()
    assert verify_graph_document(document) == []

    # 篡改节点属性但不更新指纹。
    tampered = graph.to_dict()
    for raw in tampered["nodes"]:
        if raw["node_id"] == "total:S1":
            raw["attributes"]["total_seconds"] = 999999
    problems = verify_graph_document(tampered)
    assert any("指纹" in p for p in problems)
    assert any("摘要" in p for p in problems)

    # 删除一个节点导致悬空引用。
    broken = graph.to_dict()
    broken["nodes"] = [
        raw for raw in broken["nodes"] if raw["node_id"] != "event:E-01"
    ]
    broken["node_count"] = len(broken["nodes"])
    problems = verify_graph_document(broken)
    assert any("不存在的输入" in p for p in problems)


def test_graph_roundtrip_serialization_is_stable():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _event(
            "E-02",
            EventType.LEAVE_CORRECTION,
            "S1",
            {"adjustment_seconds": 300, "reason": "make-up"},
        ),
    ]
    _, graph = _explain(events)
    restored = ExplanationGraph.from_dict(graph.to_dict())
    assert restored.digest == graph.digest
    assert restored.to_dict() == graph.to_dict()
    assert verify_graph_document(restored.to_dict()) == []
