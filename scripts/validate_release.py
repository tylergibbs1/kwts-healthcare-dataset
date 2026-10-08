"""Validate the release and optionally record its local integration checks."""

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from build_dataset import ROOT, build as build_with_global_phases
from build_release import HARNESS_SHA256, build, fingerprint, load_json, validate_release, write_json
from validate_dataset import read_jsonl


def copied_files_check(root):
    protected = load_json(root / "baseline-protection.json")["files"]
    result = {}
    for relative in ("scripts/build_dataset.py", "scripts/run_pilot.py", "scripts/validate_dataset.py", "scripts/agent_view.py", "tests/test_dataset_integrity.py"):
        actual = fingerprint(root / relative)["sha256"]
        if actual != protected[relative]:
            raise ValueError(f"{relative}: reviewed copied file bytes changed")
        result[relative] = {"sha256": actual, "byte_identical_to_original": True}
    relative = "tests/test_pilot_execution.py"
    copied = (root / relative).read_bytes()
    old = b'ROOT / "pilot/dataset/tool_contract.json"'
    new = b'ROOT / "dataset/tool_contract.json"'
    if copied.count(new) != 1 or sha256(copied.replace(new, old, 1)).hexdigest() != protected[relative]:
        raise ValueError("Copied evaluator test differs beyond the single authorized fixture-path portability edit")
    result[relative] = {"sha256": sha256(copied).hexdigest(), "original_sha256": protected[relative],
                        "single_portability_edit_verified": True, "change": 'ROOT / "pilot/dataset/tool_contract.json" -> ROOT / "dataset/tool_contract.json"',
                        "assertions_and_test_behavior_preserved": True}
    return result


def original_baseline_check(root):
    expected = load_json(root / "baseline-protection.json")["files"]
    checked, missing, changed = 0, [], []
    for relative, digest in expected.items():
        path = root.parent / relative
        if not path.exists():
            missing.append(relative)
        else:
            checked += 1
            if fingerprint(path)["sha256"] != digest:
                changed.append(relative)
    if changed:
        raise ValueError(f"Fingerprint-protected original files changed: {changed}")
    if checked and missing:
        raise ValueError(f"Original baseline is only partially present: {missing}")
    return {"protected_files": len(expected), "checked_files": checked, "changed_files": changed,
            "status": "passed" if checked == len(expected) else "originals_not_available_in_standalone_package"}


def standalone_rebuild_check(root):
    paths = ["authoring-contract.json", "evaluation-protocol.json", "baseline-protection.json", "dataset/tool_contract.json", "source/templates.json"]
    paths.extend(str(path.relative_to(root)) for path in (root / "source/workflows").glob("*.json"))
    paths.extend(str(path.relative_to(root)) for path in (root / "scripts").glob("*.py"))
    compare = ["source/templates.json", "source/provenance.json", "dataset/manifest.json", "data/benchmark.jsonl"]
    compare.extend(f"dataset/{name}" for name in ("cases.jsonl", "agent_inputs.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json"))
    compare.extend(f"examples/{name}.json" for name in ("case", "agent-input", "answer-key"))
    with tempfile.TemporaryDirectory() as temporary:
        standalone = Path(temporary) / "release90"
        for relative in paths:
            destination = standalone / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        build(standalone)
        changed = [relative for relative in compare if (standalone / relative).read_bytes() != (root / relative).read_bytes()]
        if changed:
            raise ValueError(f"Standalone rebuild changes exported bytes: {changed}")
        validate_release(standalone)
    return {"status": "passed", "original_workspace_files_required": False, "files_compared": compare,
            "export_bytes_reproduced": True, "existing_review_status_preserved": True}


