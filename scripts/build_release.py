"""Compose the standalone development release using one phase per family."""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from build_dataset import ROOT, agent_view, differences, make_case, write_jsonl
from run_pilot import Episode, argument_error, score_case
from validate_dataset import DATA_FILES, read_jsonl, validate_directory, validate_records

WORKFLOWS = ("prior_auth", "appeals", "equipment")
VARIANTS = ("finish", "stop", "distractor")
FLAT_STRING_FIELDS = ("case_id", "title", "instruction", "as_of", "role")
FLAT_JSON_FIELDS = ("permissions", "available_tools", "artifact_index", "initial_artifacts")
REVIEW_STATUSES = ("author_draft", "ai_reviewed")
HARNESS_SHA256 = "c3a362dd403669d03af22ab9cf15cc4aebc02fcd0ce4a0ebb7b881e88035a288"
CONTRACT_SHA256 = "60917ed0d6024af997c9d4ae7abc405bcccae02565865bc1182b638d3aaebd06"


def load_json(path):
    return json.loads(path.read_text())


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def fingerprint(path):
    data = path.read_bytes()
    return {"sha256": sha256(data).hexdigest(), "bytes": len(data)}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def phase_for_family(contract, family):
    workflow = contract["pre_execution_workflow_for_trigger"][family["trigger"]]
    return "pre-execution" if family["workflow"] == workflow else contract["all_other_family_phases"]


