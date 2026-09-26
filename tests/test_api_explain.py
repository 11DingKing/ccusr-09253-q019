"""解释图 API：解释查询、节点追溯、完整性核验与受控导出。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
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


def _confirm(eid, student, checkin_event_id):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm",
        "student_id": student,
        "payload": {"checkin_event_id": checkin_event_id},
    }


def _correction(eid, student, seconds, reason):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _seed_appeal_case(client):
    """S1：重叠签到 + 实习确认 + 负向修正；S2：无关学生；E-07 在冻结后到达。"""
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
                _checkin("E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00"),
                _checkin("E-03", "S1", "2024-03-15T13:00:00+08:00", "2024-03-15T15:00:00+08:00", activity_type="internship"),
                _confirm("E-04", "S1", "E-03"),
                _correction("E-05", "S1", -900, "迟到扣减"),
                _checkin("E-06", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/plans/{pv}/freezes/F-01", json={})
    assert resp.status_code == 201, resp.text
    # 冻结后到达的正向修正，不得影响 F-01 的解释图。
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={"events": [_correction("E-07", "S1", 3600, "补时")]},
    )
    assert resp.status_code == 201, resp.text
    return pv


def _explanation_url(pv, student, freeze="F-01"):
    return f"/api/plans/{pv}/freezes/{freeze}/explanation/{student}"


def _total_node(graph_body):
    return next(n for n in graph_body["nodes"] if n["kind"] == "total")


def test_frozen_explanation_is_stable_and_causal(client):
    pv = _seed_appeal_case(client)
    resp = client.get(_explanation_url(pv, "S1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["freeze_id"] == "F-01"
    assert body["event_cutoff_id"] == "E-06"
    assert body["student_id"] == "S1"
    assert body["rule_version"] == "hours-rules/v1"
    assert len(body["digest"]) == 64

    kinds = {n["kind"] for n in body["nodes"]}
    assert kinds == {"event", "merged_interval", "academic_day", "total", "progress"}

    # 只有对总学时有因果贡献的事件进入子图：E-06（他人）与 E-07（冻结后）排除。
    event_ids = sorted(
        n["attributes"]["event_id"]
        for n in body["nodes"]
        if n["kind"] == "event"
    )
    assert event_ids == ["E-01", "E-02", "E-03", "E-04", "E-05"]

    # 重叠签到 E-01/E-02 合并为一个区间。
    merges = [n for n in body["nodes"] if n["kind"] == "merged_interval"]
    assert len(merges) == 2  # 08:00-11:00 合并区间 + 13:00-15:00 实习区间
    merged_seconds = sorted(m["attributes"]["seconds"] for m in merges)
    assert merged_seconds == [7200, 10800]

    total = _total_node(body)
    assert total["attributes"]["confirmed_seconds"] == 18000
    assert total["attributes"]["adjustment_seconds"] == -900
    assert total["attributes"]["total_seconds"] == 17100
    assert total["attributes"]["clamped"] is False

    # 每个节点都带来源版本与输入指纹。
    for node in body["nodes"]:
        assert node["source_version"] == {
            "plan_version": pv,
            "rule_version": "hours-rules/v1",
        }
        assert len(node["input_fingerprint"]) == 64

    # 相同输入重复计算结果一致。
    again = client.get(_explanation_url(pv, "S1")).json()
    assert again == body


def test_live_explanation_reflects_post_freeze_events(client):
    pv = _seed_appeal_case(client)
    live = client.get(f"/api/plans/{pv}/students/S1/explanation").json()
    assert live["freeze_id"] is None
    assert live["event_cutoff_id"] is None
    total = _total_node(live)
    assert total["attributes"]["adjustment_seconds"] == -900 + 3600
    assert total["attributes"]["total_seconds"] == 20700

    # 冻结视图不受迟到事件影响。
    frozen = client.get(_explanation_url(pv, "S1")).json()
    assert _total_node(frozen)["attributes"]["total_seconds"] == 17100


def test_viewer_role_trims_fields_but_keeps_identity(client):
    pv = _seed_appeal_case(client)
    staff = client.get(_explanation_url(pv, "S1") + "?viewer_role=staff").json()
    mentor = client.get(_explanation_url(pv, "S1") + "?viewer_role=mentor").json()
    student = client.get(_explanation_url(pv, "S1") + "?viewer_role=student").json()

    for view in (mentor, student):
        assert [n["node_id"] for n in view["nodes"]] == [
            n["node_id"] for n in staff["nodes"]
        ]
        assert view["digest"] == staff["digest"]
        assert view["edges"] == staff["edges"]

    staff_reason = next(
        n for n in staff["nodes"] if n["node_id"] == "evt:E-05"
    )["attributes"]["reason"]
    mentor_reason = next(
        n for n in mentor["nodes"] if n["node_id"] == "evt:E-05"
    )["attributes"]["reason"]
    assert staff_reason == "迟到扣减"
    assert mentor_reason == "[redacted]"
    assert mentor["redacted_fields"] == ["reason"]

    bad = client.get(_explanation_url(pv, "S1") + "?viewer_role=ghost")
    assert bad.status_code == 400


def test_node_trace_walks_causal_chain(client):
    pv = _seed_appeal_case(client)
    trace = client.get(
        _explanation_url(pv, "S1") + "/nodes/sum:S1/trace"
    ).json()
    assert trace["node"]["node_id"] == "sum:S1"
    ancestor_ids = {n["node_id"] for n in trace["ancestors"]}
    assert {"evt:E-01", "evt:E-02", "evt:E-03", "evt:E-04", "evt:E-05"} <= ancestor_ids
    assert any(i.startswith("mrg:") for i in ancestor_ids)
    assert any(i.startswith("day:S1:") for i in ancestor_ids) is False
    assert {n["node_id"] for n in trace["descendants"]} == {"prog:S1"}

    # 从导师确认事件向下能到达被确认的实习签到。
    confirm_trace = client.get(
        _explanation_url(pv, "S1") + "/nodes/evt:E-04/trace"
    ).json()
    descendant_ids = {n["node_id"] for n in confirm_trace["descendants"]}
    assert "evt:E-03" in descendant_ids
    assert "sum:S1" in descendant_ids

    missing = client.get(_explanation_url(pv, "S1") + "/nodes/evt:NOPE/trace")
    assert missing.status_code == 404


def test_verify_reports_all_checks_passing(client):
    pv = _seed_appeal_case(client)
    resp = client.get(_explanation_url(pv, "S1") + "/verify")
    assert resp.status_code == 200, resp.text
    report = resp.json()

    assert report["ok"] is True
    assert report["freeze_id"] == "F-01"
    assert report["student_id"] == "S1"
    assert all(c["ok"] for c in report["checks"])
    names = {c["name"] for c in report["checks"]}
    assert {
        "unique_node_ids",
        "node_fingerprints",
        "edge_endpoints",
        "inputs_match_edges",
        "graph_digest",
        "cutoff_respected",
        "deterministic_replay",
        "matches_frozen_snapshot",
    } <= names

    graph = client.get(_explanation_url(pv, "S1")).json()
    assert report["graph_digest"] == graph["digest"]
    assert report["node_count"] == len(graph["nodes"])
    assert report["edge_count"] == len(graph["edges"])


def test_export_is_controlled_and_redacted(client):
    pv = _seed_appeal_case(client)
    url = _explanation_url(pv, "S1") + "/export"

    resp = client.post(
        url, json={"viewer_role": "mentor", "purpose": "申诉 A-123 复核"}
    )
    assert resp.status_code == 200, resp.text
    package = resp.json()
    manifest = package["manifest"]

    assert manifest["package_type"] == "explanation-export"
    assert manifest["viewer_role"] == "mentor"
    assert manifest["purpose"] == "申诉 A-123 复核"
    assert manifest["freeze_id"] == "F-01"
    assert manifest["redacted_fields"] == ["reason"]
    assert len(manifest["graph_digest"]) == 64
    assert len(manifest["content_digest"]) == 64

    # 导出内容按角色裁剪，但节点标识与图摘要与全量视图一致。
    staff = client.get(_explanation_url(pv, "S1") + "?viewer_role=staff").json()
    assert [n["node_id"] for n in package["graph"]["nodes"]] == [
        n["node_id"] for n in staff["nodes"]
    ]
    assert manifest["graph_digest"] == staff["digest"]
    reason = next(
        n for n in package["graph"]["nodes"] if n["node_id"] == "evt:E-05"
    )["attributes"]["reason"]
    assert reason == "[redacted]"

    # 相同请求重复导出内容一致。
    again = client.post(
        url, json={"viewer_role": "mentor", "purpose": "申诉 A-123 复核"}
    ).json()
    assert again["manifest"]["content_digest"] == manifest["content_digest"]
    assert again == package

    # 受控：必须给出用途，角色必须已知。
    assert client.post(url, json={"viewer_role": "mentor"}).status_code == 422
    assert (
        client.post(
            url, json={"viewer_role": "mentor", "purpose": "   "}
        ).status_code
        == 400
    )
    assert (
        client.post(
            url, json={"viewer_role": "ghost", "purpose": "x"}
        ).status_code
        == 400
    )


def test_explanation_not_found_cases(client):
    pv = _seed_appeal_case(client)
    assert (
        client.get(_explanation_url(pv, "S1", freeze="F-99")).status_code == 404
    )
    assert client.get(_explanation_url(pv, "S9")).status_code == 404
    assert (
        client.get(_explanation_url(pv, "S1") + "/verify").status_code == 200
    )
    assert (
        client.get("/api/plans/NOPE/students/S1/explanation").status_code == 404
    )
    assert (
        client.get(_explanation_url(pv, "S9") + "/verify").status_code == 404
    )
    assert (
        client.post(
            _explanation_url(pv, "S9") + "/export",
            json={"viewer_role": "staff", "purpose": "x"},
        ).status_code
        == 404
    )
