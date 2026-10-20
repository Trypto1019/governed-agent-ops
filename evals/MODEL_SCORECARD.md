# Model scorecard (template)

This file is the committed TEMPLATE. `python -m evals.model_eval --report`
generates the live scorecard (default: `var/MODEL_SCORECARD.md`, git-ignored)
from the `model_evals` table.

eval_version `<version>` | provider `<openrouter|mock>` | generated `<timestamp>`

`pass` = F1 zero AND F2 thresholds AND F3 zero AND F4 role threshold AND F5 zero
AND F6 threshold. **Cost never affects pass**; it decides which passing model to use.

| model | compliance | finance | operator | seo | nurture |
|---|---|---|---|---|---|
| `<provider/model-a>` | **PASS** / $0.0000 | ... | ... | ... | ... |
| `<provider/model-b>` | FAIL / $0.0000 | ... | ... | ... | ... |

## Failure detail

- `<provider/model-b>` / **compliance**: F3 authority violations=1; F5 PII repeats=2

## Rules of use

1. A role's `role.json` may only name a `model` and `fallback_model` that have a
   passing scorecard for THAT role dated within 90 days.
2. `budget/provision_keys.py` refuses to issue a key otherwise, and the Verifier
   check `evals.charter_models_qualified` fails.
3. Among passing models, pick on cost and p95 latency (F7). Never the reverse.