def scalar_values(value):
    if isinstance(value, dict):
        return [item for nested in value.values() for item in scalar_values(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in scalar_values(nested)]
    return [value]


def validate_source(source, contract, protocol, definitions):
    """Refuse incomplete coverage or a delivery plan outside the release contract."""
    errors = []
    if source["version"] != contract["version"] or source["as_of"] != contract["as_of"] or source["as_of"] != protocol["case_clock"]:
        errors.append("Source version or fixed clock disagrees with the authoring contract/protocol")
    if source["review_status"] not in REVIEW_STATUSES:
        errors.append("Unsupported source review status")
    if tuple(contract["variants"]) != VARIANTS:
        errors.append("The release requires finish, stop and distractor variants")
    families = source["families"]
    slots = [(family["workflow"], family["trigger"]) for family in families]
    expected_slots = {(workflow, trigger) for workflow in WORKFLOWS for trigger in contract["triggers"]}
    duplicates = [slot for slot, count in Counter(slots).items() if count != 1]
    if duplicates:
        errors.append(f"Duplicate workflow/trigger coverage slot: {duplicates}")
    if set(slots) != expected_slots or len(families) != contract["total_families"]:
        errors.append("Missing or unexpected workflow/trigger coverage slots")
    if len({family["family_id"] for family in families}) != len(families):
        errors.append("Family IDs must be unique")
    tools = {item["name"]: item for item in definitions}
    actions = []
    for family in families:
        label = family["family_id"]
        if (family["workflow"], family["trigger"]) not in expected_slots:
            continue
        phase = phase_for_family(contract, family)
        if family.get("phase") != phase:
            errors.append(f"{label}: family phase disagrees with the authoring contract")
        if "read_dependencies" in family:
            errors.append(f"{label}: read dependencies are prohibited")
        refs = [item["reference"] for item in family["artifacts"]]
        distractor_ref = family["distractor"]["reference"]
        all_refs = refs + [distractor_ref]
        if len(set(all_refs)) != len(all_refs):
            errors.append(f"{label}: artifact/distractor references must be unique")
        delayed = family.get("delayed_refs", [])
        if len(set(delayed)) != len(delayed) or not set(delayed).issubset(all_refs):
            errors.append(f"{label}: delayed_refs cites unavailable or duplicate records")
        initial = [item for item in family["artifacts"] if item["reference"] not in delayed]
        if phase == "runtime":
            if len(initial) != 1 or not initial[0]["reference"].startswith("desk://") or "message" not in initial[0]["data"]:
                errors.append(f"{label}: runtime initially exposes only the ordinary staff request")
            if distractor_ref not in delayed:
                errors.append(f"{label}: runtime distractor must be delayed")
        else:
            initial = family["artifacts"]
        observed = family["must_observe_refs"]
        evidence = family["evidence_refs"]
        if not observed or len(set(observed)) != len(observed) or not set(observed).issubset(refs):
            errors.append(f"{label}: must_observe_refs needs unique available records")
        if not evidence or len(set(evidence)) != len(evidence) or not set(evidence).issubset(refs):
            errors.append(f"{label}: evidence_refs needs unique available records")
        available_support = set(observed) | {item["reference"] for item in initial}
        if family["stop_patch"]["reference"] not in observed:
            errors.append(f"{label}: the decisive patched record must be observed")
        expected_urgency = "urgent" if family["trigger"] in {"emergent_risk", "deadline_risk"} else "routine"
        if family["urgency"] != expected_urgency:
            errors.append(f"{label}: urgency disagrees with the authoring contract")
        tool = tools.get(family["action"])
        if not tool or not tool["irreversible"]:
            errors.append(f"{label}: unsupported transmission action")
            continue
        error = argument_error(family["action_arguments"], tool["arguments_schema"])
        if error:
            errors.append(f"{label}: action arguments violate the copied tool contract: {error}")
        supported_values = scalar_values([item["data"] for item in family["artifacts"] if item["reference"] in available_support])
        if any(value not in supported_values for value in scalar_values(family["action_arguments"])):
            errors.append(f"{label}: exact transmission argument value lacks observed record support")
        actions.append(json_bytes({"tool": family["action"], "arguments": family["action_arguments"]}))
    if len(set(actions)) != len(actions):
        errors.append("Family transmission arguments must be distinct")
    family_map = {family["family_id"]: family for family in families}
    for workflow, retained in contract["retained_families"].items():
        for trigger, family_id in retained.items():
            family = family_map.get(family_id)
            if not family or (family["workflow"], family["trigger"], family["phase"]) != (workflow, trigger, "runtime"):
                errors.append(f"{family_id}: retained family identity or runtime phase changed")
    if errors:
        raise ValueError("\n".join(errors))


def compose_source(root=ROOT, review_status=None):
    contract = load_json(root / "authoring-contract.json")
    existing = root / "source/templates.json"
    status = review_status or (load_json(existing).get("review_status", "author_draft") if existing.exists() else "author_draft")
    if status not in REVIEW_STATUSES:
        raise ValueError("Unsupported source review status")
    review = "Required AI review is pending." if status == "author_draft" else "Reviewed by AI adjudicators; see the release review records."
    return {
        "name": "Knows When to Stop — 90-case synthetic healthcare administrative development dataset",
        "version": contract["version"], "as_of": contract["as_of"], "review_status": status,
        "notice": f"Ninety synthetic healthcare administrative cases in thirty families. {review} No patient data or live systems; no human healthcare validation. This is a public development dataset, with no private test set or production healthcare validity claim. CMS fact context and synthetic local procedures are explicitly distinct. Fifteen case bodies in five retained families were used in previous public development experiments; seventy-five cases in twenty-five families are new. No HealthAdminBench data, case content, or model outcomes were imported.",
        "build_plan": {"composer": "scripts/build_release.py", "phase_policy": "One phase per family from authoring-contract.json; all three variants use that phase.", "include_distractors": True, "variants": list(VARIANTS)},
        "families": [family for workflow in WORKFLOWS for family in load_json(root / "source/workflows" / f"{workflow}.json")],
    }


def compose_records(source, contract, protocol, definitions):
    validate_source(source, contract, protocol, definitions)
    cases, answers, pairs = [], [], []
    for family in source["families"]:
        phase = phase_for_family(contract, family)
        triplet = {variant: make_case(source, family, phase, variant) for variant in VARIANTS}
        finish, finish_answer = triplet["finish"]
        stop, _ = triplet["stop"]
        left, right = deepcopy(finish), deepcopy(stop)
        left.pop("case_id")
        right.pop("case_id")
        patch = differences(left, right)
        if len(patch) != 1:
            raise ValueError(f"{family['family_id']}: twin must change exactly one field")
        distractor, _ = triplet["distractor"]
        benign = deepcopy(distractor)
        benign["case_id"] = finish["case_id"]
        extra_index = benign["artifact_index"].pop()
        extra_record = benign["reveals"].pop()["artifact"] if phase == "runtime" else benign["initial_artifacts"].pop()
        if benign != finish or extra_record != family["distractor"] or extra_index != {key: extra_record[key] for key in ("reference", "label")}:
            raise ValueError(f"{family['family_id']}: distractor must only add the benign authored record")
        cases.extend(triplet[variant][0] for variant in VARIANTS)
        answers.extend(triplet[variant][1] for variant in VARIANTS)
        pairs.append({"pair_id": finish_answer["pair_id"], "finish_case_id": finish["case_id"], "stop_case_id": stop["case_id"], "patch": patch[0]})
    cases.sort(key=lambda row: row["case_id"])
    answers.sort(key=lambda row: row["case_id"])
    pairs.sort(key=lambda row: row["pair_id"])
    inputs = [agent_view(case) for case in cases]
    errors = validate_records(cases, inputs, answers, pairs)
    if errors:
        raise ValueError("\n".join(errors))
    if len(cases) != contract["total_cases"] or len(pairs) != contract["total_families"]:
        raise ValueError("Export counts disagree with the authoring contract")
    return cases, inputs, answers, pairs


def flatten_agent_input(row):
    return {**{key: row[key] for key in FLAT_STRING_FIELDS},
            **{key + "_json": json.dumps(row[key], sort_keys=True, ensure_ascii=False) for key in FLAT_JSON_FIELDS}}


def retained_preservation(root, source, cases, contract):
    """Use packaged snapshots; also compare original line bytes when available."""
    retained_ids = {family_id for families in contract["retained_families"].values() for family_id in families.values()}
    snapshots = [family for workflow in WORKFLOWS for family in load_json(root / "source/workflows" / f"{workflow}-retained.json")]
    if {family["family_id"] for family in snapshots} != retained_ids or len(snapshots) != len(retained_ids):
        raise ValueError("Retained snapshots do not contain exactly the five contracted families")
    families = {family["family_id"]: family for family in source["families"]}
    bodies = {case["case_id"]: json_bytes(case) for case in cases}
    old_path = root.parent / "realism/dataset/cases.jsonl"
    old_lines = {json.loads(line)["case_id"]: line for line in old_path.read_bytes().splitlines(keepends=True)} if old_path.exists() else None
    original_source = root.parent / "realism/templates.json"
    original_families = {family["family_id"]: family for family in load_json(original_source)["families"]} if original_source.exists() else None
    hashes = {}
    for snapshot in snapshots:
        family_id = snapshot["family_id"]
        family = deepcopy(families[family_id])
        family.pop("phase")
        if family != snapshot or (original_families is not None and snapshot != original_families[family_id]):
            raise ValueError(f"{family_id}: retained family body changed beyond phase metadata")
        for variant in VARIANTS:
            expected, _ = make_case(source, snapshot, "runtime", variant)
            expected_bytes = json_bytes(expected)
            if bodies.get(expected["case_id"]) != expected_bytes:
                raise ValueError(f"{family_id}/{variant}: retained case body differs from its snapshot")
            if old_lines is not None and old_lines.get(expected["case_id"]) != expected_bytes:
                raise ValueError(f"{family_id}/{variant}: retained case line bytes changed from realism dataset")
            hashes[expected["case_id"]] = sha256(expected_bytes).hexdigest()
    return {"families": len(retained_ids), "cases": len(hashes), "retained_family_ids": sorted(retained_ids),
            "family_bodies_unchanged_except_phase": True, "case_line_sha256": hashes,
            "snapshot_verification": "passed", "original_realism_case_bytes_verification": "passed" if old_lines is not None else "not_available_in_standalone_package",
            "original_realism_family_verification": "passed" if original_families is not None else "not_available_in_standalone_package"}


def compose_provenance(root, source):
    registries = {workflow: load_json(root / "source/workflows" / f"{workflow}-sources.json") for workflow in WORKFLOWS}
    sources, mappings = [], []
    families = {family["family_id"]: family for family in source["families"]}
    for workflow, registry in registries.items():
        source_key = {"prior_auth": "cms_sources", "appeals": "sources", "equipment": "verified_sources"}[workflow]
        definitions = registry[source_key]
        source_ids = {item["source_id"] for item in definitions}
        if len(source_ids) != len(definitions):
            raise ValueError(f"{workflow}: registry source IDs must be unique")
        primary_ids = {item["source_id"] for item in definitions if item.get("url", "") and item["url"].startswith("https://www.cms.gov/")}
        for item in definitions:
            sources.append({"qualified_source_id": f"{workflow}:{item['source_id']}", "workflow": workflow,
                            "source_kind": "verified_cms_fact_context" if item["source_id"] in primary_ids else "synthetic_local_policy_or_record", "source": deepcopy(item)})
        if workflow == "prior_auth":
            native_maps = [{"family_id": family_id, "source_ids": ids} for family_id, ids in registry["family_source_map"].items()]
            rules = {rule["family_id"]: rule for rule in registry["synthetic_rules"]}
        else:
            native_maps = registry["family_source_mappings" if workflow == "appeals" else "family_mappings"]
            rules = {}
        if len(native_maps) != 10 or {item["family_id"] for item in native_maps} != {family_id for family_id, family in families.items() if family["workflow"] == workflow}:
            raise ValueError(f"{workflow}: source mapping must cover its ten families exactly once")
        for native in native_maps:
            family = families[native["family_id"]]
            for field in ("trigger", "phase"):
                if field in native and native[field] != family[field]:
                    raise ValueError(f"{family['family_id']}: registry {field} disagrees with the authored family")
            if "decisive_patch" in native and native["decisive_patch"] != family["stop_patch"]:
                raise ValueError(f"{family['family_id']}: registry decisive patch disagrees with the authored family")
            ids = native.get("source_ids", native.get("verified_source_ids", []))
            if len(set(ids)) != len(ids) or not set(ids).issubset(source_ids):
                raise ValueError(f"{family['family_id']}: source mapping cites unavailable or duplicate sources")
            rule = rules.get(family["family_id"])
            local_refs = [rule["record"]] if rule else native.get("synthetic_rule_record_refs", [item["reference"] for item in family["artifacts"] if item["reference"].startswith("policy://")])
            refs = {item["reference"] for item in family["artifacts"]}
            if not local_refs or not set(local_refs).issubset(refs):
                raise ValueError(f"{family['family_id']}: synthetic rule mapping lacks case record support")
            mappings.append({"family_id": family["family_id"], "workflow": workflow, "trigger": family["trigger"], "phase": family["phase"],
                             "verified_cms_source_ids": [f"{workflow}:{item}" for item in ids if item in primary_ids],
                             "synthetic_source_ids": [f"{workflow}:{item}" for item in ids if item not in primary_ids],
                             "synthetic_rule_record_refs": local_refs,
                             "synthetic_rule": deepcopy(rule) if rule else native.get("local_rule", native.get("synthetic_rule_summary")),
                             "registry_mapping": deepcopy(native)})
    return {"version": source["version"], "as_of": source["as_of"], "review_status": source["review_status"],
            "notice": "Author-supplied primary CMS fact context is preserved separately from synthetic local administrative policies, fictional payer terms and signed synthetic clinician facts. Integration does not itself verify medical or legal validity. No patient data, human healthcare validation or HealthAdminBench case content is included.",
            "workflow_registries": registries, "sources": sources, "family_source_mappings": mappings}


def prove_execution_budget(cases, answers, definitions, protocol):
    """Check local executor feasibility; no provider calls or model traces."""
    keys = {row["case_id"]: row for row in answers}
    maxima = {mode: {"max_model_responses": 0, "max_executed_actions": 0} for mode in ("all_delayed_records_batched", "relevant_records_sequential")}
    for case in cases:
        answer = keys[case["case_id"]]
        initial = {item["reference"] for item in case["initial_artifacts"]}
        for mode in maxima:
            refs = [item["artifact"]["reference"] for item in case["reveals"]]
            if mode == "relevant_records_sequential":
                wanted = set(answer["must_observe_refs"])
                if answer["escalation"]:
                    wanted.update(answer["escalation"]["evidence_refs"])
                refs = [ref for ref in refs if ref in wanted - initial]
            episode = Episode(case, definitions)
            turn = 0
            for ref in refs:
                turn = turn + 1 if mode == "relevant_records_sequential" else 1
                result = episode.invoke("read_artifact", {"reference": ref}, model_turn=turn)
                if "error" in result:
                    raise ValueError(f"{case['case_id']}: relevant read is not executable")
            turn += 1
            if answer["expected_decision"] == "finish":
                action = answer["expected_action"]
                receipt = episode.invoke(action["tool"], action["arguments"], model_turn=turn)
                turn += 1
                result = episode.invoke("complete", {"receipt_id": receipt["receipt_id"]}, model_turn=turn)
            else:
                handoff = answer["escalation"]
                arguments = {key: deepcopy(handoff[key]) for key in ("reason_code", "urgency", "evidence_refs")}
                arguments["note"] = handoff["note_requirement"]
                result = episode.invoke("escalate", arguments, model_turn=turn)
            score = score_case(case, answer, {"status": result.get("status"), "events": episode.events})
            if not score["passed"] or (answer["escalation"] and not all(score["handoff"].values())):
                raise ValueError(f"{case['case_id']}: local executor path cannot satisfy its key")
            actions = len(episode.events)
            if turn > protocol["max_model_responses_per_case"] or actions > protocol["max_executed_tool_actions_per_case"]:
                raise ValueError(f"{case['case_id']}: {mode} exceeds the fixed response/action cap")
            maxima[mode]["max_model_responses"] = max(maxima[mode]["max_model_responses"], turn)
            maxima[mode]["max_executed_actions"] = max(maxima[mode]["max_executed_actions"], actions)
    return {"basis": "Deterministic local executor feasibility with earlier-turn record delivery; these are key-guided paths, not model evaluations. No provider request or row traces are saved.",
            "cases_checked_per_path": len(cases), "limits": {"model_responses": protocol["max_model_responses_per_case"], "executed_actions": protocol["max_executed_tool_actions_per_case"]}, **maxima}


def coverage(source, answers, contract):
    retained = {family_id for mapping in contract["retained_families"].values() for family_id in mapping.values()}
    return {
        "variants": {value: sum(row["variant"] == value for row in answers) for value in VARIANTS},
        "workflows": {value: sum(row["workflow"] == value for row in answers) for value in WORKFLOWS},
        "triggers": {value: sum(row["trigger"] == value for row in answers) for value in contract["triggers"]},
        "discovery_phases": {value: sum(row["discovery_phase"] == value for row in answers) for value in ("pre-execution", "runtime")},
        "decisions": {value: sum(row["expected_decision"] == value for row in answers) for value in ("finish", "escalate")},
        "stop_urgencies": {value: sum(bool(row["escalation"]) and row["escalation"]["urgency"] == value for row in answers) for value in ("routine", "urgent")},
        "families_by_workflow_and_trigger": {workflow: {trigger: sum(family["workflow"] == workflow and family["trigger"] == trigger for family in source["families"]) for trigger in contract["triggers"]} for workflow in WORKFLOWS},
        "phase_by_family": {family["family_id"]: phase_for_family(contract, family) for family in source["families"]},
        "retained_families": len(retained), "new_families": len(source["families"]) - len(retained),
        "retained_cases": sum(row["family_id"] in retained for row in answers), "new_cases": sum(row["family_id"] not in retained for row in answers),
    }


def load_plan(root):
    contract = load_json(root / "authoring-contract.json")
    protocol = load_json(root / "evaluation-protocol.json")
    if fingerprint(root / "scripts/run_pilot.py")["sha256"] != HARNESS_SHA256 or fingerprint(root / "dataset/tool_contract.json")["sha256"] != CONTRACT_SHA256:
        raise ValueError("The reviewed v1.2 harness or fixed tool contract was modified")
    return contract, protocol, load_json(root / "dataset/tool_contract.json")["tools"]


def build(root=ROOT, review_status=None):
    root = Path(root)
    contract, protocol, definitions = load_plan(root)
    source = compose_source(root, review_status)
    cases, inputs, answers, pairs = compose_records(source, contract, protocol, definitions)
    retained = retained_preservation(root, source, cases, contract)
    provenance = compose_provenance(root, source)
    budgets = prove_execution_budget(cases, answers, definitions, protocol)
    counts = coverage(source, answers, contract)
    retained_manifest = {key: value for key, value in retained.items() if not key.startswith("original_realism_")}
    retained_manifest["original_byte_verification_record"] = "reviews/integration-check.json"
    write_json(root / "source/templates.json", source)
    write_json(root / "source/provenance.json", provenance)
    dataset = root / "dataset"
    for name, rows in (("cases", cases), ("agent_inputs", inputs), ("answers", answers), ("pairs", pairs)):
        write_jsonl(dataset / f"{name}.jsonl", rows)
    (root / "data").mkdir(parents=True, exist_ok=True)
    write_jsonl(root / "data/benchmark.jsonl", [flatten_agent_input(row) for row in inputs])
    sample_family = source["families"][0]
    sample, sample_answer = make_case(source, sample_family, phase_for_family(contract, sample_family), "stop")
    for name, value in (("case", sample), ("agent-input", agent_view(sample)), ("answer-key", sample_answer)):
        write_json(root / "examples" / f"{name}.json", value)
    authored_paths = [f"source/workflows/{workflow}{suffix}.json" for workflow in WORKFLOWS for suffix in ("", "-retained", "-sources")]
    manifest = {
        "name": source["name"], "version": source["version"], "owner": "Grayhaven Industries", "prepared": "2026-10-08", "license": "Apache-2.0",
        "notice": source["notice"], "split": "public_development", "review_status": source["review_status"], "as_of": source["as_of"],
        "cases": len(cases), "pairs": len(pairs), "independent_case_families": len(source["families"]),
        **counts, "source_sha256": fingerprint(root / "source/templates.json")["sha256"], "provenance_sha256": fingerprint(root / "source/provenance.json")["sha256"],
        "files": {name: fingerprint(dataset / name) for name in sorted(DATA_FILES)},
        "authored_inputs": {name: fingerprint(root / name) for name in authored_paths + ["authoring-contract.json", "evaluation-protocol.json"]},
        "viewer_export": {"path": "data/benchmark.jsonl", **fingerprint(root / "data/benchmark.jsonl"), "columns": list(FLAT_STRING_FIELDS) + [key + "_json" for key in FLAT_JSON_FIELDS], "all_columns_are_strings": True, "agent_input_only": True},
        "retained_preservation": retained_manifest, "execution_budget": budgets,
        "harness": {"name": protocol["harness"], "path": "scripts/run_pilot.py", "sha256": HARNESS_SHA256, "unmodified": True},
        "commitment_scope": "Exact public development file bytes. All ninety cases are exposed. Fifteen retained cases were used in previous public experiments. No private or human-validated healthcare test set exists.",
    }
    write_json(dataset / "manifest.json", manifest)
    validate_directory(dataset)
    return manifest


def validate_release(root=ROOT):
    """Verify authored input, exported records, viewer rows and the release manifest."""
    root = Path(root)
    contract, protocol, definitions = load_plan(root)
    source = load_json(root / "source/templates.json")
    if source != compose_source(root, source["review_status"]):
        raise ValueError("Exported source differs from authored workflow inputs")
    expected = compose_records(source, contract, protocol, definitions)
    for name, rows in zip(("cases", "agent_inputs", "answers", "pairs"), expected):
        if read_jsonl(root / "dataset" / f"{name}.jsonl") != rows:
            raise ValueError(f"{name}: export differs from the release composer")
    cases, inputs, answers, _ = expected
    manifest = load_json(root / "dataset/manifest.json")
    validate_directory(root / "dataset")
    for field in ("name", "version", "notice", "review_status", "as_of"):
        if manifest.get(field) != source[field]:
            raise ValueError(f"Manifest {field} disagrees with the source")
    for key, value in coverage(source, answers, contract).items():
        if manifest.get(key) != value:
            raise ValueError(f"Manifest {key} release coverage is incorrect")
    if manifest["source_sha256"] != fingerprint(root / "source/templates.json")["sha256"] or manifest["provenance_sha256"] != fingerprint(root / "source/provenance.json")["sha256"]:
        raise ValueError("Source/template or provenance hash is stale")
    if load_json(root / "source/provenance.json") != compose_provenance(root, source):
        raise ValueError("Aggregated provenance differs from the authored source registries")
    for path, expected_hash in manifest["authored_inputs"].items():
        if fingerprint(root / path) != expected_hash:
            raise ValueError(f"{path}: authored input hash is stale")
    flat = read_jsonl(root / "data/benchmark.jsonl")
    if flat != [flatten_agent_input(row) for row in inputs] or any(not all(isinstance(value, str) for value in row.values()) for row in flat):
        raise ValueError("Viewer export must have only flat string columns encoding exact agent inputs")
    if {key: manifest["viewer_export"][key] for key in ("sha256", "bytes")} != fingerprint(root / "data/benchmark.jsonl"):
        raise ValueError("Viewer export hash is stale")
    sample_family = source["families"][0]
    sample, answer = make_case(source, sample_family, phase_for_family(contract, sample_family), "stop")
    for name, value in (("case", sample), ("agent-input", agent_view(sample)), ("answer-key", answer)):
        if load_json(root / "examples" / f"{name}.json") != value:
            raise ValueError(f"{name}: example has an incorrect family phase or export")
    retained = retained_preservation(root, source, cases, contract)
    if retained["case_line_sha256"] != manifest["retained_preservation"]["case_line_sha256"]:
        raise ValueError("Retained case body hashes differ from the manifest")
    budgets = prove_execution_budget(cases, answers, definitions, protocol)
    if manifest["execution_budget"] != budgets:
        raise ValueError("Execution budget proof differs from the manifest")
    return {"cases": len(cases), "pairs": len(expected[3]), "families": len(source["families"]), "review_status": source["review_status"],
            "coverage": coverage(source, answers, contract), "retained_preservation": retained, "execution_budget": budgets,
            "viewer_columns": list(flat[0]), "viewer_export_agent_input_only": True, "family_sources_mapped": len(source["families"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--review-status", choices=REVIEW_STATUSES, help="Override the existing source status; ai_reviewed is for the completed required AI reviews only.")
    args = parser.parse_args()
    manifest = build(args.root, args.review_status)
    print(f"Exported {manifest['cases']} cases, {manifest['pairs']} pairs, {manifest['independent_case_families']} families; review_status={manifest['review_status']}.")


if __name__ == "__main__":
    main()
