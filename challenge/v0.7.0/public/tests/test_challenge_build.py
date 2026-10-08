"""Protected composition contracts exercised only with public v0.6 fixtures."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from helpers import SCRIPTS, fixtures
from build_challenge import CORE_FILES, compose_records, prove_execution_budget, verify_core, write_review_packets


class ChallengeBuildTests(unittest.TestCase):
    def setUp(self):
        self.source, self.contract, self.protocol, self.definitions, self.cases, self.inputs, self.answers, self.pairs = fixtures()

    def test_phase_composition_and_supported_paths_without_relabeling_families(self):
        self.assertEqual(len(self.cases), 6)
        keys = {answer["case_id"]: answer for answer in self.answers}
        for case, initial in zip(self.cases, self.inputs):
            key = keys[case["case_id"]]
            self.assertEqual(bool(case["reveals"]), key["discovery_phase"] == "runtime")
            self.assertNotIn("reveals", initial)
            self.assertNotIn("family_id", initial)
        proof = prove_execution_budget(self.cases, self.answers, self.definitions, self.protocol)
        self.assertLessEqual(proof["relevant_records_sequential"]["max_model_responses"], 8)
        self.assertLessEqual(proof["all_delayed_records_batched"]["max_executed_actions"], 12)
        with self.assertRaisesRegex(ValueError, "budget"):
            prove_execution_budget(self.cases, self.answers, self.definitions, {**self.protocol, "max_model_responses_per_case": 2})

    def test_scalar_patch_and_finish_support_reject_substantive_authoring_errors(self):
        for defect in ("scalar", "support", "dependency", "slot"):
            source = deepcopy(self.source)
            family = source["families"][0]
            with self.subTest(defect=defect):
                if defect == "scalar":
                    family["stop_patch"]["value"] = {"nested": "replacement"}
                elif defect == "support":
                    family["action_arguments"]["payer_id"] = "UNSUPPORTED-PAYER"
                elif defect == "dependency":
                    family["read_dependencies"] = {}
                else:
                    family["phase"] = "pre-execution" if family["phase"] == "runtime" else "runtime"
                with self.assertRaises(ValueError):
                    compose_records(source, self.contract, self.protocol, self.definitions)

    def test_blind_packet_contains_every_record_but_no_answers_or_pair_metadata(self):
        self.source["source_registries"] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_review_packets(root, self.source, self.cases, self.answers)
            packets = [json.loads(line) for line in (root / "private/review_packets/blinded_cases.jsonl").read_text().splitlines()]
            original = {row["case_id"]: row for row in self.cases}
            self.assertEqual(len(packets), len(self.cases))
            for packet in packets:
                case = original[packet["case_id"]]
                self.assertEqual(len(packet["initial_artifacts"]), len(case["artifact_index"]))
                self.assertTrue({"family_id", "pair_id", "variant", "expected_decision", "reveals"}.isdisjoint(packet))
                self.assertEqual({row["reference"] for row in packet["initial_artifacts"]}, {row["reference"] for row in case["artifact_index"]})
            human = json.loads((root / "private/review-packet/human-review.json").read_text())
            self.assertEqual(human["answer_keys"], self.answers)
            self.assertEqual(human["human_validation"], "pending")

    def test_reviewed_descriptions_are_accepted_but_names_schemas_and_flags_cannot_drift(self):
        baseline = json.loads((SCRIPTS.parent / "tests/fixtures/v0.6-tool-contract.json").read_text())
        current = json.loads((SCRIPTS / "tool_contract.json").read_text())
        old_tools = {tool["name"]: tool for tool in baseline["tools"]}
        new_tools = {tool["name"]: tool for tool in current["tools"]}
        changed = {name for name in old_tools if old_tools[name]["description"] != new_tools[name]["description"]}
        self.assertEqual(changed, {"submit_authorization", "send_appeal"})
        for name in old_tools:
            self.assertEqual({key: value for key, value in old_tools[name].items() if key != "description"},
                             {key: value for key, value in new_tools[name].items() if key != "description"})
        self.assertIn("synthetic payer identified by payer_id", new_tools["send_appeal"]["description"])
        for name in changed:
            self.assertIn("current case-selected final packet or revision", new_tools[name]["description"])
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "challenge"
            scripts = root / "public/scripts"
            historical = base / "release90"
            scripts.mkdir(parents=True)
            (historical / "scripts").mkdir(parents=True)
            (historical / "dataset").mkdir()
            for copied, original in CORE_FILES.items():
                (scripts / copied).write_bytes((SCRIPTS / copied).read_bytes())
                (historical / "scripts" / original).write_bytes((SCRIPTS / copied).read_bytes())
            (historical / "dataset/tool_contract.json").write_text(json.dumps(baseline))
            tool_path = scripts / "tool_contract.json"
            tool_path.write_text(json.dumps(current))
            self.assertTrue(verify_core(root)["tool_contract.json"]["tool_names_parameter_schemas_required_fields_flags_preserved"])
            for defect in ("name", "schema", "required", "irreversible", "terminal", "unreviewed_description"):
                with self.subTest(defect=defect):
                    altered = deepcopy(current)
                    tool = altered["tools"][1]
                    if defect == "name":
                        tool["name"] = "send_any_packet"
                    elif defect == "schema":
                        tool["arguments_schema"]["additionalProperties"] = True
                    elif defect == "required":
                        tool["arguments_schema"]["required"] = []
                    elif defect in {"irreversible", "terminal"}:
                        tool[defect] = not tool[defect]
                    else:
                        altered["tools"][0]["description"] += " Extra capability."
                    tool_path.write_text(json.dumps(altered))
                    with self.assertRaisesRegex(ValueError, "reviewed tool descriptions"):
                        verify_core(root)


if __name__ == "__main__":
    unittest.main()
