"""Guard the data boundaries and controlled-pair contracts used by evaluators."""

from copy import deepcopy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_dataset import agent_view
from validate_dataset import read_jsonl, validate_directory, validate_records


class DatasetIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.cases = read_jsonl(ROOT / "dataset" / "cases.jsonl")
        self.inputs = read_jsonl(ROOT / "dataset" / "agent_inputs.jsonl")
        self.answers = read_jsonl(ROOT / "dataset" / "answers.jsonl")
        self.pairs = read_jsonl(ROOT / "dataset" / "pairs.jsonl")
        self.assertEqual(self.errors(), [], "Baseline dataset must be valid before corruption")

    def errors(self):
        return validate_records(self.cases, self.inputs, self.answers, self.pairs)

    def refresh_input(self, case):
        index = next(index for index, item in enumerate(self.inputs) if item["case_id"] == case["case_id"])
        self.inputs[index] = agent_view(case)

    def test_hidden_runtime_record_cannot_be_in_initial_agent_input(self):
        case = next(item for item in self.cases if item["reveals"])
        initial = next(item for item in self.inputs if item["case_id"] == case["case_id"])
        initial["initial_artifacts"].append(deepcopy(case["reveals"][0]["artifact"]))
        self.assertTrue(any("agent input includes delayed evidence" in error for error in self.errors()))

    def test_grading_label_cannot_be_smuggled_into_record_body(self):
        case = self.cases[0]
        case["initial_artifacts"][0]["data"]["expected_decision"] = "escalate"
        self.refresh_input(case)
        self.assertTrue(any("evaluator labels leaked" in error for error in self.errors()))

    def test_second_twin_mutation_is_rejected(self):
        pair = self.pairs[0]
        case = next(item for item in self.cases if item["case_id"] == pair["stop_case_id"])
        case["initial_artifacts"][0]["data"]["unplanned_record_change"] = "changed"
        self.refresh_input(case)
        self.assertTrue(any("more than the declared single field" in error for error in self.errors()))

    def test_omitted_pair_cannot_silently_reduce_denominator(self):
        self.pairs.pop()
        self.assertIn("Pair manifest does not cover every finish and stop case exactly once", self.errors())

    def test_handoff_key_cannot_cite_nonexistent_record(self):
        answer = next(item for item in self.answers if item["expected_decision"] == "escalate")
        answer["escalation"]["evidence_refs"].append("ehr://NOT-IN-CASE/record")
        self.assertTrue(any("answer cites unavailable evidence" in error for error in self.errors()))

    def test_completion_evidence_must_be_observable(self):
        answer = next(item for item in self.answers if item["expected_decision"] == "finish")
        answer["must_observe_refs"].append("ehr://NOT-IN-CASE/record")
        self.assertTrue(any("required observations cite unavailable" in error for error in self.errors()))

    def test_circular_read_requirements_cannot_make_decisive_record_unreachable(self):
        case = next(item for item in self.cases if item["reveals"])
        reveal = case["reveals"][0]
        reveal["requires_refs"] = [reveal["artifact"]["reference"]]
        self.refresh_input(case)
        self.assertTrue(any("read prerequisites create an unreachable cycle" in error for error in self.errors()))

    def test_public_fingerprint_detects_semantically_identical_byte_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            shutil.copytree(ROOT / "dataset", dataset)
            validate_directory(dataset)
            path = dataset / "agent_inputs.jsonl"
            data = path.read_bytes()
            path.write_bytes(data.replace(b"\n", b" \n", 1))
            with self.assertRaisesRegex(ValueError, "agent_inputs.jsonl: file bytes do not match manifest"):
                validate_directory(dataset)


if __name__ == "__main__":
    unittest.main()
