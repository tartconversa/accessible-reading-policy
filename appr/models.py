"""领域模型与资料读取。

资料契约（materials/contract.json）中的每张表映射为一个 dataclass。
原始资料在整个应用中只读：所有后续写入都进入 outputs/ 下的派生文件。
null 一律保留为 None，不会被解释为零分或零负担。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

FORMATS = ("paper", "large_text", "audio")
FORMAT_LABELS = {"paper": "纸本", "large_text": "可调字号文本", "audio": "有声听读"}
ASSIGNMENTS = ("random", "preference")
ASSIGNMENT_LABELS = {"random": "随机交叉", "preference": "偏好选择"}
EXIT_REASONS = ("device_failure", "discomfort")
EXIT_LABELS = {"device_failure": "设备故障", "discomfort": "不适退出", None: "完成"}
REQUEST_REASONS = ("device_failure", "license_limit")
REQUEST_REASON_LABELS = {"device_failure": "设备故障切换", "license_limit": "授权上限拒绝", None: "按请求提供"}
COMMITMENT_KINDS = ("necessary", "observe")
COMMITMENT_LABELS = {"necessary": "必要保障", "observe": "继续观察"}
PLANNING_WEEK_START = date(2026, 4, 1)  # 下一周期（承诺窗口）起日
PLANNING_WEEKS = 13  # 2026-04-01 至 2026-06-30 的周数


def _d(value):
    return None if value is None else date.fromisoformat(value)


def _t(value):
    return None if value is None else datetime.strptime(value, "%H:%M").time()


def _check_enum(name, value, allowed):
    if value not in allowed:
        raise ValueError(f"{name} 取值越界：{value!r}")


@dataclass(frozen=True)
class School:
    id: str
    area: str
    devices: int


@dataclass(frozen=True)
class Person:
    id: str
    school_id: str
    household: str
    grade: int
    access_need: str  # audio 听读 / text 普通文本
    sample_weight: float


@dataclass(frozen=True)
class Material:
    id: str
    family: str
    format: str
    grade_low: int
    grade_high: int


@dataclass(frozen=True)
class Experience:
    id: str
    person_id: str
    material_id: str
    order: int
    planned_on: date
    washout_days: int
    assignment: str
    actual_format: str | None
    understanding: float | None
    burden: int | None
    exit_reason: str | None

    @property
    def completed(self) -> bool:
        """完成 = 有实际形式、无退出原因，并且理解分存在。"""
        return self.exit_reason is None and self.actual_format is not None and self.understanding is not None


@dataclass(frozen=True)
class Service:
    id: str
    school_id: str
    devices: int
    concurrent_audio: int
    concurrent_text: int
    license_end: date  # 不含当天
    network_start: datetime.time
    network_end: datetime.time
    teacher_hours: float


@dataclass(frozen=True)
class Commitment:
    id: str
    person_id: str
    service_id: str
    format: str
    kind: str
    from_on: date
    to_on: date
    confirmed_on: date


@dataclass(frozen=True)
class ServiceRequest:
    id: str
    person_id: str
    service_id: str
    on: date
    requested: str
    provided: str | None
    reason: str | None
    understanding: float | None


@dataclass(frozen=True)
class SupplyChange:
    id: str
    service_id: str
    effective_on: date
    received_on: date
    audio_limit: int
    devices_available: int
    reason: str


@dataclass
class DataBundle:
    schools: list[School]
    people: list[Person]
    materials: list[Material]
    experiences: list[Experience]
    services: list[Service]
    commitments: list[Commitment]
    requests: list[ServiceRequest]
    supply_changes: list[SupplyChange]
    raw_digest: str

    def __post_init__(self):
        self.school_by_id = {s.id: s for s in self.schools}
        self.person_by_id = {p.id: p for p in self.people}
        self.material_by_id = {m.id: m for m in self.materials}
        self.service_by_id = {s.id: s for s in self.services}
        self.experiences_by_person: dict[str, list[Experience]] = {}
        for ex in self.experiences:
            self.experiences_by_person.setdefault(ex.person_id, []).append(ex)
        for rows in self.experiences_by_person.values():
            rows.sort(key=lambda x: x.order)
        self.commitments_by_service: dict[str, list[Commitment]] = {}
        for c in self.commitments:
            self.commitments_by_service.setdefault(c.service_id, []).append(c)
        self.requests_by_service: dict[str, list[ServiceRequest]] = {}
        for r in self.requests:
            self.requests_by_service.setdefault(r.service_id, []).append(r)
        self.changes_by_service: dict[str, list[SupplyChange]] = {}
        for ch in self.supply_changes:
            self.changes_by_service.setdefault(ch.service_id, []).append(ch)
        self.people_by_school: dict[str, list[Person]] = {}
        for p in self.people:
            self.people_by_school.setdefault(p.school_id, []).append(p)
        self.household_members: dict[str, list[str]] = {}
        for p in self.people:
            self.household_members.setdefault(p.household, []).append(p.id)

    def people_of_service(self, service_id: str) -> list[Person]:
        return self.people_by_school[self.service_by_id[service_id].school_id]

    def household_of(self, person_id: str) -> str:
        return self.person_by_id[person_id].household


def load_bundle(materials_dir: str | Path) -> DataBundle:
    """读取并校验合成资料；返回带索引的 DataBundle 与原始资料指纹。"""
    materials_dir = Path(materials_dir)
    raw = (materials_dir / "data.json").read_bytes()
    data = json.loads(raw.decode("utf-8"))
    contract = json.loads((materials_dir / "contract.json").read_text(encoding="utf-8"))
    if set(data) != set(contract["tables"]):
        raise ValueError("资料表与契约不一致")

    def rows(table):
        meta = contract["tables"][table]
        out = data[table]
        if not out:
            raise ValueError("资料表为空：" + table)
        ids = [r["id"] for r in out]
        if len(ids) != len(set(ids)):
            raise ValueError("资料编号重复：" + table)
        for row in out:
            if set(row) != set(meta["fields"]):
                raise ValueError("字段与契约不一致：" + table)
        for column, target in meta["references"].items():
            valid = {r["id"] for r in data[target]}
            if any(row[column] not in valid for row in out):
                raise ValueError("关联资料不存在：" + table + "." + column)
        return out

    schools = [School(r["id"], r["area"], int(r["devices"])) for r in rows("schools")]
    people = [
        Person(r["id"], r["school_id"], r["household"], int(r["grade"]), r["access_need"], float(r["sample_weight"]))
        for r in rows("people")
    ]
    materials = [
        Material(r["id"], r["family"], r["format"], int(r["grade_low"]), int(r["grade_high"]))
        for r in rows("materials")
    ]
    experiences = []
    for r in rows("experiences"):
        _check_enum("experiences.assignment", r["assignment"], ASSIGNMENTS)
        if r["actual_format"] is not None:
            _check_enum("experiences.actual_format", r["actual_format"], FORMATS)
        _check_enum("experiences.exit_reason", r["exit_reason"], (*EXIT_REASONS, None))
        if r["understanding"] is not None and not 0 <= r["understanding"] <= 100:
            raise ValueError("理解原分越界：" + r["id"])
        if r["burden"] is not None and not 1 <= r["burden"] <= 5:
            raise ValueError("负担取值越界：" + r["id"])
        experiences.append(
            Experience(
                r["id"], r["person_id"], r["material_id"], int(r["order"]), _d(r["planned_on"]),
                int(r["washout_days"]), r["assignment"], r["actual_format"],
                None if r["understanding"] is None else float(r["understanding"]),
                r["burden"], r["exit_reason"],
            )
        )
    services = []
    for r in rows("services"):
        services.append(
            Service(
                r["id"], r["school_id"], int(r["devices"]), int(r["concurrent_audio"]),
                int(r["concurrent_text"]), _d(r["license_end"]), _t(r["network_start"]),
                _t(r["network_end"]), float(r["teacher_hours"]),
            )
        )
    commitments = []
    for r in rows("commitments"):
        _check_enum("commitments.format", r["format"], FORMATS)
        _check_enum("commitments.kind", r["kind"], COMMITMENT_KINDS)
        commitments.append(
            Commitment(
                r["id"], r["person_id"], r["service_id"], r["format"], r["kind"],
                _d(r["from_on"]), _d(r["to_on"]), _d(r["confirmed_on"]),
            )
        )
    requests = []
    for r in rows("requests"):
        _check_enum("requests.requested", r["requested"], FORMATS)
        if r["provided"] is not None:
            _check_enum("requests.provided", r["provided"], FORMATS)
        _check_enum("requests.reason", r["reason"], (*REQUEST_REASONS, None))
        requests.append(
            ServiceRequest(
                r["id"], r["person_id"], r["service_id"], _d(r["on"]), r["requested"],
                r["provided"], r["reason"],
                None if r["understanding"] is None else float(r["understanding"]),
            )
        )
    changes = [
        SupplyChange(
            r["id"], r["service_id"], _d(r["effective_on"]), _d(r["received_on"]),
            int(r["audio_limit"]), int(r["devices_available"]), r["reason"],
        )
        for r in rows("supply_changes")
    ]
    #跨表业务口径校验
    person_ids = {p.id for p in people}
    for ex in experiences:
        if ex.person_id not in person_ids:
            raise ValueError("体验关联了不存在的学生：" + ex.id)
    for c in commitments:
        if c.person_id not in person_ids:
            raise ValueError("承诺关联了不存在的学生：" + c.id)
    for rq in requests:
        if rq.person_id not in person_ids:
            raise ValueError("请求关联了不存在的学生：" + rq.id)
    return DataBundle(
        schools, people, materials, experiences, services, commitments, requests, changes,
        hashlib.sha256(raw).hexdigest(),
    )
