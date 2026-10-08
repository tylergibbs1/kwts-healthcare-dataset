# Astra participant harness

`kwts-codex-subagent-tools-v1` runs the requested `gpt-6-astra` through the user's existing authenticated Codex CLI 0.161.0. It is a separate, unmatched row, not an OpenAI API or native-tool parity comparison. No API key or credential export is required. Auth files are neither inspected nor copied by this runner.

Each proposal starts a fresh ephemeral CLI process, independent of author and reviewer conversations. The host supplies the unchanged legacy task instruction, the current case's agent view, its permitted tool definitions, and the complete delivered episode history. The child returns a structured `calls_json` string containing an array of `{name, arguments}` objects. The host validates that envelope and invokes the byte-identical `legacy_v1_2.Episode`. No answer keys, pair membership, other cases, authoring sources or host state paths enter the child prompt. Pre-run schema validation reads answer keys inside the host; they never enter participant prompts or tool results. Outcome grading loads them after the participant cohort ends.

The eight host proposal batches and twelve executed host actions are enforced. All calls in a proposal receive the same `model_turn`; a read result can support transmission only in a later proposal. Calls execute in proposed order, stopping at an accepted terminal call. Trailing calls are counted as ignored. Missing or malformed proposals, timeouts, process failures, explicit wrong-model metadata, unexpected native-tool items and excess output all fail the episode. The scorer and observation logic are unchanged. Every planned episode remains in the denominator, including unrecorded episodes after interruption.

## Isolation and limits

The child works in a newly created temporary directory containing only the public output schema, under the CLI read-only sandbox. The benchmark case and prior results travel over stdin; full private journals remain in the parent. The runner starts no shell command on the model's behalf. Command construction is visible in `scripts/astra_runner.py`.

The invocation uses `exec --model gpt-6-astra --sandbox read-only --ephemeral --ignore-user-config --ignore-rules --json --skip-git-repo-check --output-schema … -`, approval policy `never`, zero project document bytes, disabled web search, disabled memory generation/injection, and enabled `skip_host_skill_discovery`. It disables shell_tool, unified_exec, multi_agent, multi_agent_v2, hooks, apps, plugins, remote_plugin, code_mode, code_mode_host, enable_mcp_apps, computer_use, browser_use, browser_use_external, in_app_browser, view_image, image_generation, memories, skill_search, skill_mcp_dependency_install, goals, sleep_tool, workspace_dependencies, tool_suggest, request_permissions_tool, unbounded_connection_retries and shell_snapshot. Host skill discovery is an under-development CLI feature; its startup warning is suppressed. The exact pre-turn diagnostic that Code Mode fails closed because its host is disabled is admitted. Other error items fail admission.

These controls are a tool-surface restriction, not an operating-system confidentiality boundary: the read-only sandbox alone would permit file reads if a native read capability were exposed. Unexpected native action items cause failure, but post-response detection cannot undo an unexpected native action. CLI scaffolding and host-managed settings remain outside this harness's complete control. The non-case probe still reports substantial scaffold input tokens even with host skill discovery skipped. No claim is made that its system context matches the native API harness. The child has no resume or cross-case memory operation; provider caching may still occur and returned usage is recorded.

The CLI emits a thread identifier and aggregate turn usage, but the verified interface does not provide an immutable resolved backend model snapshot, API response ID or internal response count. The row is identified by the requested model, not a fabricated verified snapshot. Any explicit contradictory model metadata is rejected. Eight proposals do not establish eight internal backend responses. Input context limits and backend default reasoning controls are not matched or changed. Temperature, reasoning effort and seed are omitted.

There is no supported 2,048-token pre-generation cap in this invocation. The host rejects a proposal when CLI-reported `output_tokens` exceeds 2,048; this is a post-response check and does not bound billed generation, reasoning tokens or hidden backend processing. Usage fields are retained as reported without pretending they equal native API accounting. Each proposal has a 180-second wall-clock timeout; its process group is killed on timeout/interruption. Eight proposals therefore allow up to about 24 minutes of model-call time per episode plus host overhead. There are no harness transport retries; internal CLI/network behavior may still retry.

## Freeze and execution

No challenge case is called before the existing publication/freeze validator succeeds. The freeze must also contain an `astra_interface` object exactly equal to `astra_runner.INTERFACE`, and fingerprint `scripts/astra_runner.py`, `scripts/episode_session.py`, and this document in `public_runner_sha256`. This explicitly acknowledges the unmatched interface before outcomes. The CLI version must match the frozen value.

After publication, the parent can run:

```sh
python3 challenge30/public/scripts/astra_runner.py \
  --dataset "$PWD/challenge30/private/dataset" \
  --freeze "$PWD/challenge30/private/pre-run-freeze.json" \
  --output "$PWD/challenge30/private/runs/astra-cohort-01"
```

The output directory must not exist. The run uses the same seeded case ordering as `run_repeated.case_orders`: five repetitions of thirty cases, serial within this process. The parent must keep total paid process concurrency at two or fewer across providers. Timestamped attempt/result and action journals use exclusive creation, flush and fsync. Completed episodes are never retried. Private CLI stdout/stderr is retained in transport journals for audit; do not publish those logs or private reasoning. Public reporting must use aggregate allowlists and distinguish this harness from native API rows.

`EpisodeSession` is the one-case parent-owned interface for a separately orchestrated participant: `request()` returns only supplied context and tool history; `accept()` consumes a whole proposal batch. It has no network listener and exposes no host file-reading operation. Direct sessions are a low-level interface, not an alternative way to bypass the cohort freeze gate. The supplied CLI runner is the verified participant path.

Validation uses public synthetic transport fixtures unrelated to challenge cases. Tests cover delayed-read observation grouping, terminal truncation, action/response budgets, durable action-boundary ordering, CLI-event admission, transport failure records and rejection of unpublished freezes. Runtime availability is established only by non-case probes; private evidence is retained separately.

Documentation was resolved and queried through Context7 `/openai/codex`, then compared with installed CLI help and feature output: [exec CLI source](https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs), [feature definitions](https://github.com/openai/codex/blob/main/codex-rs/features/src/lib.rs), and [host skill discovery](https://github.com/openai/codex/blob/main/codex-rs/core/src/session/session.rs).

Rejected-envelope accounting preserves explicit parseable irreversible call wrappers as blocked attempt/result events through the shared `record_rejected_proposals` helper. These events carry their actual arguments and proposal group, return `invalid_response_envelope`, and issue no delivery receipt. Non-object or missing arguments inside otherwise valid wrappers reach the unchanged Episode boundary and return its normal validation error. No calls are inferred from prose, reasoning, or incomplete JSON. Usage returned with rejected generations remains in episode totals and private raw journals. Additional regressions cover hostile malformed arguments and rejected output-cap, wrong-model, error and malformed-envelope proposals.
