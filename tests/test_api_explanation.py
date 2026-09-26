"""服务端业务模块。"""

from __future__ import annotations

from app import services
from app.core.replay import replay_explained
from app.models import Freeze
from app.repository import load_events
from tests.conftest import SHANGHAI_PLAN


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(eid, student, seconds, reason=""):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _seed_events(client, events):
    pv = SHANGHAI_PLAN["plan_version"]
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def test_explanation_query_returns_minimal_subgraph(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
            _checkin(
                "E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00"
            ),
            _checkin(
                "E-03", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"
            ),
            _correction("E-04", "S1", -900, "late arrival"),
        ],
    )

    resp = client.get(f"/api/plans/{pv}/explanation/students/S1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["student_id"] == "S1"
    assert body["root_node_id"] == "result:S1"
    assert body["graph_digest"]

    node_ids = {n["node_id"] for n in body["nodes"]}
    # 最小因果子图：覆盖合并、修正、汇总与源事件，但不包含 S2 的任何节点。
    assert "merge:S1:confirmed" in node_ids
    assert "adjustment:E-04" in node_ids
    assert "total:S1" in node_ids
    assert "event:E-01" in node_ids
    assert "event:E-02" in node_ids
    assert not any("S2" in nid for nid in node_ids)
    assert "event:E-03" not in node_ids

    merge = next(n for n in body["nodes"] if n["node_id"] == "merge:S1:confirmed")
    # 重叠签到合并为一个区间，学时按并集计算。
    assert merge["attributes"]["seconds"] == 10800
    assert merge["attributes"]["contributor_count"] == 2
    assert merge["inputs"] == ["checkin:E-01", "checkin:E-02"]

    total = next(n for n in body["nodes"] if n["node_id"] == "total:S1")
    assert total["attributes"]["confirmed_seconds"] == 10800
    assert total["attributes"]["adjustment_seconds"] == -900
    assert total["attributes"]["total_seconds"] == 9900

    # 节点带来源版本与输入指纹。
    for node in body["nodes"]:
        assert node["source_version"]
        assert len(node["input_fingerprint"]) == 64

    # 相同输入多次计算结果一致。
    again = client.get(f"/api/plans/{pv}/explanation/students/S1").json()
    assert again["graph_digest"] == body["graph_digest"]
    assert again["nodes"] == body["nodes"]


def test_explanation_query_unknown_student_or_plan(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    resp = client.get(f"/api/plans/{pv}/explanation/students/NOBODY")
    assert resp.status_code == 404
    resp = client.get("/api/plans/NOPE/explanation/students/S1")
    assert resp.status_code == 404


def test_explanation_query_uses_frozen_graph(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
        ],
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    # 冻结后到达的修正不影响冻结图。
    _seed_events(client, [_correction("E-09", "S1", 3600, "make-up")])

    frozen = client.get(
        f"/api/plans/{pv}/explanation/students/S1", params={"freeze_id": "F-01"}
    ).json()
    frozen_ids = {n["node_id"] for n in frozen["nodes"]}
    assert "adjustment:E-09" not in frozen_ids
    total = next(n for n in frozen["nodes"] if n["node_id"] == "total:S1")
    assert total["attributes"]["total_seconds"] == 7200

    live = client.get(f"/api/plans/{pv}/explanation/students/S1").json()
    live_ids = {n["node_id"] for n in live["nodes"]}
    assert "adjustment:E-09" in live_ids
    assert live["graph_digest"] != frozen["graph_digest"]

    resp = client.get(
        f"/api/plans/{pv}/explanation/students/S1", params={"freeze_id": "NOPE"}
    )
    assert resp.status_code == 404


def test_node_trace_returns_causal_ancestors(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T12:00:00+08:00",
                activity_type="internship",
            ),
            {
                "event_id": "E-02",
                "event_type": "mentor_confirm",
                "student_id": "S1",
                "payload": {"checkin_event_id": "E-01"},
            },
        ],
    )

    resp = client.get(f"/api/plans/{pv}/explanation/nodes/total:S1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node"]["node_id"] == "total:S1"
    ancestor_ids = {n["node_id"] for n in body["ancestors"]}
    # 追溯链覆盖确认、签到与源事件节点。
    assert "merge:S1:confirmed" in ancestor_ids
    assert "checkin:E-01" in ancestor_ids
    assert "confirm:E-02" in ancestor_ids
    assert "event:E-01" in ancestor_ids
    assert "event:E-02" in ancestor_ids
    assert "total:S1" not in ancestor_ids

    resp = client.get(f"/api/plans/{pv}/explanation/nodes/nope:S1")
    assert resp.status_code == 404


def test_verify_endpoint_confirms_frozen_graph(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
            _correction("E-02", "S1", -900, "late"),
        ],
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})

    resp = client.get(f"/api/plans/{pv}/freezes/F-01/explanation/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consistent"] is True
    assert body["problems"] == []
    assert body["stored_digest"] == body["rebuilt_digest"]
    assert body["event_cutoff_id"] == "E-02"

    # 冻结后追加事件不影响核验结果。
    _seed_events(client, [_correction("E-09", "S1", 60, "make-up")])
    again = client.get(f"/api/plans/{pv}/freezes/F-01/explanation/verify").json()
    assert again["consistent"] is True
    assert again["stored_digest"] == body["stored_digest"]

    resp = client.get(f"/api/plans/{pv}/freezes/NOPE/explanation/verify")
    assert resp.status_code == 404


