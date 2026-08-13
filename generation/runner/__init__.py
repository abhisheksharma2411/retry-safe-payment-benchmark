"""T4 LLM generation + evaluation runner (see docs/EVAL_RUNNER.md).

Implements P5 of CODING_AGENT_PROMPTS.md: renders the four generation-condition
prompts, calls a model, archives every raw completion, gates on compilation, and
scores surviving candidates against the shipped families and oracle.

Written in Python deliberately. The Go module is standard-library-only and its
Dockerfile states there is nothing to `go mod download`; adding an HTTP client
SDK to `go.mod` would break that property and the repo's own ground rules. The
runner therefore drives the Go toolchain as a subprocess and never modifies
`harness/`, `tasks/`, `cmd/`, or `go.mod`.
"""

__all__ = ["evaluate", "families", "model", "prompts"]
