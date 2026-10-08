"""Protect the release-specific family phase and complete coverage contracts."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_release import compose_records, compose_source, load_plan


class ReleaseCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = compose_source(ROOT)
        cls.contract, cls.protocol, cls.definitions = load_plan(ROOT)

    def test_all_three_variants_use_the_family_phase(self):
        cases, inputs, answers, pairs = compose_records(self.source, self.contract, self.protocol, self.definitions)
        self.assertEqual((len(cases), len(pairs)), (90, 30))
        self.assertEqual(sum(row["discovery_phase"] == "pre-execution" for row in answers), 30)
        self.assertEqual(sum(row["discovery_phase"] == "runtime" for row in answers), 60)
        cases_by_id = {row["case_id"]: row for row in cases}
        inputs_by_id = {row["case_id"]: row for row in inputs}
        for trigger, phase in (("missing_information", "pre-execution"), ("ambiguous_instruction", "runtime")):
            selected = [row for row in answers if row["workflow"] == "prior_auth" and row["trigger"] == trigger]
            self.assertEqual({row["variant"] for row in selected}, {"finish", "stop", "distractor"})
            for row in selected:
                with self.subTest(trigger=trigger, variant=row["variant"]):
                    case = cases_by_id[row["case_id"]]
                    view = inputs_by_id[row["case_id"]]
                    self.assertEqual(row["discovery_phase"], phase)
                    if phase == "pre-execution":
                        self.assertEqual(case["reveals"], [])
                        self.assertEqual(len(view["initial_artifacts"]), len(view["artifact_index"]))
                    else:
                        self.assertEqual(len(view["initial_artifacts"]), 1)
                        self.assertGreater(len(case["reveals"]), 0)
                        self.assertEqual(view["initial_artifacts"][0]["label"], "Staff request")

    def test_duplicate_or_missing_coverage_slot_is_refused(self):
        duplicate = deepcopy(self.source)
        extra = deepcopy(duplicate["families"][0])
        extra["family_id"] = "accidentally-duplicated-coverage-slot"
        duplicate["families"].append(extra)
        incomplete = deepcopy(self.source)
        incomplete["families"].pop()
        for corrupted, expected in ((duplicate, "Duplicate workflow/trigger coverage slot"), (incomplete, "Missing or unexpected workflow/trigger coverage slots")):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    compose_records(corrupted, self.contract, self.protocol, self.definitions)


if __name__ == "__main__":
    unittest.main()
