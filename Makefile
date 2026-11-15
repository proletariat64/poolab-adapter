PYTHON ?= python3
BRANCH ?= feat/standalone-via
WORKTREE ?= .worktrees/standalone-via
POOLAB_ALIAS ?= default
VERSION ?= v$(shell $(PYTHON) -c 'from poolab_adapter import __version__; print(__version__)')
export BRANCH WORKTREE POOLAB_ALIAS VERSION

.PHONY: bootstrap check doc-guard package smoke release
bootstrap:
	$(PYTHON) scripts/bootstrap.py --branch "$$BRANCH" --worktree "$$WORKTREE"

check:
	$(PYTHON) scripts/check.py

doc-guard:
	$(PYTHON) scripts/doc_guard.py

package:
	$(PYTHON) scripts/package.py

smoke: package
	$(PYTHON) scripts/smoke.py

release:
	$(PYTHON) scripts/release.py --version "$$VERSION" $(if $(filter 1,$(DRY_RUN)),--dry-run,)
