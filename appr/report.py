"""把证据、方案、交付缺口与应急重估写成可复核的服务点报告。

报告必须能回答机构的六个问题：
1. 服务点为什么采用这组格式；2. 证据适用于谁；3. 承诺给谁却未能提供；
4. 缺口如何影响下一轮分析与继续服务；5. 供给冲击影响哪些持续服务；
6. 方法与限制是什么。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import (
    COMMITMENT_LABELS, FORMAT_LABELS, FORMATS, DataBundle,
)
from .planning import OBSERVE_CAP_PER_FORMAT


def _fmt_num(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}"


def _coverage_lines(evidence):
    lines = ["| 格式 | 计划 | 开始 | 完成 | 设备故障 | 不适退出 | 完成者均分(自助CI) | 平均负担 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for fmt in FORMATS:
        c = evidence.coverage[fmt]
        ci = "—"
        if c["score_mean"] is not None:
            ci = f"{c['score_mean']:.2f} ({c['score_ci_low']:.2f}–{c['score_ci_high']:.2f})"
        lines.append("| %s | %d | %d | %d | %d | %d | %s | %s |" % (
            FORMAT_LABELS[fmt], c["planned"], c["started"], c["completed"],
            c["device_failure"], c["discomfort"], ci, _fmt_num(c["mean_burden"])))
    return lines


def _pair_lines(evidence, subgroup_key=None):
    lines = ["| 分层 | 比较（前者-后者） | 配对数 | 加权均值差 | 自助95%CI | p | 敏感性方向 | 等级 | 依据 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for p in evidence.pairs:
        if subgroup_key and p.subgroup_key != subgroup_key:
            continue
        sens = "—"
        if p.sensitivity:
            vals = [s.mean_difference for s in p.sensitivity if s.mean_difference is not None]
            if vals:
                sens = f"{min(vals):.2f} 至 {max(vals):.2f}" + ("（跨0）" if min(vals) < 0 < max(vals) else "")
        lines.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s |" % (
            f"{p.subgroup_key}={p.subgroup_value}",
            f"{FORMAT_LABELS[p.format_a]} - {FORMAT_LABELS[p.format_b]}",
            p.n_pairs, _fmt_num(p.point_difference),
            "—" if p.ci_low is None else f"{p.ci_low:.2f} 至 {p.ci_high:.2f}",
            _fmt_num(p.p_value, 3), sens, p.grade, p.grade_reason))
    return lines


def build_service_answer(bundle: DataBundle, evidence, plan, contingency_assessments,
                         delivery_report, post_delivery) -> list[str]:
    sid = plan.service_id
    service = bundle.service_by_id[sid]
    lines = [f"## 服务点 {sid}（学校 {service.school_id}）", ""]

    # 问题 1：为什么采用这组格式
    lines += ["### 1. 为什么采用这组格式组合", ""]
    bundle_labels = "、".join(FORMAT_LABELS[f] for f in plan.bundle)
    lines.append(f"入选组合：{bundle_labels}。入选依据是随机交叉臂的同生配对证据分级，"
                 "而不是三组平均完成率：完成率把设备故障与不适退出混在一起，且格式在部分学校并不可比。")
    for f in plan.bundle:
        related = [p for p in evidence.pairs if p.subgroup_key == "all" and f in (p.format_a, p.format_b)]
        grades = sorted({p.grade for p in related})
        lines.append(f"- {FORMAT_LABELS[f]}：总体证据等级 {','.join(grades) or 'D'}；"
                     f"容量口径见下方约束表。")
    feas = plan.feasibility
    lines.append("")
    lines.append("容量约束（周期口径，双周一次/学生/格式）：")
    for name in ("devices", "audio_license", "text_license", "teacher_hours",
                 "network_for_audio", "license_window"):
        c = feas[name]
        if "demand" in c and "limit" in c:
            lines.append(f"- {name}：需求 {c['demand']}，容量 {c['limit']}，{'通过' if c['ok'] else '不通过'}")
        elif "demand" in c:
            lines.append(f"- {name}：需求 {c['demand']}，{'通过' if c['ok'] else '不通过'}")
        else:
            lines.append(f"- {name}：{'通过' if c['ok'] else '不通过'}")
    if plan.warnings:
        lines.append("- 方案警告：" + "；".join(plan.warnings))
    lines.append("")

    # 问题 2：证据适用于谁
    lines += ["### 2. 证据适用于谁、哪些人群尚无支持", ""]
    lines += ["总体配对证据（机构层估计，适用范围以分层结果为准）：", ""]
    lines += _pair_lines(evidence, subgroup_key="all")
    lines.append("")
    for key, title in (("grade_band", "年级分层"), ("access_need", "必要访问形式分层"), ("area", "地区分层")):
        rows = [p for p in evidence.pairs if p.subgroup_key == key]
        weak = [p for p in rows if p.grade in ("C", "D")]
        lines.append(f"- {title}：{len(rows)} 项比较，其中 {len(weak)} 项证据不足（C/D）。")
    gaps = [g for g in evidence.support_gaps]
    if gaps:
        lines.append("- 尚无充分支持的人群/格式：")
        for g in gaps[:8]:
            lines.append(f"  - {g['subgroup']}｜{g['comparison']}｜等级 {g['grade']}：{g['reason']}")
        if len(gaps) > 8:
            lines.append(f"  - 其余 {len(gaps) - 8} 项见机读 JSON。")
    lines.append("")

    # 问题 3：承诺给谁却未能提供
    lines += ["### 3. 承诺给谁、实际提供了什么、缺口在哪", ""]
    dr = delivery_report["by_service"][sid]
    fill = "无请求" if dr["fill_rate"] is None else f"{dr['fill_rate']:.2%}"
    lines.append(f"服务请求 {dr['total_requests']} 条：按请求提供 {dr['accepted']} 条、"
                 f"设备故障被动切换 {dr['supply_driven_switch']} 条、授权上限拒绝 {dr['license_rejected']} 条，"
                 f"填充率 {fill}。")
    lines.append(f"若只分析成功提供的记录，将排除 {dr['excluded_if_success_only']} 条缺口记录，"
                 f"理解均值被抬高至 {_fmt_num(dr['mean_among_provided_only'])}（仅按请求提供者为 "
                 f"{_fmt_num(dr['mean_among_accepted_only'])}）。")
    if plan.uncovered:
        lines.append("方案内未覆盖学生：")
        for u in plan.uncovered:
            lines.append(f"- {u['person_id']}：{u['reason']}")
    else:
        lines.append("本方案内必要可访问格式全部有安排，无组合内未覆盖学生。")
    if plan.commitment_gaps:
        lines.append("")
        lines.append("应承诺而尚未确认必要保障的学生：")
        for g in plan.commitment_gaps:
            lines.append(f"- {g['person_id']}（{FORMAT_LABELS[g['format']]}）：{g['reason']}")
    lines.append("")

    # 问题 4：缺口如何影响下一轮分析与继续服务
    lines += ["### 4. 缺口对下一轮分析与继续服务的影响", ""]
    lines.append("- 被拒绝与被动切换的请求保留原请求格式，下一轮估计按三类来源分别报告，"
                 "不把缺失当作零分，也不把故障切换计作偏好。")
    lines.append(f"- 继续观察名额每格式上限 {OBSERVE_CAP_PER_FORMAT} 人，只用于证据不足群体；"
                 "已确认的必要保障不被试用资源侵占。")
    pd_row = post_delivery.get("audio")
    if pd_row:
        lines.append(f"- 听读服务后观测：请求 {pd_row['requests']}、有观测 {pd_row['observed']}、"
                     f"授权拒绝 {pd_row['missing_due_to_rejection']}、故障切换 {pd_row['missing_due_to_switch']}；"
                     f"观测均值 {_fmt_num(pd_row['observed_mean'])} 仅定义在获得者上。")
    lines.append("")

    # 问题 5：供给冲击
    lines += ["### 5. 授权减少/设备故障的影响", ""]
    hit = [a for a in contingency_assessments if a["service_id"] == sid]
    if not hit:
        lines.append("本周期资料中本服务点没有供给变化记录。")
    for a in hit:
        notes = "；".join(a["capacity_after"]["notes"]) or "容量参数调整"
        lines.append(f"供给变化 {a['supply_change_id']}（{notes}）：")
        lines.append(f"- 绑定约束：{', '.join(a['binding_constraints']) or '无'}")
        for ic in a["impacted_commitments"]:
            lines.append(f"- 受影响承诺 {ic['commitment_id']}（学生 {ic['person_id']}，"
                         f"{FORMAT_LABELS[ic['format']]}，{COMMITMENT_LABELS[ic['kind']]}）：{ic['detail']}")
        lines.append(f"- 仍可观察：{', '.join(o['label'] for o in a['remaining_observe_options']) or '无'}")
        lines.append("- " + a["provenance_rule"])
    lines.append("")
    return lines


def build_markdown_report(bundle: DataBundle, evidence, plans, ranking, contingency_assessments,
                          delivery_report, post_delivery, commitment_findings) -> str:
    today = date.today().isoformat()
    lines = [
        "# 多格式阅读支持：适用证据与服务方案报告",
        "",
        f"生成日期：{today}",
        f"原始合成资料指纹（sha256）：`{bundle.raw_digest}`",
        "本报告所有数据均为合成演练资料；分析方法与限制随结果一并保留。",
        "",
        "## 一、资料口径与不能直接比较平均完成率的原因",
        "",
        "- 三种格式为有声听读、可调字号文本、普通纸本；部分格式依赖设备与并发授权，供给条件本身不同。",
        "- 体验分随机交叉（random）与偏好选择（preference）两种安排；效果证据只取自随机臂。",
        "- 设备故障（未暴露）与不适退出（已暴露、有负担、无理解分）分别记录；未完成不是零分。",
        "- 同一学生在三期交叉中按不同顺序体验三种格式，比较必须在同一学生、同一材料族内配对，",
        "  并检验顺序与残留效应；自助重采样以家庭为单位，避免把同家庭学生当独立观测。",
        "",
        "## 二、格式覆盖与退出（不合并为单一完成率）",
        "",
        *_coverage_lines(evidence),
        "",
        "## 三、同生配对效果证据（仅随机臂）",
        "",
        *_pair_lines(evidence, subgroup_key="all"),
        "",
        "### 顺序与残留效应",
        "",
        f"- 顺序项系数 {evidence.order_carryover['order_coefficient']['beta']:.3f}，"
        f"p={evidence.order_carryover['order_coefficient']['p_value']:.3f}。",
        f"- 残留联合检验 F={evidence.order_carryover['carryover_f']['f']:.3f}，"
        f"p={evidence.order_carryover['carryover_f']['p_value']:.3f}，"
        f"自由度 {evidence.order_carryover['carryover_f']['df']}；"
        f"残留警告：{'是' if evidence.order_carryover['carryover_warning'] else '否'}。",
        "- 各前期格式残留系数："
        + "；".join(f"前期为{FORMAT_LABELS[c['prior_format']]}时 β={c['beta']:.3f}（p={c['p_value']:.3f}）"
                   for c in evidence.order_carryover["carryover_f"]["coefficients"])
        if evidence.order_carryover["carryover_f"].get("coefficients")
        else "残留模型不可估计（" + evidence.order_carryover["carryover_f"].get("unestimable", "") + "）。",
        f"- {evidence.order_carryover['washout_note']}。",
        "",
        "### 负担与不适",
        "",
    ]
    for fmt in FORMATS:
        b = evidence.burden["burden_by_format"][fmt]
        if b:
            lines.append(f"- {FORMAT_LABELS[fmt]}：平均负担 {b['mean']:.2f}（CI {b['ci_low']:.2f}–"
                         f"{b['ci_high']:.2f}，n={b['n']}）；不适退出率 "
                         f"{_fmt_num(evidence.burden['discomfort'][fmt]['rate'], 3)}。")
    lines.append("")
    lines.append("同生负担变化区间（前者-后者，负值表示前者更省力，含不适退出者的负担记录）：")
    for d in evidence.burden["burden_pair_differences"]:
        lines.append(f"- {d['comparison']}：配对 {d['n_pairs']}，均值差 {d['mean_difference']:.2f}，"
                     f"自助CI {d['ci'][0]:.2f} 至 {d['ci'][1]:.2f}，p={_fmt_num(d['p_value'], 3)}")
    lines.append("")
    lines.append("理解改善区间与退出解释敏感性（总体配对，仅随机臂）：")
    for p in (x for x in evidence.pairs if x.subgroup_key == "all"):
        lines.append(f"- {FORMAT_LABELS[p.format_a]} - {FORMAT_LABELS[p.format_b]}："
                     f"改善点估计 {_fmt_num(p.point_difference)}，自助CI "
                     f"{_fmt_num(p.ci_low)} 至 {_fmt_num(p.ci_high)}；"
                     + "；".join(f"{s.scenario}={_fmt_num(s.mean_difference)}（插补 {s.n_imputed} 人）"
                                 for s in p.sensitivity))
    lines += ["", "### 偏好选择臂（只描述，不作效果）", "",
              f"- 参与学生 {evidence.preference['n_students']} 人；" + evidence.preference["interpretation"],
              "",
              "### 服务请求来源标注", "",
              f"- 总请求 {evidence.provenance['total']} 条：按请求提供 "
              f"{evidence.provenance['accepted_as_requested']}、被动切换 "
              f"{evidence.provenance['supply_driven_switch']}、授权拒绝 "
              f"{evidence.provenance['license_rejected']}。",
              "- " + evidence.provenance["interpretation"],
              "",
        "## 四、可选格式组合比较（受益 vs 未覆盖风险）",
        "",
        "| 组合 | 全部服务点可行 | 预期受益 | 未覆盖/违约风险 |",
        "| --- | --- | --- | --- |",
    ]
    for r in ranking:
        lines.append("| %s | %s | %s | %s |" % (
            "、".join(FORMAT_LABELS[f] for f in r["bundle"]),
            "是" if r["feasible_everywhere"] else "否",
            r["expected_benefit"], r["uncovered_risk"]))
    lines += ["", "## 五、承诺核对", ""]
    if not commitment_findings:
        lines.append("既有承诺与方案一致，无核对发现。")
    for f in commitment_findings:
        lines.append(f"- [{f['severity']}] {f['issue']}："
                     + ", ".join(f"{k}={v}" for k, v in f.items()
                                  if k not in ("issue", "severity")))
    lines.append("")
    for plan in plans.values():
        lines += build_service_answer(bundle, evidence, plan, contingency_assessments,
                                      delivery_report, post_delivery)
    lines += ["## 六、方法说明（随结果保留）", ""]
    lines += [f"{i + 1}. {m}" for i, m in enumerate(evidence.methods)]
    lines += ["", "## 七、限制（随结果保留）", ""]
    lines += [f"{i + 1}. {m}" for i, m in enumerate(evidence.limitations)]
    lines += ["", "## 八、机构六问的统一回答口径", "",
              "1. 为什么采用这组格式：见各服务点第 1 节（证据等级 + 容量可行性）。",
              "2. 证据适用于谁：见第 2 节分层结果与“尚无支持”清单。",
              "3. 承诺给谁却未能提供：见第 3 节请求三类来源与未覆盖清单。",
              "4. 缺口如何影响下一轮：见第 4 节，缺口保留原请求并分别报告，不做成功者分析。",
              "5. 供给冲击影响：见第 5 节，受影响承诺与剩余可观察内容。",
              "6. 方法与限制：见第六、七节；观察性使用偏好不得当作随机体验效果。",
              ""]
    return "\n".join(lines)


def write_outputs(output_dir, bundle: DataBundle, evidence, plans, ranking,
                  contingency_assessments, delivery_report, post_delivery,
                  commitment_findings, report_md: str) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    machine = {
        "generated_on": date.today().isoformat(),
        "raw_digest": bundle.raw_digest,
        "evidence": evidence.to_dict(),
        "bundle_ranking": [
            {"bundle": r["bundle"], "feasible_everywhere": r["feasible_everywhere"],
             "expected_benefit": r["expected_benefit"], "uncovered_risk": r["uncovered_risk"],
             "per_service": {sid: {"feasible": info["feasible"],
                                   "weekly_counts": info["weekly_counts"],
                                   "unsupported": info["unsupported"]}
                             for sid, info in r["per_service"].items()}}
            for r in ranking],
        "service_plans": {sid: p.to_dict() for sid, p in plans.items()},
        "commitment_audit": commitment_findings,
        "contingency": contingency_assessments,
        "delivery_gap_report": delivery_report,
        "post_delivery_estimation": post_delivery,
    }
    json_path = output_dir / "analysis.json"
    md_path = output_dir / "report.md"
    json_path.write_text(json.dumps(machine, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report_md, encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
