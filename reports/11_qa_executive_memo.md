# Quality Review Memo

**To:** Hypothetical Content Safety QA Lead  
**From:** Independent portfolio analyst  
**Evidence snapshot:** 28 October 2026 — Google DICES-350 research annotations

## Decision Needed

Approve a proposed blinded-review validation pilot. Retain uniform random selection as the audit benchmark for the studied FN-capture objective; defer rollout of the severity-first proxy. Review budget and decision guardrails require agreement before collection. No pilot has been performed.

## Three Evidence-backed Findings

1. **Reference alignment requires investigation.** Across 350 conversations and 123 raters, exact agreement is 24,506/43,050 judgments (56.92%; conditional 95% CI 54.98–58.88%). Unsafe-reference judgments include 10,910/21,525 explicit No responses (50.69%); safe-reference judgments include 4,940/21,525 explicit Yes responses (22.95%). Unsure remains separate: 2,694/43,050 (6.26%). These are reference-relative differences, not confirmed worker errors. [E01–E06](11_evidence_ledger.json)

2. **Disagreement supports review, without establishing its cause.** Exploratory miss, opposite-judgment, split and uncertainty screens flag 95, 10, 108 and 40 conversations; their overlapping union is 186. Nominal alpha is 0.161 (conditional 95% CI 0.139–0.182), a three-response consistency coefficient. Establish whether rubric interpretation or the original reference needs reconsideration before prescribing training. [E07–E09](11_evidence_ledger.json)

3. **The prespecified priority proxy failed its stated objective.** At 35 selected conversations, mean random existing-FN capture is 10.02% and mean mixed capture is 7.30%; fixed priority captures 2.90%, versus a known-label oracle ceiling of 30.45%. The proxy was not retuned after observing failure. Capture counts existing FN judgments included, not newly discovered errors or measured quality gains. [E10–E13](11_evidence_ledger.json)

## Recommended Action

**QA lead:** preserve random measurement coverage and identify targeted diagnostic cases separately. **Policy owner:** arrange two independent blinded reviews; route Unsure to clarification, disagreement to adjudication, and reviewer agreement conflicting with reference to reference recheck. **Calibration owner:** develop versioned guidance and case exercises only after rationales establish repeatable ambiguity. **Analyst:** preregister an independent-case evaluation and sampling/inference design before rollout. All roles and actions are proposed; actual new reviews, label changes and completed coaching remain zero. [E14–E17](11_evidence_ledger.json)

## Confidence or Limitation

Intervals resample whole conversations, conditional on observed raters, fixed reference labels and the 175/175 research mix. Expert reference is fallible. Timing and severity ambiguities prevent SLA and validated harm-cost claims. Simulations assume pre-existing judgments and expert metadata; collection cost is excluded. Audit selection ranges are not bootstrap CIs. No production rate or causal improvement is established.

## What to Measure Next

On independently collected cases, record review completion and rationales; assess class-specific FN/FP, Unsure, coverage and consistency together. Set acceptance guardrails and dependence assumptions before collection. All 350 existing items informed exploration; an internal resplit is not an untouched holdout. Do not pool targeted-only cases unweighted into population estimates.

**Evidence and implementation:** [claim ledger](11_evidence_ledger.json), proposed validation and policy questions, [source attribution](../ATTRIBUTION.md), verified Power BI delivery. No raw dialogue or rater-level profiles are disclosed.
