PYTHON ?= python3

.PHONY: demo test scan clean

demo:            ## seed SQLite, run dispatcher/verifier/watchdog/approvals offline
	$(PYTHON) demo/run_demo.py

test:            ## run the test suite (offline)
	$(PYTHON) -m pytest

scan:            ## secret scan + compliance gate self-check
	gitleaks dir --redact --no-banner .
	$(PYTHON) -m compliance.compliance_check --eval-set --no-llm

clean:
	rm -rf var .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
