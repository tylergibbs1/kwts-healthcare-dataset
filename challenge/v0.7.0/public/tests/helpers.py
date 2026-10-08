"""Borrowed v0.6 environments and key-guided local records for public tests."""

from copy import deepcopy
import json
from pathlib import Path
import sys

PUBLIC = Path(__file__).resolve().parents[1]
SCRIPTS = PUBLIC / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_challenge import compose_records
from legacy_v1_2 import Episode


def fixtures():
    source = json.loads((PUBLIC / "tests/fixtures/public-v0.6.json").read_text())["source"]
    for family in source["families"]:
        family["reason_overlap_justification"] = "Existing public development reason overlap; no protected content used."
    contract = {"version": source["version"], "as_of": source["as_of"], "variants": ["finish", "stop", "distractor"],
                "total_families": len(source["families"]), "total_cases": len(source["families"]) * 3,
                "family_slots": [{key: family[key] for key in ("family_id", "workflow", "trigger", "phase")} for family in source["families"]]}
    protocol = {"case_clock": source["as_of"], "max_model_responses_per_case": 8, "max_executed_tool_actions_per_case": 12}
    definitions = json.loads((SCRIPTS / "tool_contract.json").read_text())["tools"]
    cases, inputs, answers, pairs = compose_records(source, contract, protocol, definitions)
    return source, contract, protocol, definitions, cases, inputs, answers, pairs


def passing_records(cases, answers, definitions, repetitions=5, model="test-model"):
    keys = {row["case_id"]: row for row in answers}
    records = []
    for repetition in range(1, repetitions + 1):
        for case in cases:
            key = keys[case["case_id"]]
            episode = Episode(case, definitions)
            for reveal in case["reveals"]:
                episode.invoke("read_artifact", {"reference": reveal["artifact"]["reference"]}, model_turn=1)
            if key["expected_decision"] == "finish":
                action = key["expected_action"]
                receipt = episode.invoke(action["tool"], action["arguments"], model_turn=2)
                result = episode.invoke("complete", {"receipt_id": receipt["receipt_id"]}, model_turn=3)
            else:
                handoff = key["escalation"]
                arguments = {name: deepcopy(handoff[name]) for name in ("reason_code", "urgency", "evidence_refs")}
                arguments["note"] = handoff["note_requirement"]
                result = episode.invoke("escalate", arguments, model_turn=2)
            records.append({"case_id": case["case_id"], "repetition": repetition, "model": model, "status": result["status"], "events": episode.events})
    return records


def response(model, calls, stop_reason="tool_use"):
    return {"model": model, "stop_reason": stop_reason, "usage": {"input_tokens": 100, "output_tokens": 50},
            "content": [{"type": "tool_use", "id": f"tool-{index}", "name": name, "input": arguments}
                        for index, (name, arguments) in enumerate(calls)]}


class Responses:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.messages = []

    def send(self, model, messages, tools, context):
        self.messages.append(deepcopy(messages))
        result = next(self.rows)
        if isinstance(result, BaseException):
            raise result
        return result
