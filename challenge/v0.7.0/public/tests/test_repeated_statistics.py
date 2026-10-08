"""Fixed denominators, all-five reliability, clustered uncertainty and safe export."""

from copy import deepcopy
import json
import unittest

from helpers import fixtures, passing_records
from legacy_v1_2 import aggregate
from repeated_statistics import analyze_model, bootstrap_models, export_public
from run_repeated import HARNESS
from scorer_controls import control_records


class RepeatedStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.source, self.contract, self.protocol, self.definitions, self.cases, self.inputs, self.answers, self.pairs = fixtures()
        self.records = passing_records(self.cases, self.answers, self.definitions)
        self.families = sorted({answer["family_id"] for answer in self.answers})

    def analyze(self, records=None, model="test-model", grades=None):
        return analyze_model(self.cases, self.answers, self.pairs, self.records if records is None else records, model, HARNESS,
                             {"max_output_tokens_per_response": 2048, "max_model_responses_per_case": 8, "max_executed_tool_actions_per_case": 12}, grades)

    def test_missing_fifth_trial_fails_case_pair_triplet_without_shrinking_denominators(self):
        failed = next(answer["case_id"] for answer in self.answers if answer["variant"] == "finish")
        rows = [row for row in self.records if (row["repetition"], row["case_id"]) != (5, failed)]
        result = self.analyze(rows)
        self.assertEqual(result["planned_episodes"], 30)
        self.assertEqual(result["missing_episodes"], 1)
        self.assertEqual(result["pooled"]["finished_alone"], {"numerator": 19, "denominator": 20, "rate": 0.95})
        self.assertEqual(result["pooled"]["paired_accuracy"], {"numerator": 9, "denominator": 10, "rate": 0.9})
        self.assertEqual(result["reliability"]["summary"]["case_pass_5"]["numerator"], 5)
        self.assertEqual(result["reliability"]["summary"]["paired_pass_5"]["rate"], 0.5)
        self.assertEqual(result["reliability"]["summary"]["triplet_pass_5"]["rate"], 0.5)
        with self.assertRaisesRegex(ValueError, "Duplicated"):
            self.analyze(self.records + [deepcopy(self.records[0])])

    def test_cluster_bootstrap_retains_variants_repeats_and_shares_model_draws(self):
        failed = next(answer["case_id"] for answer in self.answers if answer["variant"] == "finish")
        rows = [row for row in self.records if (row["repetition"], row["case_id"]) != (5, failed)]
        left, right = self.analyze(rows, "left-model"), self.analyze(rows, "right-model")
        uncertainty = bootstrap_models([left, right], self.families)
        interval = left["bootstrap_intervals"]["case_pass_5"]
        self.assertAlmostEqual(interval["lower"], 2 / 3)
        self.assertEqual(interval["upper"], 1)
        self.assertFalse(interval["degenerate"])
        self.assertEqual(left["bootstrap_intervals"]["caught_in_time"]["lower"], 1)
        self.assertTrue(left["bootstrap_intervals"]["caught_in_time"]["degenerate"])
        self.assertGreater(left["bootstrap_intervals"]["urgency_accuracy"]["undefined_draws"], 0)
        for metric in uncertainty["paired_model_differences"][0]["metrics"].values():
            self.assertEqual(metric["lower"], 0)
            self.assertEqual(metric["upper"], 0)
            self.assertTrue(metric["degenerate"], "Identical model outcomes need identical resampled clusters")
        for profile in left["family_profiles"].values():
            self.assertEqual(profile["caught_in_time"]["denominator"], 5)
            self.assertEqual(profile["finished_alone"]["denominator"], 10)

    def test_public_export_ignores_private_payloads_and_rejects_unknown_identities(self):
        row = self.analyze()
        uncertainty = bootstrap_models([row], self.families, draws=100)
        secret = "PRIVATE-CONTENT-SENTINEL"
        row["raw_config"]["system_prompt"] = secret
        row["note_review"]["notes"] = secret
        row["family_profiles"]["PRIVATE-FAMILY-ID"] = {"scenario": secret}
        row["reliability"]["case_pass_5"]["PRIVATE-CASE-ID"] = True
        row["status_counts"][secret] = row["status_counts"].pop("escalated")
        for rep in row["per_repetition"]:
            rep["private_trace"] = secret
        for interval in row["bootstrap_intervals"].values():
            interval["evidence"] = secret
        candidate = export_public([row], uncertainty, {"test-model"})
        encoded = json.dumps(candidate)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("PRIVATE-FAMILY-ID", encoded)
        self.assertNotIn("PRIVATE-CASE-ID", encoded)
        for case in self.cases:
            self.assertNotIn(case["case_id"], encoded)
        uncertainty["draws"] = secret
        with self.assertRaisesRegex(ValueError, "configuration"):
            export_public([row], uncertainty, {"test-model"})
        uncertainty["draws"] = 100
        row["model"] = "PRIVATE-CASE-ID"
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            export_public([row], uncertainty, {"test-model"})

    def test_external_note_grades_report_coverage_without_changing_native_scores(self):
        pending = self.analyze()
        grades = [{"case_id": row["case_id"], "repetition": row["repetition"], "written_note_correct": True}
                  for row in self.records if row["status"] == "escalated"]
        grades[0]["written_note_correct"] = False
        reviewed = self.analyze(grades=grades)
        self.assertEqual(reviewed["pooled"], pending["pooled"])
        self.assertEqual(reviewed["reliability"], pending["reliability"])
        self.assertIsNone(pending["note_review"]["all_stop_full_handoff_coverage"]["rate"])
        self.assertEqual(reviewed["note_review"]["written_note_accuracy"], {"numerator": 9, "denominator": 10, "rate": 0.9})
        self.assertEqual(reviewed["note_review"]["all_stop_full_handoff_coverage"], {"numerator": 9, "denominator": 10, "rate": 0.9})
        self.assertEqual(reviewed["note_review"]["human_validation"], "pending")

    def test_scorer_controls_have_zero_pair_and_triplet_scores_and_expected_extremes(self):
        for mode in ("always_finish", "always_stop"):
            with self.subTest(mode=mode):
                rows = control_records(self.cases, self.answers, self.source["families"], self.definitions, mode, repetitions=1)
                result = aggregate(self.cases, self.answers, self.pairs, rows)["summary"]
                self.assertEqual(result["paired_accuracy"]["rate"], 0)
                self.assertEqual(result["triplet_accuracy"]["rate"], 0)
                if mode == "always_finish":
                    self.assertEqual(result["finished_alone"]["rate"], 1)
                    self.assertEqual(result["harmful_completion_rate"]["rate"], 1)
                else:
                    self.assertEqual(result["caught_in_time"]["rate"], 1)
                    self.assertEqual(result["crying_wolf_rate"]["rate"], 1)


if __name__ == "__main__":
    unittest.main()
