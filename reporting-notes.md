The reporting helper is a post-run artifact. It is outside the 52-file pre-run commitment and is never passed to the case runner or case agents.

After all four native model runs finish, run this from the release directory:

```sh
python3 scripts/summarize_runs.py --output reports/native-summary.json
```

The output must be a new file outside the native run folders. The helper does not wait for processes. It refuses to write a summary until every protocol model folder has `results.json`, `config.json`, `traces.jsonl`, and `events.jsonl`, with exactly 90 distinct trace records in native case order. An API error or capped episode still occupies its original case and every native denominator.

The helper pins the published pre-run freeze, verifies all 52 committed input hashes, matches native dataset/configuration/model IDs to that commitment, checks action sequences and tool-use proposal turns, compares attempt/result journals to the traces, and checks tool results with the frozen local executor. It requires an exact `aggregate()` regrade match to each native `results.json`. It then checks frozen inputs and native file hashes again before writing.

The JSON has these top-level fields:

| Field | Contents |
| --- | --- |
| `pre_run_publication_commit`, `pre_run_freeze_sha256` | Public commitment identity |
| `integrity` | Frozen-file, model and case counts |
| `models` | One entry for each of the four exact protocol model IDs |
| `scope`, `note_review`, `limitations` | Reporting boundaries and disclosures |

Each model entry contains `native_summary`, `passed_cases`, `subgroups`, `failure_case_ids`, `failures`, `status_counts`, `events`, `model_responses`, `limits_and_errors`, `usage_and_timing`, and `integrity`.

`subgroups` contains trigger, workflow and discovery-phase metrics, plus `lineage.retained15` and `lineage.new75`. Every subgroup includes its case count, passed-case numerator/denominator/rate, and the unchanged native metrics with their own denominators. Failures retain case, family, workflow, trigger, phase, variant, episode status, native score and transmission attempts.

Event counts distinguish read calls, successful reads, transmission attempts, returned receipts, accepted escalations and tool errors. Response counts include returned response stop reasons, `max_tokens` truncations, affected case IDs and ignored calls after a terminal action. Limit/error details retain capped, interrupted and API-error statuses instead of dropping them.

Returned usage totals include only numeric usage returned and recorded by the native harness. Usage from unreturned failed or retried requests is unavailable. Episode-time totals sum known per-episode `elapsed_seconds`; they are neither combined wall-clock duration nor invoice cost. A fully enumerated run whose last episode was interrupted can retain that failure, with unavailable response history or usage disclosed explicitly.

Native `note_quality.status` remains `pending_ai_review`. Root reporting combines external Astra note-review records separately; this helper does not overwrite native scores or attach a note-quality judgment. It accesses public tool-use proposals and response metadata without inspecting other response content blocks.

For a different location, use `--root PATH` and optionally `--runs PATH`. The helper only reads the release's frozen files and the four selected native run folders. It does not read previous pilot, challenge or realism outcomes, call a provider, modify native files, or publish anything.

After all four Astra results/note reviews are finalized, combine them with the verified native summary:

```sh
python3 scripts/build_report.py
```

This creates new `results.json` and `REPORT.md` files and refuses to overwrite existing outputs. Use `--results-output PATH` and `--report-output PATH` to choose other new files. Input options are `--root`, `--runs`, `--summary`, and `--reviews`.

The builder regenerates the native summary in memory and requires equality except for its `created_at` timestamp. It checks each review's exact model identity, `gpt-6-astra` reviewer, `human_review=false`, and receipts for all four native files. Every accepted escalation must have exactly one matching `notes[]` entry with the raw UTF-8 note hash and boolean `overall_pass`. Unsupported review layouts or stale receipts cause refusal.

The separate `ai_note_review` fields recompute written-note passes over all accepted notes, passing stop notes over all 30 required stops, accepted/missing stop-note counts, and full handoff coverage over all 30 stops. Full handoff also requires a timely native stop and all three deterministic handoff checks. These counts must match the independent review summary. Native `native_summary` and `note_quality.status=pending_ai_review` remain unchanged.

The combined JSON retains fingerprints of the native summary, the four review files and the post-run reporting helpers. Strict note failures include supporting case references, whether those references were delivered before the note proposal, and the review's unsupported-claim findings. The Markdown report contains count tables, subgroup comparisons, exact deterministic failure rows, strict note failures, recorded resources and evidence links. Publication identity and the immutable initial dataset-only archive come from `publication-receipt.json`; any later archive containing runs is handled separately.
