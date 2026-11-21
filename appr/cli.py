"""命令行应用：把资料 -> 证据 -> 方案 -> 交付 -> 应急 -> 报告串成可重复流程。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import contingency, delivery, planning, report
from .evidence import build_evidence
from .models import FORMATS, load_bundle
from .stats import DEFAULT_SEED


def _load(materials_dir):
    return load_bundle(materials_dir)


def cmd_evidence(args):
    bundle = _load(args.materials)
    ev = build_evidence(bundle, seed=args.seed)
    payload = ev.to_dict()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("证据分析已写入", args.output)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args):
    bundle = _load(args.materials)
    ev = build_evidence(bundle, seed=args.seed)
    chosen = tuple(args.bundle.split(",")) if args.bundle else tuple(FORMATS)
    invalid = [f for f in chosen if f not in FORMATS]
    if invalid:
        raise SystemExit("未知格式：" + ",".join(invalid))
    ranking = planning.evaluate_bundles(bundle, ev)
    plans = {s.id: planning.build_service_plan(bundle, ev, s.id, chosen) for s in bundle.services}
    findings = planning.audit_commitments(bundle, plans)
    out = {
        "chosen_bundle": list(chosen),
        "ranking": [{"bundle": r["bundle"], "feasible_everywhere": r["feasible_everywhere"],
                     "expected_benefit": r["expected_benefit"], "uncovered_risk": r["uncovered_risk"]}
                    for r in ranking],
        "plans": {sid: p.to_dict() for sid, p in plans.items()},
        "commitment_audit": findings,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("方案已写入", args.output)
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_gaps(args):
    bundle = _load(args.materials)
    print(json.dumps(delivery.delivery_gap_report(bundle), ensure_ascii=False, indent=2))
    return 0


def cmd_contingency(args):
    bundle = _load(args.materials)
    ev = build_evidence(bundle, seed=args.seed)
    plans = {s.id: planning.build_service_plan(bundle, ev, s.id, tuple(FORMATS))
             for s in bundle.services}
    out = contingency.run_contingency(bundle, plans)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_record_delivery(args):
    bundle = _load(args.materials)
    record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    path = delivery.record_delivery(args.output_dir, record, bundle.raw_digest, bundle=bundle)
    print("交付记录已追加到", path)
    return 0


def cmd_report(args):
    bundle = _load(args.materials)
    ev = build_evidence(bundle, seed=args.seed)
    chosen = tuple(args.bundle.split(",")) if args.bundle else tuple(FORMATS)
    ranking = planning.evaluate_bundles(bundle, ev)
    plans = {s.id: planning.build_service_plan(bundle, ev, s.id, chosen) for s in bundle.services}
    findings = planning.audit_commitments(bundle, plans)
    cont = contingency.run_contingency(bundle, plans)
    gaps = delivery.delivery_gap_report(bundle)
    post = delivery.post_delivery_estimation(bundle)
    md = report.build_markdown_report(bundle, ev, plans, ranking, cont, gaps, post, findings)
    paths = report.write_outputs(args.output_dir, bundle, ev, plans, ranking, cont, gaps, post,
                                 findings, md)
    print("报告已写入：", paths["markdown"])
    print("机读结果：", paths["json"])
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reading-policy",
        description="多格式阅读支持的适用证据分析与服务方案应用（合成资料，标准库实现）")
    parser.add_argument("--materials", default="materials", help="合成资料目录")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="自助随机种子（固定可复跑）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("evidence", help="运行证据分析并输出 JSON")
    p.add_argument("--output", help="输出文件路径；缺省打印到标准输出")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("plan", help="比较格式组合并生成各服务点方案")
    p.add_argument("--bundle", help="逗号分隔的格式，如 paper,audio；缺省为全部三种")
    p.add_argument("--output")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("gaps", help="交付缺口与成功者偏差诊断")
    p.set_defaults(func=cmd_gaps)

    p = sub.add_parser("contingency", help="供给变化后的可兑现性重估")
    p.set_defaults(func=cmd_contingency)

    p = sub.add_parser("record-delivery", help="录入一条实际交付/拒绝记录到 outputs")
    p.add_argument("--record", required=True, help="交付记录 JSON 文件")
    p.add_argument("--output-dir", default="outputs")
    p.set_defaults(func=cmd_record_delivery)

    p = sub.add_parser("report", help="生成完整 Markdown 报告与机读 JSON")
    p.add_argument("--bundle", help="逗号分隔的格式；缺省为全部三种")
    p.add_argument("--output-dir", default="outputs")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