def regression_proof(root):
    """Show that the previous global-phase composer violates this release's contract."""
    source = deepcopy(load_json(root / "source/templates.json"))
    source["build_plan"] = {"phases": ["runtime"], "include_distractors": True}
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        path = directory / "templates.json"
        write_json(path, source)
        dataset = directory / "dataset"
        dataset.mkdir()
        shutil.copyfile(root / "dataset/tool_contract.json", dataset / "tool_contract.json")
        with redirect_stdout(io.StringIO()):
            build_with_global_phases(path, dataset, directory / "examples")
        answers = read_jsonl(dataset / "answers.jsonl")
    actual = {phase: sum(row["discovery_phase"] == phase for row in answers) for phase in ("pre-execution", "runtime")}
    required = {"pre-execution": 30, "runtime": 60}
    if actual == required:
        raise ValueError("The global-phase regression probe no longer reproduces the intended failure")
    return {"prior_global_phase_builder": "Copied, unchanged scripts/build_dataset.py with runtime as its single global phase",
            "prior_export_phase_counts": actual, "release_required_phase_counts": required,
            "new_phase_regression_test_rejects_prior_behavior": True}


def run_tests(root):
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    output = completed.stdout + completed.stderr
    if completed.returncode:
        raise ValueError(f"Release regression tests failed:\n{output}")
    match = re.search(r"Ran (\d+) tests", output)
    return {"command": "python3 -m unittest discover -s tests -v", "executed_argv": command, "exit_code": completed.returncode,
            "tests_run": int(match.group(1)) if match else None, "output": output, "paid_provider_calls": 0}


def record_integration(root):
    facts = validate_release(root)
    copied = copied_files_check(root)
    baseline = original_baseline_check(root)
    standalone = standalone_rebuild_check(root)
    regression = regression_proof(root)
    tests = run_tests(root)
    paths = ["source/templates.json", "source/provenance.json", "data/benchmark.jsonl", "dataset/manifest.json", "authoring-contract.json", "evaluation-protocol.json"]
    paths.extend(str(path.relative_to(root)) for path in (root / "scripts").glob("*.py"))
    paths.extend(str(path.relative_to(root)) for path in (root / "tests").glob("*.py"))
    paths.extend(f"dataset/{name}" for name in ("cases.jsonl", "agent_inputs.jsonl", "answers.jsonl", "pairs.jsonl", "tool_contract.json"))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "passed", "review_status": facts["review_status"],
        "scope": "Mechanical export, standalone reproduction, fixed v1.2 evaluator regression, controlled triplets and local executor feasibility. This record does not substitute for blind Astra or source/answer-key review.",
        "harness": "kwts-json-tools-v1.2", "harness_sha256": HARNESS_SHA256, "copied_reviewed_files": copied,
        "original_baseline_fingerprints": baseline, "release_validation": facts, "standalone_rebuild": standalone,
        "phase_regression_proof": regression, "tests": tests,
        "new_test_authoring_gate": {
            "skill": "test-audit/SKILL.md; authoring skill, outside this release",
            "observable_contract": "All three variants share their family's phase and the export has one family in every required workflow/trigger slot.",
            "credible_regression": "Reusing the old global phase/distractor behavior or silently exporting a duplicated/incomplete slot inventory.",
            "existing_coverage_gap": "Copied tests validate pair mechanics and evaluator behavior, but permit any coverage distribution and do not exercise the release-specific composer.",
            "production_seam": "None added only for tests; compose_records is used by both the build and validation entry points.",
            "tests_added": ["test_all_three_variants_use_the_family_phase", "test_duplicate_or_missing_coverage_slot_is_refused"],
        },
        "checked_file_fingerprints": {relative: fingerprint(root / relative) for relative in sorted(set(paths))},
        "paid_provider_calls": 0, "publication_performed": False, "prior_model_outcomes_or_traces_imported": False,
        "limitations": ["Synthetic public development dataset; no patient data or human healthcare validation.",
                        "Local key-guided executor proofs establish cap feasibility, not model task performance or clinical validity.",
                        "Astra review completion is tracked in separate release review records; this mechanical check preserves the source's current review status."],
    }
    write_json(root / "reviews/integration-check.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--record", action="store_true", help="Also run the copied/new tests and record standalone integration checks.")
    args = parser.parse_args()
    if args.record:
        result = record_integration(args.root)
        print(f"Integration checks passed; {result['tests']['tests_run']} tests. Recorded reviews/integration-check.json.")
    else:
        result = validate_release(args.root)
        copied_files_check(args.root)
        original_baseline_check(args.root)
        print(f"Valid release: {result['cases']} cases, {result['pairs']} pairs, {result['families']} families; review_status={result['review_status']}.")


if __name__ == "__main__":
    main()
