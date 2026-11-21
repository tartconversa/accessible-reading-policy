"""实际服务交付记录与成功者偏差诊断。

requests 表同时包含成功提供、被动切换和被拒绝请求。后续估计如果只分析
provided 非空的记录，就会把授权/设备造成的缺口错误归因于学生或格式。
本模块把三类记录分开，并让报告显式携带缺口。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import FORMAT_LABELS, DataBundle

DELIVERY_RECORD_VERSION = 1


@dataclass(frozen=True)
class DeliveryRecord:
    id: str
    person_id: str
    service_id: str
    on: str
    requested: str
    provided: str | None
    reason: str | None
    understanding: float | None
    source: str          # accepted / supply_switch / rejected
    is_student_choice: bool

    def to_dict(self):
        return {
            "id": self.id, "person_id": self.person_id, "service_id": self.service_id,
            "on": self.on, "requested": self.requested, "provided": self.provided,
            "reason": self.reason, "understanding": self.understanding,
            "source": self.source, "is_student_choice": self.is_student_choice,
        }


def classify_requests(bundle: DataBundle) -> list[DeliveryRecord]:
    records = []
    for rq in bundle.requests:
        if rq.reason is None and rq.provided == rq.requested:
            source, choice = "accepted", True
        elif rq.reason == "device_failure":
            # 供给侧故障后的被动切换：保留原请求，不解释为偏好
            source, choice = "supply_switch", False
        elif rq.reason == "license_limit":
            source, choice = "rejected", False
        else:
            source, choice = "other", False
        records.append(DeliveryRecord(
            rq.id, rq.person_id, rq.service_id, rq.on.isoformat(), rq.requested,
            rq.provided, rq.reason, rq.understanding, source, choice,
        ))
    return records


def delivery_gap_report(bundle: DataBundle) -> dict:
    records = classify_requests(bundle)
    by_service = {}
    for service in bundle.services:
        rows = [r for r in records if r.service_id == service.id]
        accepted = [r for r in rows if r.source == "accepted"]
        switched = [r for r in rows if r.source == "supply_switch"]
        rejected = [r for r in rows if r.source == "rejected"]
        # 成功者偏差诊断：只看 provided 非空会得到怎样的“理解均值”
        provided_scores = [r.understanding for r in rows if r.understanding is not None]
        accepted_scores = [r.understanding for r in accepted if r.understanding is not None]
        by_service[service.id] = {
            "total_requests": len(rows),
            "accepted": len(accepted),
            "supply_driven_switch": len(switched),
            "license_rejected": len(rejected),
            "fill_rate": len(accepted) / len(rows) if rows else None,
            "mean_among_provided_only": (sum(provided_scores) / len(provided_scores)) if provided_scores else None,
            "mean_among_accepted_only": (sum(accepted_scores) / len(accepted_scores)) if accepted_scores else None,
            "excluded_if_success_only": len(switched) + len(rejected),
        }
    exclusions = sorted({v["excluded_if_success_only"] for v in by_service.values()})
    exclusion_text = ("、".join(str(n) for n in exclusions) + " 条") if exclusions else "0 条"
    flagged = {
        "diagnosis": (
            f"若仅分析 provided 非空的请求，每个服务点将排除 {exclusion_text}设备/授权失败记录，"
            "理解均值会系统性偏高，且故障导致的格式切换会被误读为格式偏好。"),
        "rule": "后续估计必须同时纳入三类记录并报告 fill_rate 与缺口原因；"
                "被动切换与拒绝不进入偏好统计。",
        "by_service": by_service,
    }
    return flagged


def post_delivery_estimation(bundle: DataBundle) -> dict:
    """服务后理解观测的估计：按请求格式与实际提供分别呈现，并带缺口警示。"""
    records = classify_requests(bundle)
    out = {}
    for requested in sorted({r.requested for r in records}):
        rows = [r for r in records if r.requested == requested]
        # 有实际提供即产生服务后观测（个别记录理解分暂缺，仍是已提供事件，不计入缺失）
        observed_rows = [r for r in rows if r.provided is not None]
        rejected = [r for r in rows if r.source == "rejected"]
        switched = [r for r in rows if r.source == "supply_switch"]
        scored = [r for r in observed_rows if r.understanding is not None]
        out[requested] = {
            "label": FORMAT_LABELS[requested],
            "requests": len(rows),
            "observed": len(observed_rows),
            "observed_with_score": len(scored),
            "missing_due_to_rejection": len(rejected),
            "missing_due_to_switch": len(switched),
            "observed_mean": (sum(r.understanding for r in scored) / len(scored)) if scored else None,
            "estimand_warning": (
                "该均值只在实际获得服务且有分数的请求上定义；缺失不是零分，也不能假定与得分无关，"
                "应与 fill_rate 一起报告，不能作为格式效果的无偏估计。"),
        }
    return out


def record_delivery(output_dir, record: dict, bundle_digest: str, bundle=None) -> Path:
    """把负责人确认后的实际交付/拒绝追加写入 outputs（原始资料保持不变）。

    传入 bundle 时额外校验学生与服务点外键；bundle_digest 固定派生记录的资料版本。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "delivery_records.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {"version": DELIVERY_RECORD_VERSION, "raw_digest": bundle_digest, "records": []}
    required = {"id", "person_id", "service_id", "on", "requested", "provided", "reason"}
    missing = required - set(record)
    if missing:
        raise ValueError("交付记录缺少字段：" + ",".join(sorted(missing)))
    if record["id"] in {r["id"] for r in payload["records"]}:
        raise ValueError("交付记录编号重复：" + record["id"])
    # 枚举与取值校验：派生记录必须遵守与原始资料相同的口径
    from .models import FORMATS, REQUEST_REASONS
    if record["requested"] not in FORMATS:
        raise ValueError("请求格式越界：%r" % record["requested"])
    if record["provided"] is not None and record["provided"] not in FORMATS:
        raise ValueError("实际提供格式越界：%r" % record["provided"])
    if record["reason"] not in (*REQUEST_REASONS, None):
        raise ValueError("原因字段越界：%r" % record["reason"])
    try:
        date.fromisoformat(record["on"])
    except (TypeError, ValueError):
        raise ValueError("日期字段不是 ISO 日期：%r" % record["on"])
    score = record.get("understanding")
    if score is not None and not (0 <= float(score) <= 100):
        raise ValueError("理解分必须在 0-100 或为 null")
    if record["provided"] is not None and record["reason"] is None and record["provided"] != record["requested"]:
        raise ValueError("实际提供与请求不一致时必须登记原因")
    # 来源一致性：供给侧原因必须伴随与请求不同的结果；按请求提供不能挂供给原因
    if record["reason"] in REQUEST_REASONS and record["provided"] == record["requested"]:
        raise ValueError("供给侧原因（%s）不能与按请求提供并存" % record["reason"])
    if record["reason"] == "license_limit" and record["provided"] is not None:
        raise ValueError("授权上限拒绝必须 provided=null")
    if record["reason"] == "device_failure" and record["provided"] == record["requested"]:
        raise ValueError("设备故障切换的实际提供必须与请求不同")
    if bundle is not None:
        if record["person_id"] not in bundle.person_by_id:
            raise ValueError("学生编号不存在：" + record["person_id"])
        if record["service_id"] not in bundle.service_by_id:
            raise ValueError("服务点编号不存在：" + record["service_id"])
        school_id = bundle.service_by_id[record["service_id"]].school_id
        if bundle.person_by_id[record["person_id"]].school_id != school_id:
            raise ValueError("学生与服务点不属于同一学校")
    return _persist_record(path, payload, record)


def _persist_record(path, payload, record):
    payload["records"].append({
        "id": record["id"], "person_id": record["person_id"], "service_id": record["service_id"],
        "on": record["on"], "requested": record["requested"], "provided": record["provided"],
        "reason": record["reason"],
        "understanding": record.get("understanding"),
        "recorded_on": date.today().isoformat(),
    })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_delivery_records(output_dir) -> dict | None:
    path = Path(output_dir) / "delivery_records.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
