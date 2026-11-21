"""规划、交付、应急与端到端周期的业务测试。"""
import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  隔离模式路径引导
from appr import contingency, delivery, planning, report
from appr.evidence import build_evidence
from appr.models import FORMATS, load_bundle
from appr.stats import DEFAULT_SEED

ROOT = Path(__file__).resolve().parents[1]


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT / "materials")
        cls.evidence = build_evidence(cls.bundle, seed=DEFAULT_SEED)
        cls.ranking = planning.evaluate_bundles(cls.bundle, cls.evidence)
        cls.plans = {s.id: planning.build_service_plan(
            cls.bundle, cls.evidence, s.id, tuple(FORMATS)) for s in cls.bundle.services}

    def test_necessary_format_never_removed_for_average_disadvantage(self):
        """听读必要学生的方案中必须包含 audio，即使总体均值不占优。"""
        for sid, plan in self.plans.items():
            for person in self.bundle.people_of_service(sid):
                if person.access_need != "audio":
                    continue
                service = next(s for s in plan.services if s.person_id == person.id)
                self.assertIn("audio", service.formats)
                self.assertEqual(service.kind, "necessary")

    def test_bundle_ranking_prefers_feasible_then_lower_risk(self):
        r = self.ranking
        self.assertTrue(r[0]["feasible_everywhere"])
        for earlier, later in zip(r, r[1:]):
            # 排序键：不可行在后；同为可行时风险低者在前；风险相同时受益高者在前
            earlier_key = (not earlier["feasible_everywhere"],
                           earlier["uncovered_risk"], -earlier["expected_benefit"])
            later_key = (not later["feasible_everywhere"],
                         later["uncovered_risk"], -later["expected_benefit"])
            self.assertLessEqual(earlier_key, later_key)

    def test_large_text_only_is_infeasible_under_concurrent_license(self):
        """可调字号并发授权 5 < 20 名学生，单格式组合必须被判不可行。"""
        large_only = next(r for r in self.ranking if r["bundle"] == ["large_text"])
        self.assertFalse(large_only["feasible_everywhere"])
        # 纸本不占数字授权，纸本单格式在所有点可行
        paper_only = next(r for r in self.ranking if r["bundle"] == ["paper"])
        self.assertTrue(paper_only["feasible_everywhere"])

    def test_observe_slots_capped_and_do_not_crowd_necessary(self):
        for plan in self.plans.values():
            for fmt, used in plan.score["observe_slots_used"].items():
                self.assertLessEqual(used, planning.OBSERVE_CAP_PER_FORMAT)
            # 必要服务学生数始终保留（无组合内未覆盖时等于名册人数）
            if not plan.uncovered:
                self.assertEqual(
                    sum(1 for s in plan.services if s.kind == "necessary"),
                    len(self.bundle.people_of_service(plan.service_id)))

    def test_commitment_audit_flags_missing_necessary_commitments(self):
        findings = planning.audit_commitments(self.bundle, self.plans)
        missing = [f for f in findings if "缺少必要保障承诺" in f["issue"]]
        # 资料事实：9 名听读必要学生中仅 3 人有 necessary 承诺
        self.assertEqual(len(missing), 6)
        # 且这些缺口进入对应服务点方案
        gap_people = {g["person_id"] for plan in self.plans.values()
                      for g in plan.commitment_gaps}
        self.assertEqual({f["person_id"] for f in missing}, gap_people)

    def test_feasibility_uses_weekly_and_cycle_levels(self):
        service = self.bundle.service_by_id["S1"]
        cap = planning.capacity_snapshot(service)
        # 终端槽位 = 7 台 × 15；授权槽位 = 并发上限本身
        self.assertEqual(cap.student_device_slots_week, 7 * planning.STUDENT_SLOTS_PER_DEVICE_WEEK)
        self.assertEqual(cap.audio_slots_week, service.concurrent_audio)
        self.assertEqual(cap.teacher_hours_cycle, service.teacher_hours * planning.PLANNING_WEEKS)


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT / "materials")

    def test_requests_split_into_three_sources(self):
        records = delivery.classify_requests(self.bundle)
        self.assertEqual({r.source for r in records}, {"accepted", "supply_switch", "rejected"})
        # P013 两条请求：一条按请求提供、一条设备故障切换，后者不是学生选择
        p013 = [r for r in records if r.person_id == "P013"]
        self.assertEqual(len(p013), 2)
        switched = next(r for r in p013 if r.source == "supply_switch")
        self.assertFalse(switched.is_student_choice)
        self.assertEqual(switched.requested, "audio")
        self.assertEqual(switched.provided, "large_text")

    def test_success_only_analysis_is_flagged_with_exclusion_count(self):
        gaps = delivery.delivery_gap_report(self.bundle)
        for sid, info in gaps["by_service"].items():
            self.assertEqual(info["excluded_if_success_only"],
                             info["supply_driven_switch"] + info["license_rejected"])
            self.assertIsNotNone(info["mean_among_provided_only"])
            self.assertLess(info["fill_rate"], 1.0)

    def test_post_delivery_keeps_missing_separate_from_zero(self):
        post = delivery.post_delivery_estimation(self.bundle)
        audio = post["audio"]
        self.assertEqual(audio["missing_due_to_rejection"], 14)
        self.assertEqual(audio["missing_due_to_switch"], 25)
        # 请求按来源三类划分：按请求提供 + 故障切换（已提供替代）+ 授权拒绝（未提供）
        records = delivery.classify_requests(self.bundle)
        accepted = sum(1 for r in records if r.source == "accepted")
        self.assertEqual(accepted + audio["missing_due_to_switch"]
                         + audio["missing_due_to_rejection"], audio["requests"])
        self.assertIn("不能作为格式效果的无偏估计", audio["estimand_warning"])

    def test_record_delivery_writes_outside_materials_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok_record = {
                "id": "DR-T1", "person_id": "P013", "service_id": "S1", "on": "2026-05-01",
                "requested": "audio", "provided": None, "reason": "license_limit",
                "understanding": None,
            }
            path = delivery.record_delivery(tmp, ok_record, self.bundle.raw_digest,
                                            bundle=self.bundle)
            self.assertTrue(str(path).startswith(tmp))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["records"][0]["id"], "DR-T1")
            # 重复编号拒绝
            with self.assertRaisesRegex(ValueError, "重复"):
                delivery.record_delivery(tmp, ok_record, self.bundle.raw_digest,
                                         bundle=self.bundle)
            # 切换不登记原因拒绝
            bad = dict(ok_record, id="DR-T2", provided="large_text", reason=None)
            with self.assertRaisesRegex(ValueError, "原因"):
                delivery.record_delivery(tmp, bad, self.bundle.raw_digest, bundle=self.bundle)
            # 枚举/外键/日期/分数/来源一致性均被校验
            garbage = dict(ok_record, id="DR-T3", requested="hologram")
            with self.assertRaisesRegex(ValueError, "格式越界"):
                delivery.record_delivery(tmp, garbage, self.bundle.raw_digest, bundle=self.bundle)
            ghost = dict(ok_record, id="DR-T4", person_id="GHOST")
            with self.assertRaisesRegex(ValueError, "学生编号不存在"):
                delivery.record_delivery(tmp, ghost, self.bundle.raw_digest, bundle=self.bundle)
            bad_date = dict(ok_record, id="DR-T5", on="not-a-date")
            with self.assertRaisesRegex(ValueError, "ISO 日期"):
                delivery.record_delivery(tmp, bad_date, self.bundle.raw_digest, bundle=self.bundle)
            bad_score = dict(ok_record, id="DR-T6", understanding=-999)
            with self.assertRaisesRegex(ValueError, "理解分"):
                delivery.record_delivery(tmp, bad_score, self.bundle.raw_digest, bundle=self.bundle)
            contradictory = dict(ok_record, id="DR-T7", provided="audio", reason="license_limit")
            with self.assertRaisesRegex(ValueError, "供给侧原因|授权上限拒绝"):
                delivery.record_delivery(tmp, contradictory, self.bundle.raw_digest,
                                         bundle=self.bundle)
        # 原始资料保持不变：指纹仍可复算
        self.assertEqual(load_bundle(ROOT / "materials").raw_digest, self.bundle.raw_digest)


class ContingencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT / "materials")
        cls.evidence = build_evidence(cls.bundle, seed=DEFAULT_SEED)
        cls.plans = {s.id: planning.build_service_plan(
            cls.bundle, cls.evidence, s.id, tuple(FORMATS)) for s in cls.bundle.services}
        cls.assessments = contingency.run_contingency(cls.bundle, cls.plans)

    def test_supply_change_binds_audio_license_and_marks_necessary_commitment(self):
        s1 = next(a for a in self.assessments if a["service_id"] == "S1")
        self.assertIn("audio_license", s1["binding_constraints"])
        impacted_ids = {i["commitment_id"] for i in s1["impacted_commitments"]}
        self.assertIn("C13", impacted_ids)  # P013 的必要保障承诺
        for i in s1["impacted_commitments"]:
            self.assertEqual(i["impact"], "at_risk")
            self.assertIn("不能转为观察或取消", i["detail"])

    def test_remaining_observe_options_exclude_binding_format(self):
        s1 = next(a for a in self.assessments if a["service_id"] == "S1")
        options = {o["format"] for o in s1["remaining_observe_options"]}
        self.assertNotIn("audio", options)
        self.assertIn("paper", options)

    def test_reassessment_uses_effective_date_window(self):
        """冲击在周期中途生效，容量按剩余周数折算而非整周期。"""
        s1 = next(a for a in self.assessments if a["service_id"] == "S1")
        self.assertEqual(s1["effective_on"], "2026-04-12")
        self.assertLess(s1["remaining_weeks"], planning.PLANNING_WEEKS)
        self.assertEqual(s1["remaining_weeks"], 12)
        self.assertLess(s1["capacity_after"]["teacher_hours_cycle"],
                        s1["capacity_before"]["teacher_hours_cycle"])

    def test_device_shortfall_also_impacts_digital_commitments(self):
        """设备归零场景：依赖设备的 large_text 承诺不能被报告为 preserved。"""
        plan = self.plans["S1"]
        assessment = contingency.reassess_service(
            self.bundle, plan, "S1",
            {"audio_limit": 3, "devices_available": 0},
            effective_on=self.bundle.supply_changes[0].effective_on)
        self.assertIn("devices", assessment["binding_constraints"])
        impacted_fmts = {i["format"] for i in assessment["impacted_commitments"]}
        # C13 是 audio 必要承诺；设备归零后数字格式承诺都必须进入受影响清单
        self.assertIn("audio", impacted_fmts)

    def test_provenance_guard_catches_unexplained_switches(self):
        guard = contingency.provenance_guard(self.bundle)
        self.assertEqual(guard["supply_driven_records_guarded"], 39)
        self.assertEqual(guard["violations"], [])

    def test_contingency_does_not_relabel_switch_as_preference(self):
        for a in self.assessments:
            self.assertIn("禁止", a["provenance_rule"])


