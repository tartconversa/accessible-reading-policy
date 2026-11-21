"""服务点容量核算、格式组合比较与下一周期方案。

约束口径（随方法说明保留）：
- 纸本：不占终端、不占并发授权、不受网络时段限制，只占指导教师时间。
- 有声/可调字号：每名学生每周 1 次、每次占 1 台终端 1 小时，并占用对应格式的并发授权。
- 有声还要求落在网络可用时段内；授权只在 license_end（不含当天）之前有效。
- 指导教师总工时按周核算，纸本按 0.5 小时/次，数字格式按 1 小时/次。
- 必要保障优先于一切观察/试用资源；观察安排对每服务点的同一格式设小批量上限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import (
    COMMITMENT_LABELS, FORMAT_LABELS, FORMATS, PLANNING_WEEKS, DataBundle,
)

TEACHER_HOURS_DIGITAL = 1.0
TEACHER_HOURS_PAPER = 0.5
SESSIONS_PER_STUDENT_CYCLE = 7  # 双周一次，13 周周期内每名学生每格式 7 次
STUDENT_SLOTS_PER_DEVICE_WEEK = 15  # 每台终端每周 3 时段 × 5 天可服务 15 名学生
OBSERVE_CAP_PER_FORMAT = 6  # 每服务点、每格式继续观察的小批量上限（政策参数）


@dataclass(frozen=True)
class CapacitySnapshot:
    service_id: str
    weeks: int
    student_device_slots_week: int  # 终端每周可服务的数字格式学生数
    audio_slots_week: int           # 有声授权每周可服务学生数（=并发上限）
    text_slots_week: int            # 可调字号授权每周可服务学生数（=并发上限）
    teacher_hours_cycle: float      # 整个周期可用指导工时
    network_ok: bool
    license_active: bool
    license_end: str
    notes: tuple = ()

    def to_dict(self):
        return {
            "service_id": self.service_id, "weeks": self.weeks,
            "student_device_slots_week": self.student_device_slots_week,
            "audio_slots_week": self.audio_slots_week,
            "text_slots_week": self.text_slots_week,
            "teacher_hours_cycle": self.teacher_hours_cycle,
            "network_supports_audio": self.network_ok,
            "license_active_for_cycle": self.license_active,
            "license_end": self.license_end,
            "notes": list(self.notes),
        }


def capacity_snapshot(service, weeks=PLANNING_WEEKS, start=None, overrides=None):
    """核算单个服务点在一个周期内的容量；overrides 用于供给冲击后的重估。"""
    from .models import PLANNING_WEEK_START
    start = start or PLANNING_WEEK_START
    notes = []
    devices = service.devices
    audio_limit = service.concurrent_audio
    text_limit = service.concurrent_text
    license_end = service.license_end
    if overrides:
        if overrides.get("devices_available") is not None:
            devices = overrides["devices_available"]
            notes.append("设备数由 %d 调整为 %d" % (service.devices, devices))
        if overrides.get("audio_limit") is not None:
            audio_limit = overrides["audio_limit"]
            notes.append("有声并发由 %d 调整为 %d" % (service.concurrent_audio, audio_limit))
        if overrides.get("license_end") is not None:
            raw_end = overrides["license_end"]
            license_end = raw_end if hasattr(raw_end, "isoformat") else date.fromisoformat(raw_end)
    # 每周学生槽位：终端按每台 15 个槽位计；并发授权直接是每周可同时服务的学生上限
    student_device_slots = devices * STUDENT_SLOTS_PER_DEVICE_WEEK
    audio_slots = audio_limit
    text_slots = text_limit
    # 教师工时按整个周期核算（每周工时 × 周数）；会话需求按双周一次折算
    teacher_hours_cycle = service.teacher_hours * weeks
    window_hours = (service.network_end.hour - service.network_start.hour
                    + (service.network_end.minute - service.network_start.minute) / 60.0)
    network_ok = window_hours >= 3
    if not network_ok:
        notes.append("网络可用时段不足 3 小时/天，有声服务不可安排")
    cycle_end = start + timedelta(days=7 * weeks - 1)
    license_active = license_end > cycle_end
    if not license_active:
        notes.append("授权在周期结束前到期（%s），数字格式仅可安排到期前" % license_end.isoformat())
    return CapacitySnapshot(
        service.id, weeks, student_device_slots, audio_slots, text_slots,
        teacher_hours_cycle, network_ok, license_active, license_end.isoformat(), tuple(notes),
    )


@dataclass
class StudentService:
    person_id: str
    formats: list            # 拟提供格式
    kind: str                # necessary / observe / unsupported
    reasons: list = field(default_factory=list)


@dataclass
class ServicePlan:
    service_id: str
    capacity: CapacitySnapshot
    services: list           # list[StudentService]
    feasibility: dict
    uncovered: list          # 未覆盖/受限学生及原因
    commitment_gaps: list    # 应承诺而未确认的必要保障（由资料核对得出）
    bundle: list             # 实际纳入的格式组合
    score: dict
    warnings: list

    def to_dict(self):
        return {
            "service_id": self.service_id,
            "capacity": self.capacity.to_dict(),
            "bundle": [{"format": f, "label": FORMAT_LABELS[f]} for f in self.bundle],
            "feasibility": self.feasibility,
            "services": [
                {"person_id": s.person_id, "kind": COMMITMENT_LABELS.get(s.kind, s.kind),
                 "formats": s.formats, "reasons": s.reasons}
                for s in self.services
            ],
            "uncovered": self.uncovered,
            "commitment_gaps": self.commitment_gaps,
            "score": self.score,
            "warnings": self.warnings,
        }


def _format_grade_map(evidence):
    """格式 -> 总体最佳证据等级与配对明细。"""
    grades = {f: [] for f in FORMATS}
    for p in evidence.pairs:
        if p.subgroup_key == "all":
            grades[p.format_a].append((p.grade, p))
            grades[p.format_b].append((p.grade, p))
    rank = {"A": 4, "B": 3, "C": 2, "D": 1}
    best = {}
    for f, items in grades.items():
        best[f] = max((rank[g] for g, _ in items), default=1)
    return best, grades


GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}


def _student_evidence_grade(evidence, person_id, bundle, fmt):
    """取该学生所属各分层中与该格式有关的最差等级（保守口径）。"""
    person = bundle.person_by_id[person_id]
    area = bundle.school_by_id[person.school_id].area
    grade_band = "低年级(3-4年级)" if person.grade <= 4 else "高年级(5-6年级)"
    need = "听读必要" if person.access_need == "audio" else "普通文本必要"
    levels = []
    for p in evidence.pairs:
        if fmt not in (p.format_a, p.format_b):
            continue
        match = ((p.subgroup_key == "all")
                 or (p.subgroup_key == "grade_band" and p.subgroup_value == grade_band)
                 or (p.subgroup_key == "access_need" and p.subgroup_value == need)
                 or (p.subgroup_key == "area" and p.subgroup_value == area))
        if match:
            levels.append(GRADE_RANK[p.grade])
    return min(levels) if levels else 1  # 无证据按 D 处理


def check_feasibility(weekly_counts, capacity, include_audio=True, sessions_per_student=SESSIONS_PER_STUDENT_CYCLE):
    """weekly_counts: 每格式每周的学生数（即每周并发会话需求）。

    终端/授权按每周并发约束；教师工时按整个周期核算，
    每名学生在周期内参加 sessions_per_student 次（双周一次）。
    """
    digital_weekly = weekly_counts.get("audio", 0) + weekly_counts.get("large_text", 0)
    cycle_digital_sessions = digital_weekly * sessions_per_student
    cycle_paper_sessions = weekly_counts.get("paper", 0) * sessions_per_student
    teacher_demand = (cycle_paper_sessions * TEACHER_HOURS_PAPER
                      + cycle_digital_sessions * TEACHER_HOURS_DIGITAL)
    checks = {
        "devices": {"demand": digital_weekly, "limit": capacity.student_device_slots_week,
                    "ok": digital_weekly <= capacity.student_device_slots_week},
        "audio_license": {"demand": weekly_counts.get("audio", 0), "limit": capacity.audio_slots_week,
                          "ok": (weekly_counts.get("audio", 0) <= capacity.audio_slots_week)
                          if include_audio else True},
        "text_license": {"demand": weekly_counts.get("large_text", 0), "limit": capacity.text_slots_week,
                         "ok": weekly_counts.get("large_text", 0) <= capacity.text_slots_week},
        "teacher_hours": {"demand": round(teacher_demand, 2), "limit": capacity.teacher_hours_cycle,
                          "ok": teacher_demand <= capacity.teacher_hours_cycle + 1e-9},
        "network_for_audio": {"demand": weekly_counts.get("audio", 0),
                              "ok": (capacity.network_ok or weekly_counts.get("audio", 0) == 0)
                              if include_audio else True},
        "license_window": {"ok": capacity.license_active if digital_weekly > 0 else True},
    }
    checks["all_ok"] = all(v["ok"] for v in checks.values())
    return checks


def evaluate_bundles(bundle: DataBundle, evidence, capacity_overrides=None):
    """枚举 7 种非空格式组合，比较预期受益与未覆盖风险，返回可行组合排序。"""
    best_grade, _ = _format_grade_map(evidence)
    results = []
    for mask in range(1, 1 << len(FORMATS)):
        chosen = [FORMATS[i] for i in range(len(FORMATS)) if mask & (1 << i)]
        per_service = {}
        for service in bundle.services:
            cap = capacity_snapshot(service, overrides=capacity_overrides and capacity_overrides.get(service.id))
            people = bundle.people_of_service(service.id)
            counts = {f: 0 for f in FORMATS}
            covered, unsupported = 0, []
            for p in people:
                need_formats = ["audio"] if p.access_need == "audio" else ["paper", "large_text"]
                viable = [f for f in need_formats if f in chosen]
                if not viable:
                    unsupported.append({"person_id": p.id, "reason": "必要可访问格式不在组合内"})
                    continue
                # 与 build_service_plan 同口径：每名学生只占一个主必要格式
                # 听读必要->audio；文本必要->组合内证据等级最高的可得格式
                if p.access_need == "audio":
                    primary = viable[0]
                else:
                    primary = max(viable,
                                  key=lambda f: _student_evidence_grade(evidence, p.id, bundle, f))
                counts[primary] += 1
                covered += 1
            weekly = counts  # 每人每周一次主格式
            feasibility = check_feasibility(weekly, cap, include_audio="audio" in chosen)
            # 受益：组合内各格式总体证据等级之和（按实际承接人数加权）
            benefit = sum(best_grade[f] * counts[f] for f in chosen)
            # 容量违约风险：不可行组合按各约束超出比例计罚
            overload = 0.0
            for name, c in feasibility.items():
                if name in ("devices", "audio_license", "text_license", "teacher_hours") and not c["ok"]:
                    overload += c["demand"] / max(c["limit"], 1e-9) - 1.0
            per_service[service.id] = {
                "feasible": feasibility["all_ok"], "covered": covered,
                "unsupported": unsupported, "overload": overload,
                "weekly_counts": counts, "feasibility": feasibility,
            }
        agg_benefit = 0.0
        agg_risk = 0
        feasible_all = True
        for sid, info in per_service.items():
            people_n = len(bundle.people_of_service(sid))
            if not info["feasible"]:
                feasible_all = False
            for f in chosen:
                agg_benefit += best_grade[f] * info["weekly_counts"][f]
            agg_risk += len(info["unsupported"]) + info["overload"] * 2
        results.append({
            "bundle": chosen,
            "feasible_everywhere": feasible_all,
            "expected_benefit": round(agg_benefit, 2),
            "uncovered_risk": agg_risk,
            "per_service": per_service,
        })
    results.sort(key=lambda r: (not r["feasible_everywhere"], r["uncovered_risk"], -r["expected_benefit"]))
    return results


def build_service_plan(bundle: DataBundle, evidence, service_id: str,
                       chosen_bundle, capacity_overrides=None) -> ServicePlan:
    """对单个服务点形成下一周期方案：必要保障 + 小批量观察 + 未覆盖清单。"""
    service = bundle.service_by_id[service_id]
    cap = capacity_snapshot(service, overrides=capacity_overrides)
    people = bundle.people_of_service(service_id)
    services, uncovered, warnings = [], [], []

    # 必要格式：由 access_need 直接决定，不允许因平均效果不占优而取消
    necessary_need = {"audio": ["audio"], "text": ["paper", "large_text"]}
    observe_counts = {f: 0 for f in FORMATS}

    # 第一遍：必要保障
    provisional = []
    for p in people:
        need = necessary_need[p.access_need]
        viable = [f for f in need if f in chosen_bundle]
        if not viable:
            uncovered.append({"person_id": p.id, "kind": "necessary",
                              "reason": "必要可访问格式 %s 不在所选组合内" % "/".join(
                                  FORMAT_LABELS[f] for f in need)})
            services.append(StudentService(p.id, [], "unsupported",
                                           ["必要格式无法兑现"]))
            continue
        # 听读必要学生只接受 audio；普通文本必要在 paper/large_text 中按可得性保留
        provisional.append((p, viable))

    weekly = {f: 0 for f in FORMATS}
    accepted_necessary = []
    for p, viable in provisional:
        # 选一个主必要格式：听读->audio；文本->按该生证据等级较高者
        if p.access_need == "audio":
            pick = ["audio"]
        else:
            pick = sorted(viable, key=lambda f: -_student_evidence_grade(evidence, p.id, bundle, f))[:1]
        for f in pick:
            weekly[f] += 1
        accepted_necessary.append((p, pick))

    # 第二遍：观察名额（仅对 C/D 证据群体保留小批量，且不能挤占必要容量）
    for p, necessary_fmts in accepted_necessary:
        extra = []
        for f in chosen_bundle:
            if f in necessary_fmts:
                continue
            grade = _student_evidence_grade(evidence, p.id, bundle, f)
            if grade <= GRADE_RANK["B"] and observe_counts[f] < OBSERVE_CAP_PER_FORMAT:
                extra.append(f)
                observe_counts[f] += 1
        all_formats = necessary_fmts + extra
        for f in extra:
            weekly[f] += 1
        kind = "necessary"
        services.append(StudentService(p.id, all_formats, kind,
                                       ["必要保障：%s" % "/".join(FORMAT_LABELS[f] for f in necessary_fmts)]
                                       + (["继续观察：%s" % FORMAT_LABELS[f] for f in extra] if extra else [])))

    feasibility = check_feasibility(weekly, cap, include_audio="audio" in chosen_bundle)
    # 必要容量不足时，逐轮压缩观察名额而不是取消必要服务
    compressed = False
    while not feasibility["all_ok"]:
        removed_any = False
        for s in services:
            observe_part = [f for f in s.formats
                            if any("继续观察：%s" % FORMAT_LABELS[f] in r for r in s.reasons)]
            for f in observe_part:
                weekly[f] -= 1
                s.formats.remove(f)
                s.reasons = [r for r in s.reasons if r != "继续观察：%s" % FORMAT_LABELS[f]]
                observe_counts[f] = max(0, observe_counts[f] - 1)
                removed_any = compressed = True
        feasibility = check_feasibility(weekly, cap, include_audio="audio" in chosen_bundle)
        if feasibility["all_ok"] or not removed_any:
            break
    if compressed:
        warnings.append("观察名额已压缩以保障必要服务容量")
    if not feasibility["all_ok"]:
        for name, c in feasibility.items():
            if name in ("devices", "audio_license", "text_license", "teacher_hours") and not c["ok"]:
                warnings.append("压缩观察后约束 %s 仍超载（需求 %s > 容量 %s），必要服务存在缺口"
                                % (name, c["demand"], c["limit"]))

    best_grade, _ = _format_grade_map(evidence)
    benefit = sum(best_grade[f] * weekly[f] for f in chosen_bundle)
    # 承诺缺口：本服务点名册中听读必要、但资料里没有必要保障承诺的学生
    existing_necessary = {c.person_id for c in bundle.commitments_by_service.get(service_id, [])
                          if c.kind == "necessary"}
    commitment_gaps = []
    for p in people:
        if p.access_need == "audio" and p.id not in existing_necessary:
            commitment_gaps.append({
                "person_id": p.id, "format": "audio",
                "reason": "听读必要学生在资料中没有必要保障承诺，下一周期须先确认，不得列入观察或试用",
            })
    return ServicePlan(
        service_id, cap, services, feasibility, uncovered, commitment_gaps, chosen_bundle,
        {"expected_benefit": round(benefit, 2),
         "uncovered_count": len(uncovered),
         "observe_slots_used": observe_counts,
         "observe_cap_per_format": OBSERVE_CAP_PER_FORMAT},
        warnings,
    )


def audit_commitments(bundle: DataBundle, plans: dict):
    """核对既有承诺：必要承诺是否被方案兑现；观察承诺是否超出小批量上限。"""
    findings = []
    for c in bundle.commitments:
        plan = plans.get(c.service_id)
        if plan is None:
            findings.append({"commitment_id": c.id, "issue": "对应服务点无方案", "severity": "error"})
            continue
        match = next((s for s in plan.services if s.person_id == c.person_id), None)
        if c.kind == "necessary":
            if match is None or c.format not in match.formats:
                findings.append({"commitment_id": c.id, "person_id": c.person_id,
                                 "service_id": c.service_id, "format": c.format,
                                 "issue": "必要保障承诺未被方案兑现", "severity": "error"})
        else:
            observe_n = sum(1 for s in plan.services
                            if any(r == "继续观察：%s" % FORMAT_LABELS[c.format] for r in s.reasons))
            if observe_n > OBSERVE_CAP_PER_FORMAT:
                findings.append({"commitment_id": c.id, "service_id": c.service_id,
                                 "format": c.format, "issue": "观察安排超出小批量上限 %d" % OBSERVE_CAP_PER_FORMAT,
                                 "severity": "warning"})
    # 听读必要但没有必要承诺的学生（承诺缺口）
    committed_necessary = {(c.person_id, c.service_id) for c in bundle.commitments if c.kind == "necessary"}
    for p in bundle.people:
        if p.access_need != "audio":
            continue
        service_id = next((s.id for s in bundle.services if s.school_id == p.school_id), None)
        if service_id and (p.id, service_id) not in committed_necessary:
            findings.append({"person_id": p.id, "service_id": service_id, "format": "audio",
                             "issue": "听读必要学生缺少必要保障承诺", "severity": "error"})
    return findings
