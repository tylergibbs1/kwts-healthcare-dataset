"""Validate the data boundary, controlled twins, coverage metadata, and file hashes."""

from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

from build_dataset import ROOT, agent_view, differences

TRIGGERS = {"missing_information", "ambiguous_instruction", "conflicting_constraints", "conflicting_evidence", "authority_gap", "tool_failure", "insufficient_capability", "emergent_risk", "policy_exception", "deadline_risk"}
RESERVED = {"expected", "expected_decision", "variant", "trigger", "discovery_phase", "pair_id", "stop_patch", "reason_code", "handoff_requirement", "review_status"}
CASE_FIELDS = {"case_id", "title", "instruction", "as_of", "role", "permissions", "available_tools", "artifact_index", "initial_artifacts", "reveals"}
TOOLS = {"read_artifact", "submit_authorization", "send_appeal", "fax_records", "complete", "escalate"}
IRREVERSIBLE_TOOLS = {"submit_authorization", "send_appeal", "fax_records"}
DATA_FILES = {"cases.jsonl", "agent_inputs.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json"}


def read_jsonl(path):
    records = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{path.name}:{line_number}: blank record")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"{path.name}:{line_number}: record must be an object")
        records.append(record)
    return records


def reserved_keys(value):
    if isinstance(value, dict):
        return [key for key in value if key in RESERVED] + [key for item in value.values() for key in reserved_keys(item)]
    if isinstance(value, list):
        return [key for item in value for key in reserved_keys(item)]
    return []


def validate_records(cases, inputs, answers, pairs):
    errors = []
    indexes = {}
    for name, records, key in (("cases", cases, "case_id"), ("agent inputs", inputs, "case_id"), ("answers", answers, "case_id"), ("pairs", pairs, "pair_id")):
        keys = [record.get(key) for record in records]
        if any(not isinstance(value, str) or not value for value in keys):
            errors.append(f"{name}: invalid {key}")
            return errors
        if len(set(keys)) != len(keys):
            errors.append(f"{name}: duplicate {key}")
        indexes[name] = dict(zip(keys, records))
    if not cases:
        errors.append("The dataset is empty")
    if set(indexes["cases"]) != set(indexes["agent inputs"]) or set(indexes["cases"]) != set(indexes["answers"]):
        errors.append("Cases, agent inputs and answer keys must have identical case IDs")
        return errors
    for case in cases:
        case_id = case["case_id"]
        answer = indexes["answers"][case_id]
        if set(case) != CASE_FIELDS:
            errors.append(f"{case_id}: case structure differs from the declared fields")
            continue
        if reserved_keys(case):
            errors.append(f"{case_id}: evaluator labels leaked into case data")
        if indexes["agent inputs"][case_id] != agent_view(case):
            errors.append(f"{case_id}: agent input includes delayed evidence or differs from the initial view")
        try:
            if datetime.fromisoformat(case["as_of"]).utcoffset() is None:
                raise ValueError("missing offset")
        except (TypeError, ValueError):
            errors.append(f"{case_id}: invalid evaluation clock")
        if len(set(case["available_tools"])) != len(case["available_tools"]) or not set(case["available_tools"]).issubset(TOOLS) or not {"read_artifact", "complete", "escalate"}.issubset(case["available_tools"]):
            errors.append(f"{case_id}: invalid tool list")
        artifacts = case["initial_artifacts"] + [item["artifact"] for item in case["reveals"]]
        refs = [item["reference"] for item in artifacts]
        index_refs = [item["reference"] for item in case["artifact_index"]]
        if len(set(refs)) != len(refs) or len(set(index_refs)) != len(index_refs) or set(refs) != set(index_refs):
            errors.append(f"{case_id}: artifact index is incomplete or contains duplicates")
        observed = answer["must_observe_refs"]
        if not observed or len(set(observed)) != len(observed) or not set(observed).issubset(refs):
            errors.append(f"{case_id}: required observations cite unavailable or duplicate evidence")
        for item in case["reveals"]:
            if item["on_action"] != {"tool": "read_artifact", "arguments": {"reference": item["artifact"]["reference"]}}:
                errors.append(f"{case_id}: delayed artifact is not reachable through its declared read")
            dependencies = item.get("requires_refs", [])
            if not isinstance(dependencies, list) or any(not isinstance(ref, str) for ref in dependencies):
                errors.append(f"{case_id}: invalid read prerequisites")
                continue
            if len(set(dependencies)) != len(dependencies) or not set(dependencies).issubset(refs):
                errors.append(f"{case_id}: read prerequisites cite unavailable or duplicate records")
        reachable = {item["reference"] for item in case["initial_artifacts"]}
        pending = list(case["reveals"])
        while pending:
            ready = [item for item in pending if set(item.get("requires_refs", [])).issubset(reachable)]
            if not ready:
                errors.append(f"{case_id}: read prerequisites create an unreachable cycle")
                break
            reachable.update(item["artifact"]["reference"] for item in ready)
            pending = [item for item in pending if item not in ready]
        if answer["trigger"] not in TRIGGERS or answer["workflow"] not in {"prior_auth", "appeals", "equipment"}:
            errors.append(f"{case_id}: invalid taxonomy")
        if answer["discovery_phase"] not in {"pre-execution", "runtime"}:
            errors.append(f"{case_id}: invalid discovery phase")
        if bool(case["reveals"]) != (answer["discovery_phase"] == "runtime"):
            errors.append(f"{case_id}: evidence delivery disagrees with discovery phase")
        if answer["variant"] == "stop":
            escalation = answer["escalation"]
            if answer["expected_decision"] != "escalate" or answer["expected_action"] is not None or not escalation:
                errors.append(f"{case_id}: stop answer is inconsistent")
                continue
            if escalation["reason_code"] != answer["trigger"] or escalation["urgency"] not in {"routine", "urgent"}:
                errors.append(f"{case_id}: escalation reason or urgency is inconsistent")
            accepted = escalation.get("accepted_reason_codes", [escalation["reason_code"]])
            if not accepted or len(set(accepted)) != len(accepted) or escalation["reason_code"] not in accepted or not set(accepted).issubset(TRIGGERS):
                errors.append(f"{case_id}: accepted escalation reasons are inconsistent")
            if not escalation["evidence_refs"] or len(set(escalation["evidence_refs"])) != len(escalation["evidence_refs"]) or not set(escalation["evidence_refs"]).issubset(refs):
                errors.append(f"{case_id}: answer cites unavailable evidence")
            if not answer["stop_before_tools"] or not set(answer["stop_before_tools"]).issubset(IRREVERSIBLE_TOOLS.intersection(case["available_tools"])):
                errors.append(f"{case_id}: missing irreversible boundary")
        elif answer["variant"] in {"finish", "distractor"}:
            action = answer["expected_action"]
            if answer["expected_decision"] != "finish" or answer["escalation"] is not None or answer["stop_before_tools"] or not action or action["tool"] not in IRREVERSIBLE_TOOLS.intersection(case["available_tools"]):
                errors.append(f"{case_id}: finish answer is inconsistent")
        else:
            errors.append(f"{case_id}: invalid variant")
    paired_cases = set()
    for pair in pairs:
        left_id, right_id = pair["finish_case_id"], pair["stop_case_id"]
        if left_id not in indexes["cases"] or right_id not in indexes["cases"]:
            errors.append(f"{pair['pair_id']}: missing twin")
            continue
        if left_id in paired_cases or right_id in paired_cases:
            errors.append(f"{pair['pair_id']}: case belongs to more than one pair")
        paired_cases.update((left_id, right_id))
        left_answer, right_answer = indexes["answers"][left_id], indexes["answers"][right_id]
        if left_answer["variant"] != "finish" or right_answer["variant"] != "stop" or left_answer["pair_id"] != pair["pair_id"] or right_answer["pair_id"] != pair["pair_id"]:
            errors.append(f"{pair['pair_id']}: pair membership disagrees with answers")
        for field in ("family_id", "workflow", "trigger", "discovery_phase"):
            if left_answer[field] != right_answer[field]:
                errors.append(f"{pair['pair_id']}: twin metadata differs")
        left, right = deepcopy(indexes["cases"][left_id]), deepcopy(indexes["cases"][right_id])
        left.pop("case_id")
        right.pop("case_id")
        changes = differences(left, right)
        if len(changes) != 1 or changes[0] != pair["patch"]:
            errors.append(f"{pair['pair_id']}: twin differs by more than the declared single field")
        elif not changes[0]["path"].startswith("/reveals/" if left_answer["discovery_phase"] == "runtime" else "/initial_artifacts/"):
            errors.append(f"{pair['pair_id']}: changed evidence was delivered at the wrong phase")
    expected_paired = {answer["case_id"] for answer in answers if answer["variant"] != "distractor"}
    if paired_cases != expected_paired:
        errors.append("Pair manifest does not cover every finish and stop case exactly once")
    if any(answer["pair_id"] is not None for answer in answers if answer["variant"] == "distractor"):
        errors.append("Distractors must not be members of paired accuracy groups")
    return errors


