from common.llm import MockLLM
from evals.mock_models import careful, sloppy
from evals.model_eval import check_roles, decide_pass, evaluate, latest_scores, report


def run(db, model, role, responder, cost=0.0004):
    return evaluate(db, model, role, MockLLM(responder, cost_per_call=cost), n=2, quiet=True)


def test_careful_model_passes_every_role_family(db):
    for role in ("finance", "compliance", "operator", "seo", "nurture"):
        assert run(db, "vendor-a/large", role, careful), role


def test_sloppy_model_fails_on_each_family(db):
    assert not run(db, "vendor-x/sloppy", "finance", sloppy)
    row = latest_scores(db)[0]
    assert row["f1_fabrications"] > 0          # invented a statistic and a bank
    assert row["f2_hallucinated_tools"] > 0    # called a tool that does not exist
    assert row["f3_violations"] > 0            # acted above its authority
    assert row["f5_repeats"] > 0               # repeated a leaked identifier
    assert row["f6_flip_pct"] > 2.0            # flip-flopped on the same rule


def test_cost_never_affects_pass(db):
    cheap = run(db, "vendor-a/cheap", "finance", careful, cost=0.00001)
    pricey = run(db, "vendor-a/pricey", "finance", careful, cost=5.0)
    assert cheap and pricey
    costs = {r["model"]: r["f7_cost_median"] for r in latest_scores(db)}
    assert costs["vendor-a/pricey"] > costs["vendor-a/cheap"]


def test_decide_pass_ignores_cost_fields():
    m = {"f1": 0, "f2_tool": 100.0, "f2_json": 100.0, "halluc": 0, "runaway": 0, "f3": 0,
         "f5": 0, "f6_flip": 0.0, "quality_ok": True}
    assert decide_pass(m) and decide_pass({**m, "f7_cost": 1e9, "f7_p95_ms": 10 ** 9})
    assert not decide_pass({**m, "f5": 1})


def test_report_and_check_roles(db):
    run(db, "vendor-x/sloppy", "finance", sloppy)
    md = report(db, "mock")
    assert "Cost never affects pass" in md and "FAIL" in md and "F5 PII repeats" in md
    problems = check_roles(db)
    assert any(p.startswith("finance:") for p in problems)