def test_export_trims_fields_by_role_without_changing_identifiers(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
            _correction("E-02", "S1", -900, "late arrival"),
        ],
    )

    auditor = client.get(
        f"/api/plans/{pv}/explanation/students/S1/export",
        params={"viewer_role": "auditor"},
    )
    assert auditor.status_code == 200, auditor.text
    auditor_body = auditor.json()
    assert auditor_body["manifest"]["viewer_role"] == "auditor"
    assert auditor_body["manifest"]["restricted_fields_included"] is False
    assert auditor_body["manifest"]["manifest_fingerprint"]

    staff = client.get(
        f"/api/plans/{pv}/explanation/students/S1/export",
        params={"viewer_role": "staff"},
    ).json()
    assert staff["manifest"]["restricted_fields_included"] is True

    auditor_nodes = {n["node_id"]: n for n in auditor_body["graph"]["nodes"]}
    staff_nodes = {n["node_id"]: n for n in staff["graph"]["nodes"]}
    # 字段裁剪不改变节点标识、指纹与子图摘要。
    assert set(auditor_nodes) == set(staff_nodes)
    for node_id in auditor_nodes:
        assert (
            auditor_nodes[node_id]["input_fingerprint"]
            == staff_nodes[node_id]["input_fingerprint"]
        )
    assert auditor_body["graph"]["graph_digest"] == staff["graph"]["graph_digest"]

    # 审计角色看不到修正理由与事件负载，工作人员可以看到。
    auditor_adjustment = auditor_nodes["adjustment:E-02"]["attributes"]
    staff_adjustment = staff_nodes["adjustment:E-02"]["attributes"]
    assert "reason" not in auditor_adjustment
    assert staff_adjustment["reason"] == "late arrival"
    assert "payload" not in auditor_nodes["event:E-01"]["attributes"]
    assert staff_nodes["event:E-01"]["attributes"]["payload"]["activity_id"] == "A1"

    resp = client.get(
        f"/api/plans/{pv}/explanation/students/S1/export",
        params={"viewer_role": "root"},
    )
    assert resp.status_code == 422


def test_explanation_reflects_negative_correction_and_clamping(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"
            ),
            _correction("E-02", "S1", -7200, "unapproved absence"),
        ],
    )

    body = client.get(f"/api/plans/{pv}/explanation/students/S1").json()
    nodes = {n["node_id"]: n for n in body["nodes"]}
    total = nodes["total:S1"]
    assert total["attributes"]["raw_total_seconds"] == -3600
    assert total["attributes"]["clamped"] is True
    assert total["attributes"]["total_seconds"] == 0
    adjustment = nodes["adjustment:E-02"]
    assert adjustment["attributes"]["seconds"] == -7200


def test_verify_detects_tampered_frozen_graph(db, client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
        ],
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})

    # 直接篡改持久化冻结中的节点内容，不更新指纹。
    row = db.get(Freeze, (pv, "F-01"))
    snapshot = dict(row.snapshot)
    graph = dict(snapshot["explanation_graph"])
    graph["nodes"] = [
        (
            {**raw, "attributes": {**raw["attributes"], "total_seconds": 999999}}
            if raw["node_id"] == "total:S1"
            else raw
        )
        for raw in graph["nodes"]
    ]
    snapshot["explanation_graph"] = graph
    row.snapshot = snapshot
    db.commit()

    body = client.get(f"/api/plans/{pv}/freezes/F-01/explanation/verify").json()
    assert body["consistent"] is False
    assert any("指纹" in p for p in body["problems"])

    # 篡改后的图仍可用于追溯，但核验能暴露问题。
    resp = client.get(
        f"/api/plans/{pv}/explanation/students/S1", params={"freeze_id": "F-01"}
    )
    assert resp.status_code == 200


def test_frozen_graph_rebuild_with_recorded_rule_version(db, client):
    """冻结图按记录的规则版本核验：规则升级不改变既有冻结图的核验结论。"""
    from app.core.explain import RULE_VERSION

    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _seed_events(
        client,
        [
            _checkin(
                "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
            ),
        ],
    )
    client.post(f"/api/plans/{pv}/freezes/F-01", json={})

    verify = services.verify_explanation_graph(db, pv, "F-01")
    assert verify["consistent"] is True
    assert verify["stored_digest"] == verify["rebuilt_digest"]

    # 即使当前内核规则版本变化，按冻结图记录的版本重放仍一致。
    stored = db.get(Freeze, (pv, "F-01")).snapshot
    assert stored["explanation_graph"]["rule_version"] == RULE_VERSION
    events = load_events(db, pv)
    plan = services.get_plan(db, pv)
    assert plan is not None
    _, rebuilt_old = replay_explained(
        events,
        plan_version=pv,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id="E-01",
        rule_version="hours-rules/v1",
    )
    _, rebuilt_new = replay_explained(
        events,
        plan_version=pv,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id="E-01",
        rule_version="hours-rules/v2",
    )
    assert rebuilt_old.digest == verify["stored_digest"]
    assert rebuilt_new.digest != verify["stored_digest"]
