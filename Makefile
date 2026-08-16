# T4 benchmark — reproducibility Makefile.
#
# All commands are POSIX-sh compatible and assume a Go 1.24 toolchain plus
# python3 with matplotlib and numpy (see Dockerfile for a pinned environment).
# Only the `figures` step needs Python; everything else is pure Go.
#
# The harness is deterministic. On the same toolchain the data artifacts
# (pilot_results.json, metrics.csv, metrics_summary.json, paper_data.tex)
# regenerate byte-for-byte; the figure PNGs regenerate pixel-for-pixel but are
# NOT byte-identical across platforms, because libpng/zlib builds and the
# matplotlib version stamp differ. Use the Dockerfile for a byte-stable image.

GO      ?= go
PYTHON  ?= python3
PILOT    = results/pilot_results.json

.PHONY: all smoke test-harness test-references test-mutants test-determinism \
        test pilot figures reproduce-small reproduce-paper manifest clean help

all: reproduce-small

## help: list the available targets
help:
	@echo "T4 benchmark targets:"
	@echo "  make smoke            build everything + fast harness tests (sanity check)"
	@echo "  make test-harness     verbose harness (kernel + oracle) tests"
	@echo "  make test-references  every correct reference passes every schedule"
	@echo "  make test-mutants     every seeded bug is caught by its intended invariant"
	@echo "  make test-determinism identical (candidate, schedule, seed) => identical run"
	@echo "  make test             the full Go test suite"
	@echo "  make pilot            run the pilot -> $(PILOT) (+ summary, shrink demo)"
	@echo "  make figures          regenerate figures/metrics from $(PILOT)"
	@echo "  make reproduce-small  test + pilot + figures (the main reviewer target)"
	@echo "  make reproduce-paper  reproduce-small, then print results/metrics_summary.json"
	@echo "  make manifest         print the SHA-256 of $(PILOT)"
	@echo "  make clean            remove build output (keeps committed artifacts)"
	@echo ""
	@echo "Python: only 'figures' (and therefore 'reproduce-small') needs python3"
	@echo "  with matplotlib + numpy. If they are missing:"
	@echo "    python3 -m venv .venv && .venv/bin/pip install matplotlib numpy"
	@echo "    make figures PYTHON=.venv/bin/python"
	@echo "  Or use the Dockerfile, which ships a pinned plotting stack."
	@echo ""
	@echo "Reproducibility: data artifacts (pilot_results.json, metrics.csv,"
	@echo "  metrics_summary.json, paper_data.tex) regenerate byte-for-byte on the"
	@echo "  same toolchain. Figure PNGs regenerate pixel-for-pixel but are not"
	@echo "  byte-identical across platforms (libpng/zlib + matplotlib version"
	@echo "  stamp); use the Dockerfile for a byte-stable image."

## smoke: compile the whole module and run the harness tests quickly
smoke:
	$(GO) build ./...
	$(GO) test ./harness/

## test-harness: kernel + oracle tests, verbose
test-harness:
	$(GO) test ./harness/ -v

## test-references: the correct reference of every family passes every schedule
test-references:
	$(GO) test ./tasks/... -run TestCorrectReferencePassesAll -v

## test-mutants: every seeded-bug mutant is caught by its intended invariant
test-mutants:
	$(GO) test ./tasks/... -run TestMutantsAreCaught -v

## test-determinism: identical (candidate, schedule, seed) produces identical runs
test-determinism:
	$(GO) test ./tasks/... -run TestDeterminism

## test: the full test suite (harness + every family)
test:
	$(GO) test ./...

## pilot: execute every candidate x schedule and archive the raw records
pilot:
	$(GO) run ./cmd/t4run

## figures: regenerate figures, metrics_summary.json, metrics.csv, paper_data.tex
figures:
	$(PYTHON) analysis/make_figures.py

## model-figures: paper outputs from the LLM evaluation (results/model_results.json)
##   Separate from `figures` on purpose: that target regenerates the deterministic
##   reference/mutant pilot, this one summarises a stochastic model sweep. Neither
##   reads the other's input, and this one never touches pilot_results.json.
model-figures:
	$(PYTHON) analysis/make_model_figures.py
	$(PYTHON) analysis/recompute_report.py

## reproduce-small: the main reviewer entry point (a few seconds end to end)
reproduce-small: test pilot figures
	@echo "reproduce-small complete: results/ and analysis/figures/ regenerated."

## reproduce-paper: reproduce-small, then echo the headline numbers
reproduce-paper: reproduce-small
	@echo "=================== results/metrics_summary.json ==================="
	@cat results/metrics_summary.json
	@echo ""

## manifest: SHA-256 of the raw pilot records (basis of the hidden-set commitment)
manifest:
	@if command -v sha256sum >/dev/null 2>&1; then \
		sha256sum $(PILOT); \
	else \
		shasum -a 256 $(PILOT); \
	fi

## clean: remove build output only; committed artifacts (results/, figures/) stay
clean:
	$(GO) clean ./... 2>/dev/null || true
	rm -rf bin
	find . -name '*.test' -delete 2>/dev/null || true
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
