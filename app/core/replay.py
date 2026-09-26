"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .explain import RULE_VERSION, ExplanationBuilder, ExplanationGraph


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
    )


def _utc_stamp(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _replay_events(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    rule_version: str = RULE_VERSION,
    build_graph: bool = True,
) -> tuple[ReplayState, ExplanationGraph | None]:
    """执行确定性的业务处理，解释图与最终状态由同一遍重放产出。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}
    builder = (
        ExplanationBuilder(
            plan_version=plan_version,
            timezone_name=timezone_name,
            required_seconds=required_seconds,
            rule_version=rule_version,
            event_cutoff_id=up_to_event_id,
        )
        if build_graph
        else None
    )

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
            if builder is not None:
                builder.add_checkin(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    event_type=event.event_type.value,
                    payload=event.payload,
                    activity_id=record.activity_id,
                    activity_type=record.activity_type,
                    start_utc=_utc_stamp(record.start_utc),
                    end_utc=_utc_stamp(record.end_utc),
                    raw_seconds=record.seconds,
                    initial_status=record.status.value,
                )
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
                if builder is not None:
                    builder.add_confirmation(
                        event_id=event.event_id,
                        student_id=event.student_id,
                        event_type=event.event_type.value,
                        payload=event.payload,
                        checkin_event_id=target_id,
                    )
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )
            )
            if builder is not None:
                builder.add_adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    event_type=event.event_type.value,
                    payload=event.payload,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_records = [r for r in records if r.counts]
        pending_records = [r for r in records if r.status == CheckinStatus.PENDING]
        confirmed_intervals = [(r.start_utc, r.end_utc) for r in confirmed_records]
        pending_intervals = [(r.start_utc, r.end_utc) for r in pending_records]

        merged_confirmed = merge_intervals(confirmed_intervals)
        confirmed_seconds = sum(
            elapsed_seconds(start, end) for start, end in merged_confirmed
        )
        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        raw_total_seconds = confirmed_seconds + adjustment_seconds
        clamped = raw_total_seconds < 0
        total_seconds = max(raw_total_seconds, 0)

        day_totals: dict[str, int] = {}
        for start, end in merged_confirmed:
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

        if builder is not None:
            records_by_id = {r.event_id: r for r in records}
            effective_statuses = {
                eid: record.status.value for eid, record in records_by_id.items()
            }
            builder.close_student(
                student_id=student_id,
                effective_statuses=effective_statuses,
                confirmed_checkin_ids=sorted(r.event_id for r in confirmed_records),
                pending_checkin_ids=sorted(r.event_id for r in pending_records),
                confirmed_intervals=[
                    (_utc_stamp(start), _utc_stamp(end))
                    for start, end in merged_confirmed
                ],
                confirmed_seconds=confirmed_seconds,
                pending_intervals=[
                    (_utc_stamp(start), _utc_stamp(end))
                    for start, end in merge_intervals(pending_intervals)
                ],
                pending_seconds=pending_seconds,
                adjustment_seconds=adjustment_seconds,
                raw_total_seconds=raw_total_seconds,
                clamped=clamped,
                total_seconds=total_seconds,
                lesson_units=total_seconds // (45 * 60),
                pending_lesson_units=pending_seconds // (45 * 60),
                meets_requirement=total_seconds >= required_seconds,
                daily=[(d.academic_day, d.seconds) for d in daily],
            )

    state = ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
    )
    return state, builder.build() if builder is not None else None


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    state, _ = _replay_events(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        up_to_event_id=up_to_event_id,
        build_graph=False,
    )
    return state


def replay_explained(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    rule_version: str = RULE_VERSION,
) -> tuple[ReplayState, ExplanationGraph]:
    """执行确定性的业务处理，返回状态与因果解释图。"""
    state, graph = _replay_events(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        up_to_event_id=up_to_event_id,
        rule_version=rule_version,
        build_graph=True,
    )
    assert graph is not None
    return state, graph


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
