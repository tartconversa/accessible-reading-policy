"""授权减少与设备故障后的可兑现性重估。

供给变化（supply_changes）是供给侧事实，不是学生选择。本模块：
1. 把变化应用为容量快照的 override；
2. 重算受影响服务点方案的可行性，明确哪些持续服务受影响、还能观察什么；
3. 标注被故障切换的请求来源，禁止把它们当作替代格式更受欢迎的证据。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import FORMAT_LABELS, DataBundle
from .planning import (
    OBSERVE_CAP_PER_FORMAT, capacity_snapshot, check_feasibility,
)


@dataclass
class ImpactedCommitment:
    commitment_id: str
    person_id: str
    format: str
    kind: str
    impact: str
    detail: str


def apply_supply_change(change):
    return {
        "audio_limit": change.audio_limit,
        "devices_available": change.devices_available,
        "effective_on": change.effective_on.isoformat(),
        "reason": change.reason,
    }


def reassess_service(bundle: DataBundle, plan, service_id: str, overrides: dict,
                     effective_on=None) -> dict:
    """用新容量重算既有方案，返回受影响承诺与仍可保留的安排。

    effective_on 给定时，容量按生效日之后的剩余周数折算，而不是假定冲击贯穿整个周期。
    """
    from datetime import timedelta
    from .models import PLANNING_WEEK_START
    service = bundle.service_by_id[service_id]
    if effective_on is not None:
        cycle_end = PLANNING_WEEK_START + timedelta(weeks=plan.capacity.weeks) - timedelta(days=1)
        remain_days = max(0, (cycle_end - effective_on).days + 1)
        remain_weeks = max(1, -(-remain_days // 7))  # 向上取整，至少保留一周用于重排
    else:
        remain_weeks = plan.capacity.weeks
    before = capacity_snapshot(service)
    after = capacity_snapshot(service, overrides=overrides, weeks=remain_weeks)
    weekly = {f: 0 for f in ("paper", "large_text", "audio")}
    for s in plan.services:
        for f in s.formats:
            weekly[f] += 1
    feasibility_before = check_feasibility(weekly, before, include_audio="audio" in plan.bundle)
    feasibility_after = check_feasibility(weekly, after, include_audio="audio" in plan.bundle)

    impacted, preserved = [], []
    binding = [name for name, c in feasibility_after.items()
               if name != "all_ok" and isinstance(c, dict) and not c["ok"]]
    device_bound = "devices" in binding
    for c in bundle.commitments_by_service.get(service_id, []):
        at_risk = False
        detail = ""
        if c.format == "audio" and ("audio_license" in binding or device_bound
                                    or "network_for_audio" in binding):
            at_risk = True
            detail = "有声授权/设备容量下降，必要保障承诺需要补位或等待恢复，不能转为观察或取消"
        elif c.format == "large_text" and ("text_license" in binding or device_bound):
            at_risk = True
            if device_bound:
                detail = "可用终端下降，可调字号服务缺少设备，观察安排暂停扩容并登记设备缺口"
            else:
                detail = "可调字号授权受限，观察安排暂停扩容"
        if at_risk:
            impacted.append(ImpactedCommitment(
                c.id, c.person_id, c.format, c.kind, "at_risk", detail).__dict__)
        else:
            preserved.append({"commitment_id": c.id, "person_id": c.person_id,
                              "format": c.format, "kind": c.kind})
    # 仍可观察的内容：不受绑定约束影响的格式，且观察名额不超上限
    observe_options = []
    for f in plan.bundle:
        constraint = {"audio": "audio_license", "large_text": "text_license"}.get(f)
        needs_device = f in ("audio", "large_text")
        blocked = (constraint is not None and constraint in binding) or (needs_device and device_bound)
        if not blocked:
            observe_options.append({
                "format": f, "label": FORMAT_LABELS[f],
                "observe_cap": OBSERVE_CAP_PER_FORMAT,
                "note": "可在剩余内容量内维持小批量继续观察",
            })
    return {
        "service_id": service_id,
        "effective_on": effective_on.isoformat() if effective_on else None,
        "remaining_weeks": remain_weeks,
        "capacity_before": before.to_dict(),
        "capacity_after": after.to_dict(),
        "feasibility_before": feasibility_before,
        "feasibility_after": feasibility_after,
        "binding_constraints": binding,
        "impacted_commitments": impacted,
        "preserved_commitments": preserved,
        "remaining_observe_options": observe_options,
        "provenance_rule": (
            "本服务点因故障发生的格式切换保留原始请求格式，标记为 supply_switch；"
            "后续分析禁止把切换后的格式计入学生偏好或替代格式受欢迎程度。"),
    }


def run_contingency(bundle: DataBundle, plans: dict) -> list:
    """对资料中全部供给变化逐一重估。"""
    out = []
    for change in bundle.supply_changes:
        plan = plans.get(change.service_id)
        if plan is None:
            continue
        overrides = apply_supply_change(change)
        assessment = reassess_service(bundle, plan, change.service_id, overrides,
                                      effective_on=change.effective_on)
        assessment["supply_change_id"] = change.id
        assessment["received_on"] = change.received_on.isoformat()
        out.append(assessment)
    return out


def provenance_guard(bundle: DataBundle) -> dict:
    """来源守卫：核对 requests 中所有非按请求提供的记录是否都有供给侧原因。"""
    violations = []
    guarded = 0
    for rq in bundle.requests:
        if rq.provided != rq.requested:
            if rq.reason in ("device_failure", "license_limit"):
                guarded += 1
            else:
                violations.append({"request_id": rq.id, "reason": rq.reason,
                                   "issue": "非按请求提供但缺少供给侧原因，存在被误记为偏好的风险"})
        elif rq.reason in ("device_failure", "license_limit"):
            violations.append({"request_id": rq.id, "reason": rq.reason,
                               "issue": "按请求提供却挂供给侧原因，记录自相矛盾"})
        if rq.reason == "license_limit" and rq.provided is not None:
            violations.append({"request_id": rq.id, "reason": rq.reason,
                               "issue": "授权上限拒绝却有实际提供格式"})
    return {
        "supply_driven_records_guarded": guarded,
        "violations": violations,
        "rule": "provided != requested 且 reason 为供给侧原因时，is_student_choice=False，"
                "不得进入偏好/受欢迎程度统计。",
    }
