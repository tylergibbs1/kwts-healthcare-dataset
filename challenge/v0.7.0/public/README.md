# KWTS protected challenge pilot

This candidate adds a protected synthetic challenge set to the unchanged [90-case public development release](https://github.com/tylergibbs1/kwts-healthcare-dataset/releases/tag/v0.6.0-development). It contains 30 cases in ten newly authored families. Each family has an ordinary finish case, a one-field stop twin and a finishable benign-warning variant. All members of every family remain protected together.

Public material consists of the evaluator, protocol, commitment records and aggregate evidence. Active challenge cases, answer keys, authoring sources, transcripts, notes and detailed review records remain private. Exact bytes and private random salts can be revealed when this test set is retired. Publishing a commitment does not independently prove healthcare validity or correct authoring.

The challenge design draws on lessons from public development failures. It has not been tuned against challenge model outcomes. Five trials per model are planned, with fresh case contexts and a predeclared order. Exact model and harness configurations must be frozen before their first challenge request. No scenario, label, prompt or budget corrections are made after results become visible; defects are disclosed for a later version.

Task metrics separately measure timely stops, correct autonomous completion, paired and triplet accuracy, transmission attempts on required stops, false alarms and urgency. Written-handoff review is separate from native task scoring. Success on all five trials is reported as `pass^5`; it is never the best of five attempts.

Uncertainty estimates resample entire workflow families, retaining all their variants and repetitions. Thirty variants and five repetitions still represent ten authored workflow families. The 95% cluster-bootstrap intervals describe sensitivity to this small, deliberately selected family set. They are not estimates of hospital population risk. An all-pass or all-fail sample can produce a zero-width bootstrap interval without establishing certainty.

Claude native-tool runs and Astra Codex child-agent runs have distinct harness labels. They share host action semantics and scoring where supported, while differences in prompts, tool interfaces, available metadata and inference budgets are disclosed. A difference between those rows cannot be attributed solely to the model provider.

The new challenge families use explicitly fictional administrative controls and supplied clinician routing directions. They test reconciliation and authority decisions in their supplied environments; they do not establish the prevalence of those conditions in hospitals or validate Medicare rule application.

Review is performed by fresh Astra contexts. Healthcare expert sign-off and validation against real administrative workflows remain pending. The private healthcare review packet includes the case states, keys, source boundaries and a sign-off template; preparing that packet does not constitute independent validation. There is no patient data, clinical diagnosis test, live portal connection or production safety claim.

Owner: Grayhaven Industries. Candidate version: `0.7.0-challenge-candidate`. See `evaluation-protocol.json` for the run plan and `commitment.json` for pre-run commitment status once published.
