"""体验证据分析。

口径要点（随结果保留到 report.method_card）：
1. 效果估计只使用 assignment=random 的交叉体验；preference 臂只做描述，不能当随机效果。
2. 同一名学生在同一材料族内完成的两种格式构成一对；未完成不是零分：
   设备故障行没有实际暴露，既不计分也不计拒绝；不适退出保留负担并进入敏感性分析。
3. 自助重采样以家庭为单位；点估计使用抽样基础权重。
4. 顺序与残留影响单独检验；洗脱期在本资料中恒为 7 天，无法估计其效应。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import stats
from .models import (
    FORMAT_LABELS, FORMATS, DataBundle,
)

STATUS_COMPLETED = "completed"
STATUS_DEVICE = "device_failure"
STATUS_DISCOMFORT = "discomfort"
STATUS_OTHER = "incomplete_other"
STATUS_LABELS = {
    STATUS_COMPLETED: "完成并计分",
    STATUS_DEVICE: "设备故障（未暴露）",
    STATUS_DISCOMFORT: "不适退出（未计分，有负担）",
    STATUS_OTHER: "其他未完成（不计零分）",
}

# 证据分级阈值（业务政策参数，随方法说明一起保留）
GRADE_A = "A"  # 充分可比证据
GRADE_B = "B"  # 方向性证据，受退出/残留影响
GRADE_C = "C"  # 可比但证据不足
GRADE_D = "D"  # 无可比证据
GRADE_LABELS = {
    GRADE_A: "充分可比证据",
    GRADE_B: "方向性证据（敏感性或残留存疑）",
    GRADE_C: "可比但证据不足",
    GRADE_D: "无可比证据",
}
MIN_PAIRS_FULL = 8
MIN_PAIRS_ANY = 3
CARRYOVER_ALPHA = 0.05


@dataclass(frozen=True)
class TrialView:
    id: str
    person_id: str
    format: str
    family: str
    order: int
    assignment: str
    planned_on: date
    washout_days: int
    status: str
    understanding: float | None
    burden: int | None
    exit_reason: str | None

    @property
    def started(self) -> bool:
        return self.status != STATUS_DEVICE and self.status != STATUS_OTHER


def trial_views(bundle: DataBundle) -> list[TrialView]:
    out = []
    for ex in bundle.experiences:
        material = bundle.material_by_id[ex.material_id]
        if ex.exit_reason == "device_failure":
            status = STATUS_DEVICE
        elif ex.exit_reason == "discomfort":
            status = STATUS_DISCOMFORT
        elif ex.completed:
            status = STATUS_COMPLETED
        else:
            status = STATUS_OTHER
        out.append(TrialView(
            ex.id, ex.person_id, material.format, material.family, ex.order, ex.assignment,
            ex.planned_on, ex.washout_days, status, ex.understanding, ex.burden, ex.exit_reason,
        ))
    return out


# --------------------------------------------------------------------------- #
# 覆盖与流失表：完成、设备故障、不适分别报告，不合并成“完成率”了事
# --------------------------------------------------------------------------- #
def coverage_table(views: list[TrialView], bundle: DataBundle) -> dict:
    table = {}
    for fmt in FORMATS:
        rows = [v for v in views if v.format == fmt]
        started = [v for v in rows if v.started]
        completed = [v for v in rows if v.status == STATUS_COMPLETED]
        devices = [v for v in rows if v.status == STATUS_DEVICE]
        discomfort = [v for v in rows if v.status == STATUS_DISCOMFORT]
        other = [v for v in rows if v.status == STATUS_OTHER]
        weights = [bundle.person_by_id[v.person_id].sample_weight for v in completed]
        households = [bundle.household_of(v.person_id) for v in completed]
        if len(completed) >= 2:
            score_point, score_lo, score_hi = stats.weighted_cluster_mean_ci(
                [v.understanding for v in completed], weights, households)
        else:
            score_point = score_lo = score_hi = None
        burden_rows = [v for v in rows if v.burden is not None]
        bw = [bundle.person_by_id[v.person_id].sample_weight for v in burden_rows]
        burden_mean = stats.weighted_mean([(v.burden, w) for v, w in zip(burden_rows, bw)]) if burden_rows else None
        table[fmt] = {
            "format": fmt,
            "label": FORMAT_LABELS[fmt],
            "planned": len(rows),
            "started": len(started),
            "completed": len(completed),
            "device_failure": len(devices),
            "discomfort": len(discomfort),
            "other_incomplete": len(other),
            "completion_rate_among_started": (len(completed) / len(started)) if started else None,
            "score_mean": score_point,
            "score_ci_low": score_lo,
            "score_ci_high": score_hi,
            "mean_burden": burden_mean,
            "discomfort_rate_among_started": (len(discomfort) / len(started)) if started else None,
            "device_rate_among_planned": (len(devices) / len(rows)) if rows else None,
        }
    return table


# --------------------------------------------------------------------------- #
# 同生配对：仅随机臂、同一材料族、两种格式均完成
# --------------------------------------------------------------------------- #
def _person_index(views):
    by_person: dict[str, list[TrialView]] = {}
    for v in views:
        by_person.setdefault(v.person_id, []).append(v)
    return by_person


def _subgroup_of(person, key):
    if key == "all":
        return "全体"
    if key == "grade_band":
        return "低年级(3-4年级)" if person.grade <= 4 else "高年级(5-6年级)"
    if key == "access_need":
        return "听读必要" if person.access_need == "audio" else "普通文本必要"
    raise ValueError(key)


def _area_of(bundle, person):
    return bundle.school_by_id[person.school_id].area


def _in_subgroup(bundle, person, key, value):
    if key == "all":
        return True
    if key == "area":
        return _area_of(bundle, person) == value
    return _subgroup_of(person, key) == value


def _completed_pairs(views, bundle, arm, subgroup_key, subgroup_value, fmt_a, fmt_b):
    """返回 (差值列表, 学生id列表)；差值定义为 a - b。"""
    diffs, persons = [], []
    for pid, rows in _person_index(views).items():
        person = bundle.person_by_id[pid]
        if not rows or any(v.assignment != arm for v in rows):
            continue
        if not _in_subgroup(bundle, person, subgroup_key, subgroup_value):
            continue
        by_fmt = {v.format: v for v in rows}
        va, vb = by_fmt.get(fmt_a), by_fmt.get(fmt_b)
        if va is None or vb is None:
            continue
        if va.family != vb.family or va.status != STATUS_COMPLETED or vb.status != STATUS_COMPLETED:
            continue
        diffs.append(va.understanding - vb.understanding)
        persons.append(pid)
    return diffs, persons


@dataclass
class Sensitivity:
    scenario: str
    n_pairs: int
    n_imputed: int
    mean_difference: float


@dataclass
class PairEvidence:
    subgroup_key: str
    subgroup_value: str
    arm: str
    format_a: str
    format_b: str
    n_pairs: int
    point_difference: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    method: str | None
    sensitivity: list[Sensitivity] = field(default_factory=list)
    sensitivity_crosses_zero: bool | None = None
    grade: str = GRADE_D
    grade_reason: str = ""

    def to_dict(self):
        return {
            "subgroup": f"{self.subgroup_key}={self.subgroup_value}",
            "arm": self.arm,
            "comparison": f"{FORMAT_LABELS[self.format_a]} - {FORMAT_LABELS[self.format_b]}",
            "n_pairs": self.n_pairs,
            "mean_difference": self.point_difference,
            "ci": [self.ci_low, self.ci_high],
            "p_value": self.p_value,
            "method": self.method,
            "sensitivity": [s.__dict__ for s in self.sensitivity],
            "sensitivity_crosses_zero": self.sensitivity_crosses_zero,
            "grade": self.grade,
            "grade_label": GRADE_LABELS[self.grade],
            "grade_reason": self.grade_reason,
        }


def _sensitivity_scenarios(views, bundle, arm, subgroup_key, subgroup_value, fmt_a, fmt_b):
    """退出解释敏感性：只对已暴露但不适退出的观测做边界插补；设备故障不插补。"""
    completed_scores = {fmt: [] for fmt in FORMATS}
    for v in views:
        if v.assignment == arm and v.status == STATUS_COMPLETED:
            completed_scores[v.format].append(v.understanding)
    bounds = {}
    for fmt in FORMATS:
        vals = completed_scores[fmt]
        bounds[fmt] = (min(vals), max(vals)) if vals else (None, None)

    base_diffs, base_persons = _completed_pairs(
        views, bundle, arm, subgroup_key, subgroup_value, fmt_a, fmt_b)
    base = Sensitivity("基准（仅双方完成）", len(base_diffs), 0,
                       stats.weighted_mean([(d, bundle.person_by_id[p].sample_weight)
                                            for d, p in zip(base_diffs, base_persons)])
                       if base_diffs else None)
    opt_diffs, pes_diffs, opt_ids, pes_ids, n_imputed = [], [], [], [], 0
    for pid, rows in _person_index(views).items():
        person = bundle.person_by_id[pid]
        if not rows or any(v.assignment != arm for v in rows):
            continue
        if not _in_subgroup(bundle, person, subgroup_key, subgroup_value):
            continue
        by_fmt = {v.format: v for v in rows}
        va, vb = by_fmt.get(fmt_a), by_fmt.get(fmt_b)
        if va is None or vb is None or va.family != vb.family:
            continue
        # 设备故障：没有实际暴露，不进入任何插补场景
        if STATUS_DEVICE in (va.status, vb.status):
            continue
        # 双方完成的对已在基准中，敏感性部分只处理含不适退出的对
        if va.status == STATUS_COMPLETED and vb.status == STATUS_COMPLETED:
            continue
        if STATUS_DISCOMFORT not in (va.status, vb.status):
            continue  # 其他未完成原因不做假设
        min_a, max_a = bounds[fmt_a]
        min_b, max_b = bounds[fmt_b]
        if min_a is None or min_b is None:
            continue
        n_imputed += 1
        sa, sb = va.understanding, vb.understanding
        # 乐观：a 的缺失取该格式最高分，b 的缺失取最低分（尽量抬高 a-b）
        sa_opt = sa if sa is not None else max_a
        sb_opt = sb if sb is not None else min_b
        # 悲观：a 的缺失取最低分，b 的缺失取最高分（尽量压低 a-b）
        sa_pes = sa if sa is not None else min_a
        sb_pes = sb if sb is not None else max_b
        opt_diffs.append(sa_opt - sb_opt)
        pes_diffs.append(sa_pes - sb_pes)
        opt_ids.append(pid)
        pes_ids.append(pid)
    # 基准对也并入乐观/悲观样本（完成者分数不变）
    opt_all = base_diffs + opt_diffs
    pes_all = base_diffs + pes_diffs
    optimistic = Sensitivity(
        "乐观（不适退出取该格式边界有利值）", len(opt_all), n_imputed,
        stats.weighted_mean([(d, bundle.person_by_id[p].sample_weight)
                             for d, p in zip(opt_all, base_persons + opt_ids)]) if opt_all else None)
    pessimistic = Sensitivity(
        "悲观（不适退出取该格式边界不利值）", len(pes_all), n_imputed,
        stats.weighted_mean([(d, bundle.person_by_id[p].sample_weight)
                             for d, p in zip(pes_all, base_persons + pes_ids)]) if pes_all else None)
    return [base, optimistic, pessimistic]


def _grade_for(n_pairs, ci_low, ci_high, sensitivity, carryover_warning):
    means = [s.mean_difference for s in sensitivity if s.mean_difference is not None]
    crosses = None
    if len(means) >= 2:
        crosses = min(means) < 0 < max(means) or min(means) == 0 or max(means) == 0
    if n_pairs < MIN_PAIRS_ANY:
        return GRADE_D, "可比配对少于 %d 对" % MIN_PAIRS_ANY, crosses
    if n_pairs < MIN_PAIRS_FULL:
        return GRADE_C, "可比配对 %d 对，低于充分证据阈值 %d 对" % (n_pairs, MIN_PAIRS_FULL), crosses
    clear_direction = ci_low is not None and not (ci_low <= 0 <= ci_high)
    if not clear_direction:
        return GRADE_C, "自助置信区间跨 0，方向不明确", crosses
    if crosses or carryover_warning:
        reasons = []
        if crosses:
            reasons.append("退出解释敏感性下方向反转")
        if carryover_warning:
            reasons.append("检出残留效应风险")
        return GRADE_B, "；".join(reasons), crosses
    return GRADE_A, "配对≥%d、CI 不跨 0、敏感性稳定且无残留警告" % MIN_PAIRS_FULL, crosses


def pair_comparisons(views, bundle, carryover_warning_flags, seed=stats.DEFAULT_SEED):
    """生成全部随机臂配对证据：总体 + 年级/必要访问形式/地区分层。"""
    strata = [("all", "全体")]
    strata += [("grade_band", v) for v in ("低年级(3-4年级)", "高年级(5-6年级)")]
    strata += [("access_need", v) for v in ("听读必要", "普通文本必要")]
    strata += [("area", v) for v in sorted({s.area for s in bundle.schools})]
    out = []
    for key, value in strata:
        for i, fa in enumerate(FORMATS):
            for fb in FORMATS[i + 1:]:
                diffs, persons = _completed_pairs(views, bundle, "random", key, value, fa, fb)
                if not diffs:
                    out.append(PairEvidence(key, value, "random", fa, fb, 0, None, None, None,
                                            None, None, [], None, GRADE_D, "该分层没有可比配对"))
                    continue
                weights = [bundle.person_by_id[p].sample_weight for p in persons]
                households = [bundle.household_of(p) for p in persons]
                pr = stats.paired_estimate(diffs, weights, households, seed=seed)
                sens = _sensitivity_scenarios(views, bundle, "random", key, value, fa, fb)
                carryover_warn = carryover_warning_flags.get((fa, fb), False)
                grade, reason, crosses = _grade_for(pr.n_pairs, pr.ci_low, pr.ci_high, sens, carryover_warn)
                out.append(PairEvidence(
                    key, value, "random", fa, fb, pr.n_pairs, pr.point_difference,
                    pr.ci_low, pr.ci_high, pr.p_value, pr.method, sens, crosses, grade, reason))
    return out


# --------------------------------------------------------------------------- #
# 顺序与残留
# --------------------------------------------------------------------------- #
def order_and_carryover(views, bundle, seed=stats.DEFAULT_SEED):
    random_completed = [v for v in views if v.assignment == "random" and v.status == STATUS_COMPLETED]
    order_table = {}
    for fmt in FORMATS:
        for order in (1, 2, 3):
            rows = [v for v in random_completed if v.format == fmt and v.order == order]
            order_table[f"{fmt}:{order}"] = {
                "n": len(rows),
                "mean": stats.mean([v.understanding for v in rows]) if rows else None,
            }
    # 顺序平衡：随机臂中各顺序位上的格式计数应接近
    balance = {}
    for order in (1, 2, 3):
        balance[order] = {fmt: sum(1 for v in random_completed if v.order == order and v.format == fmt)
                          for fmt in FORMATS}
    # 简化模型：格式哑变量 + 顺序；样本不足或设计矩阵共线时返回“不可估计”标记
    y_full = [v.understanding for v in random_completed]
    washout_values = sorted({v.washout_days for v in views})
    x_full = []
    by_person = _person_index(views)
    for v in random_completed:
        prev = next((w.format for w in by_person[v.person_id] if w.order == v.order - 1), None)
        x_full.append([
            1.0 if v.format == "large_text" else 0.0,   # 相对 paper
            1.0 if v.format == "audio" else 0.0,
            float(v.order),
            1.0 if prev == "large_text" else 0.0,       # 残留项
            1.0 if prev == "audio" else 0.0,
        ])
    result = {
        "order_table": order_table,
        "sequence_balance": balance,
        "washout_days_observed": washout_values,
        "washout_note": "洗脱期恒为 %s 天，无变异，不能估计洗脱期效应，只能依赖残留项检验"
                        % ",".join(str(w) for w in washout_values),
    }
    warning_pairs = {}
    try:
        fit_reduced = stats.ols(y_full, [row[:3] for row in x_full])
        fit_full = stats.ols(y_full, [row[:5] for row in x_full])
        rss_full = sum(r * r for r in fit_full["resid"])
        rss_reduced = sum(r * r for r in fit_reduced["resid"])
        f_stat, f_p = stats.f_test_rss(rss_full, rss_reduced, 2, fit_full["dof"])
        # beta 索引：0 截距、1 large_text、2 audio、3 顺序、4 前一期large_text、5 前一期audio
        result["order_coefficient"] = {"beta": fit_reduced["beta"][3],
                                       "p_value": fit_reduced["p"][3]}
        result["carryover_f"] = {"f": f_stat, "p_value": f_p, "df": (2, fit_full["dof"]),
                                 "coefficients": [
                                     {"prior_format": "large_text", "beta": fit_full["beta"][4],
                                      "p_value": fit_full["p"][4]},
                                     {"prior_format": "audio", "beta": fit_full["beta"][5],
                                      "p_value": fit_full["p"][5]},
                                 ]}
        flagged = f_p < CARRYOVER_ALPHA
    except (ValueError, ZeroDivisionError) as exc:
        result["order_coefficient"] = {"beta": None, "p_value": None}
        result["carryover_f"] = {"f": None, "p_value": None, "df": None, "coefficients": [],
                                 "unestimable": "顺序/残留模型不可估计：%s" % exc}
        flagged = False
    if flagged:
        for i, fa in enumerate(FORMATS):
            for fb in FORMATS[i + 1:]:
                warning_pairs[(fa, fb)] = True
    result["carryover_warning"] = flagged
    return result, warning_pairs


# --------------------------------------------------------------------------- #
# 负担与不适
# --------------------------------------------------------------------------- #
def burden_and_discomfort(views, bundle, seed=stats.DEFAULT_SEED):
    burden_table = {}
    for fmt in FORMATS:
        rows = [v for v in views if v.format == fmt and v.burden is not None and v.assignment == "random"]
        if not rows:
            burden_table[fmt] = None
            continue
        weights = [bundle.person_by_id[v.person_id].sample_weight for v in rows]
        households = [bundle.household_of(v.person_id) for v in rows]
        point, lo, hi = stats.weighted_cluster_mean_ci([v.burden for v in rows], weights, households, seed=seed)
        burden_table[fmt] = {"format": fmt, "n": len(rows), "mean": point, "ci_low": lo, "ci_high": hi}
    # 同生负担差（双方都有负担记录，含不适退出者）
    burden_pair_diffs = []
    for i, fa in enumerate(FORMATS):
        for fb in FORMATS[i + 1:]:
            diffs, persons = [], []
            for pid, rows in _person_index(views).items():
                if not rows or any(v.assignment != "random" for v in rows):
                    continue
                by_fmt = {v.format: v for v in rows}
                va, vb = by_fmt.get(fa), by_fmt.get(fb)
                if va and vb and va.burden is not None and vb.burden is not None:
                    diffs.append(va.burden - vb.burden)
                    persons.append(pid)
            if len(diffs) >= 2:
                pr = stats.paired_estimate(
                    diffs, [bundle.person_by_id[p].sample_weight for p in persons],
                    [bundle.household_of(p) for p in persons], seed=seed)
                burden_pair_diffs.append({
                    "comparison": f"{FORMAT_LABELS[fa]} - {FORMAT_LABELS[fb]}",
                    "n_pairs": pr.n_pairs, "mean_difference": pr.point_difference,
                    "ci": [pr.ci_low, pr.ci_high], "p_value": pr.p_value,
                })
    discomfort = {}
    for fmt in FORMATS:
        rows = [v for v in views if v.format == fmt]
        started = [v for v in rows if v.started]
        disc = [v for v in rows if v.status == STATUS_DISCOMFORT]
        discomfort[fmt] = {
            "started": len(started), "discomfort": len(disc),
            "rate": (len(disc) / len(started)) if started else None,
        }
    return {"burden_by_format": burden_table, "burden_pair_differences": burden_pair_diffs,
            "discomfort": discomfort}


# --------------------------------------------------------------------------- #
# 偏好臂与服务请求：只描述、不做效果；来源标注
# --------------------------------------------------------------------------- #
def preference_summary(views, bundle):
    pref = [v for v in views if v.assignment == "preference"]
    people = {v.person_id for v in pref}
    sequences = {}
    for pid in people:
        seq = tuple(v.format for v in sorted([v for v in pref if v.person_id == pid], key=lambda z: z.order))
        sequences[seq] = sequences.get(seq, 0) + 1
    by_position = {order: {fmt: 0 for fmt in FORMATS} for order in (1, 2, 3)}
    for v in pref:
        by_position[v.order][v.format] += 1
    means = {}
    for fmt in FORMATS:
        scored = [v.understanding for v in pref if v.format == fmt and v.understanding is not None]
        means[fmt] = {"n": len(scored), "mean": stats.mean(scored) if scored else None}
    return {
        "n_students": len(people),
        "sequences": {",".join(FORMAT_LABELS[f] for f in seq): c for seq, c in sequences.items()},
        "format_by_position": by_position,
        "score_means_descriptive_only": means,
        "interpretation": "偏好选择臂是观察性安排：格式由学生选择，选择与基础能力相关，"
                          "其均值差不能解释为格式的因果效果，也不进入配对效果估计。",
    }


def request_provenance(bundle: DataBundle):
    """把服务请求拆成真实接受、供给驱动切换、授权拒绝三类。"""
    accepted, switched, rejected = [], [], []
    for rq in bundle.requests:
        if rq.reason is None and rq.provided == rq.requested:
            accepted.append(rq)
        elif rq.reason == "device_failure":
            switched.append(rq)
        elif rq.reason == "license_limit":
            rejected.append(rq)
    switch_dirs = {}
    for rq in switched:
        key = (rq.requested, rq.provided)
        switch_dirs[key] = switch_dirs.get(key, 0) + 1
    direction_text = "；".join(
        f"{cnt} 条 {FORMAT_LABELS[src]}→{FORMAT_LABELS[dst]}（原因 device_failure）"
        for (src, dst), cnt in sorted(switch_dirs.items()))
    return {
        "total": len(bundle.requests),
        "accepted_as_requested": len(accepted),
        "supply_driven_switch": len(switched),
        "license_rejected": len(rejected),
        "switch_detail": {
            "directions": [{"from": src, "to": dst, "count": cnt}
                           for (src, dst), cnt in sorted(switch_dirs.items())],
            "reason_field": "device_failure",
        },
        "interpretation": (
            f"{direction_text or '无切换记录'}，属于供给侧故障后的被动切换，"
            "不得计作学生偏好替代格式；授权拒绝没有实际提供，也不能视为学生放弃原请求格式。"),
    }


# --------------------------------------------------------------------------- #
# 顶层证据包
# --------------------------------------------------------------------------- #
@dataclass
class EvidenceBundle:
    digest: str
    generated_on: str
    coverage: dict
    pairs: list[PairEvidence]
    order_carryover: dict
    burden: dict
    preference: dict
    provenance: dict
    support_gaps: list
    methods: list
    limitations: list

    def to_dict(self):
        return {
            "raw_digest": self.digest,
            "generated_on": self.generated_on,
            "coverage": self.coverage,
            "pair_evidence": [p.to_dict() for p in self.pairs],
            "order_and_carryover": self.order_carryover,
            "burden_discomfort": self.burden,
            "preference_arm": self.preference,
            "request_provenance": self.provenance,
            "support_gaps": self.support_gaps,
            "methods": self.methods,
            "limitations": self.limitations,
        }


def build_evidence(bundle: DataBundle, seed=stats.DEFAULT_SEED) -> EvidenceBundle:
    views = trial_views(bundle)
    order_result, carryover_flags = order_and_carryover(views, bundle, seed=seed)
    pairs = pair_comparisons(views, bundle, carryover_flags, seed=seed)
    coverage = coverage_table(views, bundle)
    burden = burden_and_discomfort(views, bundle, seed=seed)
    preference = preference_summary(views, bundle)
    provenance = request_provenance(bundle)

    gaps = []
    for p in pairs:
        if p.grade in (GRADE_C, GRADE_D) and p.subgroup_key != "all":
            gaps.append({
                "subgroup": f"{p.subgroup_key}={p.subgroup_value}",
                "comparison": f"{FORMAT_LABELS[p.format_a]} vs {FORMAT_LABELS[p.format_b]}",
                "grade": p.grade,
                "reason": p.grade_reason,
            })
    # 没有任何随机臂暴露的格式×人群
    random_people = {v.person_id for v in views if v.assignment == "random"}
    for need in ("audio", "text"):
        for fmt in FORMATS:
            exposed = {v.person_id for v in views
                       if v.format == fmt and v.assignment == "random"
                       and bundle.person_by_id[v.person_id].access_need == need}
            if not exposed:
                gaps.append({"subgroup": f"access_need={'听读必要' if need == 'audio' else '普通文本必要'}",
                             "comparison": f"{FORMAT_LABELS[fmt]} 无随机臂暴露",
                             "grade": GRADE_D, "reason": "该人群在随机臂中从未接触该格式"})

    methods = [
        "效果估计仅使用 assignment=random 的三期交叉体验；preference 臂仅做描述性统计。",
        "配对单位为学生，要求同一材料族内两种格式均完成（有实际形式、无退出、理解分非空）。",
        "点估计为抽样基础权重加权均值差；置信区间为家庭聚类加权百分位自助（%d 次，种子 %d）。"
        % (stats.DEFAULT_BOOTSTRAP_REPS, seed),
        "配对 t 检验为等权经典检验（p 值由正则化不完全 Beta 函数给出的精确 t 分布计算），"
        "仅作方向性参考；推断以加权自助区间为准，二者口径不同，不混用。",
        "顺序效应以 OLS 顺序项检验；残留效应以前一期格式哑变量的联合 F 检验判断（α=%s）。"
        % CARRYOVER_ALPHA,
        "未完成不计零分：设备故障未暴露，不插补；不适退出在敏感性分析中取该格式已观测分数的边界值。",
        "证据分级阈值：A 需配对≥%d 且 CI 不跨 0 且敏感性稳定；B 为方向明确但敏感性/残留存疑；"
        "C 为配对 %d-%d 或方向不明确；D 为配对<%d。"
        % (MIN_PAIRS_FULL, MIN_PAIRS_ANY, MIN_PAIRS_FULL - 1, MIN_PAIRS_ANY),
    ]
    limitations = [
        "样本为合成演练资料，所有编号仅在资料包内有效，结论不可外推到真实人群。",
        "仅使用 F1 材料族，结论只对该难度联系（3-6 年级）成立。",
        "洗脱期恒为 7 天、无变异，无法估计洗脱期长短对残留的影响。",
        "资料中没有独立的缺席事件类型；当前“其他未完成”计数为 0，若未来出现缺席需单独建模。",
        "家庭可能跨学校，聚类自助按家庭处理但未做学校层级的额外随机效应建模。",
        "敏感性边界使用已观测分数的最小/最大值，是极端但非无界的假设；真实缺失机制可能更复杂。",
        "偏好臂与服务请求都是观察性数据，只能描述使用与供给，不能识别因果效果。",
    ]
    return EvidenceBundle(
        bundle.raw_digest, "2026-04-01", coverage, pairs, order_result, burden, preference,
        provenance, gaps, methods, limitations,
    )
