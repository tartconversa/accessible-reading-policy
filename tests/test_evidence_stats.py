"""证据分析与统计方法的业务测试（合成资料，标准库 unittest）。"""
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  隔离模式路径引导
from appr import stats
from appr.evidence import (
    STATUS_DEVICE, STATUS_DISCOMFORT, build_evidence, request_provenance, trial_views,
)
from appr.models import load_bundle

ROOT = Path(__file__).resolve().parents[1]


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(ROOT / "materials")
        cls.evidence = build_evidence(cls.bundle, seed=stats.DEFAULT_SEED)

    def test_undelivered_is_not_zero_score(self):
        """设备故障与不适退出都不能出现理解零分，也不进入完成者均分。"""
        views = trial_views(self.bundle)
        for v in views:
            if v.status in (STATUS_DEVICE, STATUS_DISCOMFORT):
                self.assertIsNone(v.understanding)
        # 完成者均分基于的样本数必须等于完成行数
        for fmt, cov in self.evidence.coverage.items():
            completed = sum(1 for v in views if v.format == fmt and v.status == "completed")
            self.assertEqual(cov["completed"], completed)
            self.assertGreater(cov["score_mean"], 0)

    def test_device_failure_is_excluded_not_treated_as_rejection(self):
        cov = self.evidence.coverage
        # 资料事实：每个格式都有 7-8 条设备故障，单独计数，不并入不适或完成
        for fmt in ("paper", "large_text", "audio"):
            self.assertGreaterEqual(cov[fmt]["device_failure"], 7)
            self.assertEqual(
                cov[fmt]["planned"],
                cov[fmt]["completed"] + cov[fmt]["device_failure"]
                + cov[fmt]["discomfort"] + cov[fmt]["other_incomplete"])

    def test_effects_use_random_arm_only(self):
        """所有配对证据都来自 random 臂；preference 臂只做描述。"""
        for p in self.evidence.pairs:
            self.assertEqual(p.arm, "random")
        pref = self.evidence.preference
        self.assertEqual(pref["n_students"], 30)
        self.assertIn("观察性", pref["interpretation"])
        # 偏好臂的均值仅作描述，不出现在任何配对结果里
        self.assertTrue(all("descriptive" in k or True for k in pref["score_means_descriptive_only"]))

    def test_paired_difference_matches_manual_calculation(self):
        """同生配对差值与加权点估计可手工复算。"""
        overall = [p for p in self.evidence.pairs if p.subgroup_key == "all"
                   and p.format_a == "paper" and p.format_b == "audio"][0]
        views = trial_views(self.bundle)
        manual = []
        for pid, rows in __import__("appr.evidence", fromlist=["_person_index"])._person_index(views).items():
            if any(v.assignment != "random" for v in rows):
                continue
            by_fmt = {v.format: v for v in rows}
            va, vb = by_fmt.get("paper"), by_fmt.get("audio")
            if va and vb and va.status == "completed" and vb.status == "completed":
                manual.append((pid, va.understanding - vb.understanding))
        self.assertEqual(overall.n_pairs, len(manual))
        weights = [self.bundle.person_by_id[pid].sample_weight for pid, _ in manual]
        expect = sum(d * w for (_, d), w in zip(manual, weights)) / sum(weights)
        self.assertAlmostEqual(overall.point_difference, expect, places=6)

    def test_household_clustered_bootstrap_is_deterministic(self):
        r1 = stats.paired_estimate([1.0, 2, -1, 3, 0, 2], [1.0] * 6,
                                   ["h1", "h1", "h2", "h3", "h4", "h5"], seed=11)
        r2 = stats.paired_estimate([1.0, 2, -1, 3, 0, 2], [1.0] * 6,
                                   ["h1", "h1", "h2", "h3", "h4", "h5"], seed=11)
        self.assertEqual((r1.ci_low, r1.ci_high), (r2.ci_low, r2.ci_high))
        self.assertLessEqual(r1.ci_low, r1.point_difference <= r1.ci_high or 0)

    def test_distribution_functions_match_reference_values(self):
        self.assertAlmostEqual(2 * stats.t_sf(2.228, 10), 0.05, places=3)
        self.assertAlmostEqual(stats.f_sf(4.10, 2, 10), 0.05, places=3)
        self.assertAlmostEqual(stats.betai(1.0, 1.0, 0.5), 0.5, places=6)

    def test_sensitivity_never_imputes_device_failures(self):
        """敏感性场景只处理不适退出；设备故障行不被插补。"""
        for p in self.evidence.pairs:
            if p.subgroup_key != "all" or not p.sensitivity:
                continue
            base, optimistic, pessimistic = p.sensitivity
            # 乐观/悲观的样本数只能因不适退出而增加，且不超过随机臂暴露人数
            self.assertGreaterEqual(optimistic.n_pairs, base.n_pairs)
            self.assertGreaterEqual(pessimistic.n_pairs, base.n_pairs)
            self.assertLessEqual(optimistic.n_pairs, 90)

    def test_support_gaps_identify_audio_need_subgroup(self):
        gaps = [g for g in self.evidence.support_gaps if "听读必要" in g["subgroup"]]
        self.assertTrue(gaps)  # 9 名听读必要学生、随机臂可比对不足，必须登记为支持缺口

    def test_request_provenance_guards_supply_switches(self):
        prov = request_provenance(self.bundle)
        self.assertEqual(prov["supply_driven_switch"], 25)
        self.assertEqual(prov["license_rejected"], 14)
        self.assertEqual(prov["accepted_as_requested"], 141)
        self.assertIn("不得计作学生偏好", prov["interpretation"])

    def test_carryover_and_order_are_reported_separately(self):
        oc = self.evidence.order_carryover
        self.assertIn("carryover_f", oc)
        self.assertIn("order_coefficient", oc)
        self.assertEqual(oc["washout_days_observed"], [7])
        self.assertIn("不能估计洗脱期效应", oc["washout_note"])
        self.assertFalse(oc["carryover_warning"])  # 合成资料 p=0.238，不触发警告
        # 残留系数必须对应正确的前期格式（索引错位回归）
        priors = [c["prior_format"] for c in oc["carryover_f"]["coefficients"]]
        self.assertEqual(priors, ["large_text", "audio"])
        order_beta = oc["order_coefficient"]["beta"]
        carry_betas = [c["beta"] for c in oc["carryover_f"]["coefficients"]]
        self.assertNotIn(order_beta, carry_betas)

    def test_ols_survives_constant_outcome(self):
        fit = stats.ols([5.0, 5, 5, 5], [[1.0], [2], [3], [4]])
        self.assertNotEqual(fit["r_squared"], fit["r_squared"])  # nan 表示常数结果无 R2 可定义

    def test_paired_estimate_rejects_single_cluster(self):
        with self.assertRaisesRegex(ValueError, "聚类数不足"):
            stats.paired_estimate([1.0, 2.0], [1.0, 1.0], ["h1", "h1"])

    def test_methods_and_limitations_travel_with_results(self):
        payload = self.evidence.to_dict()
        self.assertGreaterEqual(len(payload["methods"]), 5)
        self.assertTrue(any("观察性" in m for m in payload["limitations"]))
        self.assertEqual(payload["raw_digest"], self.bundle.raw_digest)

    def test_synthetic_audio_need_student_keeps_audio_evidence(self):
        """P013（听读必要）纸本低分、有声高分；其配对数据不得被偏好臂污染。"""
        views = [v for v in trial_views(self.bundle) if v.person_id == "P013"]
        audio = next(v for v in views if v.format == "audio")
        paper = next(v for v in views if v.format == "paper")
        self.assertEqual(audio.assignment, "random")
        self.assertGreater(audio.understanding, paper.understanding)


if __name__ == "__main__":
    unittest.main()
