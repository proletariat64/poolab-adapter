PYTHON ?= python3
BRANCH ?= feat/standalone-via
WORKTREE ?= .worktrees/standalone-via

.PHONY: bootstrap check doc-guard
bootstrap:
	$(PYTHON) scripts/bootstrap.py --branch "$(BRANCH)" --worktree "$(WORKTREE)"

check:
	$(PYTHON) scripts/check.py

doc-guard:
	$(PYTHON) scripts/doc_guard.py
