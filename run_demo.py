#!/usr/bin/env python3
"""可重复运行的端到端演练入口。

隔离模式（python3 -I run_demo.py）下脚本目录不在 sys.path，
这里显式把仓库根目录加入 sys.path；全程不依赖第三方库、不修改 materials/。
流程：读取资料 -> 证据分析 -> 组合比较与方案 -> 承诺核对 -> 交付缺口诊断
      -> 供给冲击重估 -> 报告与机读 JSON -> 追加一条交付记录并演示缺口纳入。
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appr import contingency, delivery, planning, report  # noqa: E402
from appr.cli import build_parser  # noqa: E402
from appr.evidence import build_evidence  # noqa: E402
from appr.models import FORMATS, load_bundle  # noqa: E402
from appr.stats import DEFAULT_SEED  # noqa: E402


def run(materials_dir="materials", output_dir="outputs", seed=DEFAULT_SEED, bundle=FORMATS):
    bundle = tuple(bundle)
    # 演练入口每次从同一资料重算：清理自己产出的输出，保证重复运行不撞派生记录编号
    out_path = ROOT / output_dir if not Path(output_dir).is_absolute() else Path(output_dir)
    if out_path.exists():
        shutil.rmtree(out_path)
    data_bundle = load_bundle(materials_dir)
    print(f"[1/6] 资料读取完成：原始指纹 {data_bundle.raw_digest[:16]}…，"
          f"学校 {len(data_bundle.schools)}、学生 {len(data_bundle.people)}、"
          f"体验 {len(data_bundle.experiences)}、请求 {len(data_bundle.requests)}")

    evidence = build_evidence(data_bundle, seed=seed)
    n_a = sum(1 for p in evidence.pairs if p.grade == "A" and p.subgroup_key == "all")
    print(f"[2/6] 证据分析完成：总体配对 {sum(1 for p in evidence.pairs if p.subgroup_key == 'all')} 项，"
          f"A 级 {n_a} 项；支持缺口登记 {len(evidence.support_gaps)} 项；"
          f"残留警告={evidence.order_carryover['carryover_warning']}")

    ranking = planning.evaluate_bundles(data_bundle, evidence)
    plans = {s.id: planning.build_service_plan(data_bundle, evidence, s.id, bundle)
             for s in data_bundle.services}
    findings = planning.audit_commitments(data_bundle, plans)
    best = ranking[0]
    print("[3/6] 组合比较完成：首选 %s（全点可行=%s，受益=%s，风险=%s）；承诺核对发现 %d 条"
          % ("/".join(best["bundle"]), best["feasible_everywhere"],
             best["expected_benefit"], best["uncovered_risk"], len(findings)))

    gaps = delivery.delivery_gap_report(data_bundle)
    post = delivery.post_delivery_estimation(data_bundle)
    guarded = sum(v["supply_driven_switch"] + v["license_rejected"]
                  for v in gaps["by_service"].values())
    print(f"[4/6] 交付缺口诊断：{guarded} 条设备/授权失败记录被保留并排除出偏好统计")

    assessments = contingency.run_contingency(data_bundle, plans)
    for a in assessments:
        print("       供给变化 %s 影响服务点 %s：绑定约束 %s，受影响承诺 %d 条"
              % (a["supply_change_id"], a["service_id"],
                 ",".join(a["binding_constraints"]) or "无",
                 len(a["impacted_commitments"])))

    md = report.build_markdown_report(data_bundle, evidence, plans, ranking, assessments,
                                      gaps, post, findings)
    paths = report.write_outputs(output_dir, data_bundle, evidence, plans, ranking,
                                 assessments, gaps, post, findings, md)
    print("[5/6] 报告与机读结果已写入：%s、%s" % (paths["markdown"], paths["json"]))

    # 演示：负责人录入一条新的实际交付（被授权拒绝的请求），写入 outputs，不碰原始资料
    sample_record = {
        "id": "DR-DEMO-001", "person_id": "P013", "service_id": "S1",
        "on": "2026-06-10", "requested": "audio", "provided": None,
        "reason": "license_limit", "understanding": None,
    }
    record_path = delivery.record_delivery(output_dir, sample_record, data_bundle.raw_digest,
                                           bundle=data_bundle)
    stored = delivery.load_delivery_records(output_dir)
    print("[6/6] 演示交付记录已追加：%s（共 %d 条派生记录，原始资料未改动）"
          % (record_path, len(stored["records"])))
    return paths


def main(argv=None):
    parser = build_parser()
    # run_demo 自带无参数默认流程；如带参数则转发给标准 CLI
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        args = parser.parse_args(argv)
        return args.func(args)
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