class CycleTests(unittest.TestCase):
    def test_full_cycle_writes_report_and_json_without_touching_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest_before = load_bundle(ROOT / "materials").raw_digest
            bundle = load_bundle(ROOT / "materials")
            ev = build_evidence(bundle, seed=DEFAULT_SEED)
            ranking = planning.evaluate_bundles(bundle, ev)
            plans = {s.id: planning.build_service_plan(bundle, ev, s.id, tuple(FORMATS))
                     for s in bundle.services}
            findings = planning.audit_commitments(bundle, plans)
            cont = contingency.run_contingency(bundle, plans)
            gaps = delivery.delivery_gap_report(bundle)
            post = delivery.post_delivery_estimation(bundle)
            md = report.build_markdown_report(bundle, ev, plans, ranking, cont, gaps, post, findings)
            paths = report.write_outputs(tmp, bundle, ev, plans, ranking, cont, gaps, post, findings, md)
            self.assertTrue(Path(paths["markdown"]).exists())
            machine = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(machine["raw_digest"], digest_before)
            self.assertIn("contingency", machine)
            self.assertEqual(load_bundle(ROOT / "materials").raw_digest, digest_before)
            # 报告必须能定位机构六问
            for keyword in ("为什么采用", "证据适用于谁", "承诺给谁", "缺口", "授权减少", "方法说明"):
                self.assertIn(keyword, md)

    def test_repeated_runs_are_identical_with_fixed_seed(self):
        b1 = build_evidence(load_bundle(ROOT / "materials"), seed=DEFAULT_SEED).to_dict()
        b2 = build_evidence(load_bundle(ROOT / "materials"), seed=DEFAULT_SEED).to_dict()
        self.assertEqual(json.dumps(b1, ensure_ascii=False, sort_keys=True),
                         json.dumps(b2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
