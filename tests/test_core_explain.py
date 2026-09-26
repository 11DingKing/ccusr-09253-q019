"""解释图核心：重叠签到、负向修正、规则版本与字段裁剪。"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from app.core.explain import (
    EDGE_CONFIRMS,
    EDGE_CONTRIBUTES,
    KIND_ACADEMIC_DAY,
    KIND_EVENT,
    KIND_MERGED_INTERVAL,
    KIND_PROGRESS,
    KIND_TOTAL,
    REDACTED_VALUE,
    build_student_explanation,
    graph_to_dict,
    trace_node,
    verify_graph_dict,
)
from app.core.replay import RULE_VERSION, Event, EventType


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


def _confirm(eid: str, student: str, checkin_event_id: str) -> Event:
    return _event(
        eid,
        EventType.MENTOR_CONFIRM,
        student,
        {"checkin_event_id": checkin_event_id},
    )


def _correction(eid: str, student: str, seconds: int, reason: str) -> Event:
    return _event(
        eid,
        EventType.LEAVE_CORRECTION,
        student,
        {"adjustment_seconds": seconds, "reason": reason},
    )


def _build(events, student="S1", **kwargs):
    params = dict(
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=10800,
    )
    params.update(kwargs)
    return build_student_explanation(events, student, **params)


def _node_map(graph):
    return {n.node_id: n for n in graph.nodes}


def test_overlapping_checkins_share_one_merge_node():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00"),
    ]
    graph = _build(events)
    assert graph is not None
    nodes = _node_map(graph)

    merges = [n for n in graph.nodes if n.kind == KIND_MERGED_INTERVAL]
    assert len(merges) == 1
    merge = merges[0]
    assert merge.attributes["seconds"] == 3 * 3600
    assert merge.attributes["start_utc"] == "2024-03-15T00:00:00Z"
    assert merge.attributes["end_utc"] == "2024-03-15T03:00:00Z"
    assert set(merge.inputs) == {"evt:E-01", "evt:E-02"}

    total = nodes["sum:S1"]
    assert total.kind == KIND_TOTAL
    assert total.attributes["confirmed_seconds"] == 3 * 3600
    assert total.attributes["total_seconds"] == 3 * 3600
    assert total.attributes["clamped"] is False

    days = [n for n in graph.nodes if n.kind == KIND_ACADEMIC_DAY]
    assert [d.attributes["academic_day"] for d in days] == ["2024-03-15"]
    assert days[0].attributes["seconds"] == 3 * 3600

    contribute = [e for e in graph.edges if e.kind == EDGE_CONTRIBUTES]
    assert {e.source for e in contribute} == {"evt:E-01", "evt:E-02"}
    assert all(e.target == merge.node_id for e in contribute)


def test_same_inputs_yield_identical_graph_regardless_of_order():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -900, "late arrival"),
        _checkin("E-03", "S1", "2024-03-15T13:00:00+08:00", "2024-03-15T15:00:00+08:00"),
        _confirm("E-04", "S1", "E-03"),
    ]
    first = _build(events)
    second = _build(list(reversed(events)))
    assert first is not None and second is not None
    assert first.digest == second.digest
    assert graph_to_dict(first) == graph_to_dict(second)


def test_negative_correction_clamps_total_and_stays_traceable():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", -7200, "unapproved absence"),
    ]
    graph = _build(events, required_seconds=3600)
    nodes = _node_map(graph)

    total = nodes["sum:S1"]
    assert total.attributes["confirmed_seconds"] == 3600
    assert total.attributes["adjustment_seconds"] == -7200
    assert total.attributes["raw_total_seconds"] == 3600 - 7200
    assert total.attributes["total_seconds"] == 0
    assert total.attributes["clamped"] is True
    assert "evt:E-02" in total.inputs

    correction = nodes["evt:E-02"]
    assert correction.attributes["event_type"] == "leave_correction"
    assert correction.attributes["adjustment_seconds"] == -7200
    assert correction.attributes["reason"] == "unapproved absence"

    progress = nodes["prog:S1"]
    assert progress.attributes["lesson_units"] == 0
    assert progress.attributes["meets_requirement"] is False

    traced = trace_node(graph, "prog:S1")
    ancestor_ids = {n.node_id for n in traced["ancestors"]}
    assert "evt:E-02" in ancestor_ids
    assert "sum:S1" in ancestor_ids


def test_rule_version_marks_source_without_changing_identity():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", 600, "make-up"),
    ]
    v1 = _build(events)
    v2 = _build(events, rule_version="hours-rules/v2")
    assert v1 is not None and v2 is not None

    # 节点标识与图结构不随规则版本变化。
    assert [n.node_id for n in v1.nodes] == [n.node_id for n in v2.nodes]
    assert v1.edges == v2.edges

    # 来源版本与指纹、图摘要随规则版本变化。
    assert all(
        n.source_version
        == {"plan_version": "P1", "rule_version": RULE_VERSION}
        for n in v1.nodes
    )
    assert all(
        n.source_version["rule_version"] == "hours-rules/v2" for n in v2.nodes
    )
    fingerprints_v1 = {n.node_id: n.input_fingerprint for n in v1.nodes}
    fingerprints_v2 = {n.node_id: n.input_fingerprint for n in v2.nodes}
    assert all(
        fingerprints_v1[node_id] != fingerprints_v2[node_id]
        for node_id in fingerprints_v1
    )
    assert v1.digest != v2.digest


def test_field_redaction_never_changes_node_identity():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -900, "personal matters"),
    ]
    graph = _build(events)
    staff = graph_to_dict(graph, "staff")
    mentor = graph_to_dict(graph, "mentor")
    student = graph_to_dict(graph, "student")

    # 节点标识、指纹、边与图摘要在所有角色下完全一致。
    for view in (mentor, student):
        assert [n["node_id"] for n in view["nodes"]] == [
            n["node_id"] for n in staff["nodes"]
        ]
        assert [e for e in view["edges"]] == staff["edges"]
        assert view["digest"] == staff["digest"]
        assert {
            n["node_id"]: n["input_fingerprint"] for n in view["nodes"]
        } == {n["node_id"]: n["input_fingerprint"] for n in staff["nodes"]}

    # 只有被策略裁剪的字段不同。
    staff_reason = next(
        n for n in staff["nodes"] if n["node_id"] == "evt:E-02"
    )["attributes"]["reason"]
    mentor_reason = next(
        n for n in mentor["nodes"] if n["node_id"] == "evt:E-02"
    )["attributes"]["reason"]
    assert staff_reason == "personal matters"
    assert mentor_reason == REDACTED_VALUE
    assert mentor["redacted_fields"] == ["reason"]
    assert staff["redacted_fields"] == []

    with pytest.raises(ValueError):
        graph_to_dict(graph, "anonymous")


def test_mentor_confirm_is_causal_for_internship_checkin():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _confirm("E-02", "S1", "E-01"),
    ]
    graph = _build(events)
    nodes = _node_map(graph)

    checkin = nodes["evt:E-01"]
    assert checkin.inputs == ("evt:E-02",)
    assert checkin.attributes["status"] == "CONFIRMED"

    confirm = nodes["evt:E-02"]
    assert confirm.attributes["event_type"] == "mentor_confirm"
    assert confirm.attributes["checkin_event_id"] == "E-01"
    assert confirm.attributes["applied"] is True

    assert any(
        e.kind == EDGE_CONFIRMS and e.source == "evt:E-02" and e.target == "evt:E-01"
        for e in graph.edges
    )
    assert nodes["sum:S1"].attributes["confirmed_seconds"] == 4 * 3600


def test_unconfirmed_internship_checkin_is_not_causal():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
    ]
    graph = _build(events)
    nodes = _node_map(graph)
    # 待确认签到不计入学时，不进入最小因果子图。
    assert all(n.kind != KIND_EVENT for n in graph.nodes)
    total = nodes["sum:S1"]
    assert total.inputs == ()
    assert total.attributes["total_seconds"] == 0


def test_minimal_subgraph_excludes_other_students_and_ineffective_events():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-03", "S1", "E-99"),  # 目标不存在，确认不生效
        _correction("E-04", "S2", 600, "other student"),
    ]
    graph = _build(events)
    ids = {n.node_id for n in graph.nodes}
    assert "evt:E-01" in ids
    assert not any("E-02" in i or "E-03" in i or "E-04" in i for i in ids)
    # 其他学生的图互不影响。
    other = _build(events, student="S2")
    other_ids = {n.node_id for n in other.nodes}
    assert "evt:E-02" in other_ids
    assert "evt:E-04" in other_ids
    assert "evt:E-01" not in other_ids


def test_trace_node_returns_ancestors_and_descendants():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", 600, "make-up"),
    ]
    graph = _build(events)
    nodes = _node_map(graph)
    merge_id = next(n.node_id for n in graph.nodes if n.kind == KIND_MERGED_INTERVAL)

    traced = trace_node(graph, "evt:E-01")
    descendant_ids = {n.node_id for n in traced["descendants"]}
    assert {merge_id, "sum:S1", "prog:S1"} <= descendant_ids
    assert traced["ancestors"] == []

    traced_total = trace_node(graph, "sum:S1")
    ancestor_ids = {n.node_id for n in traced_total["ancestors"]}
    assert {"evt:E-01", "evt:E-02", merge_id} <= ancestor_ids
    assert {n.node_id for n in traced_total["descendants"]} == {"prog:S1"}

    assert trace_node(graph, "evt:UNKNOWN") is None
    assert nodes["prog:S1"].kind == KIND_PROGRESS


def test_verify_graph_dict_accepts_built_graph_and_detects_tampering():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -900, "late"),
    ]
    graph = _build(events)
    data = graph_to_dict(graph, "staff")

    report = verify_graph_dict(data)
    assert report["ok"] is True
    assert all(c["ok"] for c in report["checks"])
    assert report["node_count"] == len(graph.nodes)
    assert report["edge_count"] == len(graph.edges)

    tampered = copy.deepcopy(data)
    merge_entry = next(
        n for n in tampered["nodes"] if n["kind"] == KIND_MERGED_INTERVAL
    )
    merge_entry["attributes"]["seconds"] += 60
    report = verify_graph_dict(tampered)
    assert report["ok"] is False
    fingerprint_check = next(
        c for c in report["checks"] if c["name"] == "node_fingerprints"
    )
    assert fingerprint_check["ok"] is False

    # 删掉一条边也会破坏输入与边的一致性。
    tampered = copy.deepcopy(data)
    tampered["edges"] = tampered["edges"][1:]
    report = verify_graph_dict(tampered)
    assert report["ok"] is False
    broken = {c["name"] for c in report["checks"] if not c["ok"]}
    assert "inputs_match_edges" in broken
