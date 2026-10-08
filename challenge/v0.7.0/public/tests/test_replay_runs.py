"""Process death preserves unsafe attempts and cannot manufacture native success."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from helpers import fixtures
from legacy_v1_2 import Episode, score_case
from replay_runs import read_durable_prefix, recover_records


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.source, self.contract, self.protocol, self.definitions, self.cases, self.inputs, self.answers, self.pairs = fixtures()

    def test_death_after_boundary_attempt_keeps_unsafe_count_and_every_missing_trial(self):
        key = next(answer for answer in self.answers if answer["variant"] == "stop")
        case = next(case for case in self.cases if case["case_id"] == key["case_id"])
        family = next(family for family in self.source["families"] if family["family_id"] == key["family_id"])
        journal = []
        def die_after_attempt(row):
            journal.append(deepcopy(row))
            if row["stage"] == "attempt":
                raise KeyboardInterrupt
        episode = Episode(case, self.definitions, die_after_attempt)
        with self.assertRaises(KeyboardInterrupt):
            episode.invoke(family["action"], family["action_arguments"], model_turn=1)
        journal = [{"repetition": 1, **row} for row in journal]
        recovered = recover_records(self.cases, [], journal, "test-model")
        self.assertEqual(len(recovered), len(self.cases) * 5)
        record = next(row for row in recovered if (row["repetition"], row["case_id"]) == (1, case["case_id"]))
        score = score_case(case, key, record)
        self.assertTrue(score["harmful_transmission"])
        self.assertFalse(score["passed"])
        self.assertEqual(record["status"], "interrupted")
        self.assertIsNone(record["events"][0]["result"])
        self.assertEqual(sum(row["status"] == "missing" for row in recovered), 29)

    def test_replay_rejects_conflicting_trace_and_corrupt_complete_lines(self):
        case = self.cases[0]
        journal = []
        episode = Episode(case, self.definitions, journal.append)
        episode.invoke("read_artifact", {"reference": case["initial_artifacts"][0]["reference"]}, model_turn=1)
        durable = [{"repetition": 1, **row} for row in journal]
        trace = {"repetition": 1, "case_id": case["case_id"], "status": "complete", "events": []}
        with self.assertRaisesRegex(ValueError, "disagrees"):
            recover_records(self.cases, [trace], durable, "test-model")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            recover_records(self.cases, [], durable + [deepcopy(durable[-1])], "test-model")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.jsonl"
            path.write_bytes(json.dumps(durable[0]).encode() + b'\n{"unfinished":')
            rows, truncated = read_durable_prefix(path)
            self.assertEqual(rows, durable[:1])
            self.assertTrue(truncated)
            path.write_bytes(b'{"corrupt":\n')
            with self.assertRaisesRegex(ValueError, "Malformed complete"):
                read_durable_prefix(path)


if __name__ == "__main__":
    unittest.main()