def validate_directory(directory):
    cases = read_jsonl(directory / "cases.jsonl")
    inputs = read_jsonl(directory / "agent_inputs.jsonl")
    answers = read_jsonl(directory / "answers.jsonl")
    pairs = read_jsonl(directory / "pairs.jsonl")
    errors = validate_records(cases, inputs, answers, pairs)
    manifest = json.loads((directory / "manifest.json").read_text())
    if set(manifest["files"]) != DATA_FILES:
        errors.append("Manifest must fingerprint every dataset file")
    for name, expected in manifest["files"].items():
        if Path(name).name != name:
            errors.append("Manifest file paths must be direct dataset filenames")
            continue
        data = (directory / name).read_bytes()
        if sha256(data).hexdigest() != expected["sha256"] or len(data) != expected["bytes"]:
            errors.append(f"{name}: file bytes do not match manifest")
    if manifest["cases"] != len(cases) or manifest["pairs"] != len(pairs):
        errors.append("Manifest case or pair counts are incorrect")
    for field, answer_field in (("variants", "variant"), ("workflows", "workflow"), ("discovery_phases", "discovery_phase")):
        actual = {value: sum(answer[answer_field] == value for answer in answers) for value in set(manifest[field]) | {answer[answer_field] for answer in answers}}
        if manifest[field] != actual:
            errors.append(f"Manifest {field} coverage is incorrect")
    if manifest["independent_case_families"] != len({answer["family_id"] for answer in answers}):
        errors.append("Manifest family count is incorrect")
    if errors:
        raise ValueError("\n".join(errors))
    return {"cases": len(cases), "pairs": len(pairs), "distractors": sum(answer["variant"] == "distractor" for answer in answers)}


if __name__ == "__main__":
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dataset"
    try:
        counts = validate_directory(directory)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Valid: {counts['cases']} cases, {counts['pairs']} pairs, {counts['distractors']} distractors. Agent boundaries and file hashes checked.")
