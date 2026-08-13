"""Per-family metadata used to render generation prompts and scaffold the
evaluation driver.

Every value here is derived from the shipped family packages under `tasks/`;
the runner reads those packages but never modifies them. If a family's
interface changes, update this table to match — `check_families()` asserts the
table still lines up with what is on disk.
"""

import os
import re
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASKS = os.path.join(ROOT, "tasks")

# Rail profile assumed by every shipped Tier-1 family (`const Profile` in cases.go).
RAIL_PROFILE = "Queryable"

# The factory the model is asked to provide. Kept identical across families and
# conditions so the generated driver can bind candidates uniformly.
FACTORY_NAME = "NewCandidate"

# Unexported top-level identifiers the candidate declares are required to carry
# this prefix. The candidate file is compiled *inside* the family package (the
# wiring contract in generation/README.md), so without a prefix a helper named
# `fingerprint` or `conflict` collides with the reference implementation's own
# helpers and fails to build for reasons unrelated to retry-safety.
HELPER_PREFIX = "llm"


@dataclass(frozen=True)
class Family:
    """One Tier-1 task family."""

    name: str  # Go package name and tasks/<name>/ directory
    semantic_task_id: str  # matches the id the family's run.go emits
    method: str  # the Service method signature
    payload_field: str  # Request field carrying the payload ("Amount"/"Payload")
    task_description: str  # substituted into {{TASK_DESCRIPTION}}

    @property
    def method_name(self) -> str:
        return self.method.split("(", 1)[0]

    @property
    def dir(self) -> str:
        return os.path.join(TASKS, self.name)


_SHARED_HAZARDS = (
    "The same request may be delivered more than once, the process may crash "
    "after the provider commits but before success is recorded, the provider "
    "may return an unknown (timeout) outcome, and a second instance may handle "
    "the same identity concurrently."
)

FAMILIES = {
    f.name: f
    for f in [
        Family(
            name="capture",
            semantic_task_id="capture-t1",
            method="Capture(Request) harness.Response",
            payload_field="Amount",
            task_description=(
                "Debit a customer at most once per operation identity. "
                + _SHARED_HAZARDS
                + " Reusing an identity with a different amount must be rejected "
                "with CONFLICT and must produce no additional effect."
            ),
        ),
        Family(
            name="refund",
            semantic_task_id="refund-t1",
            method="Refund(Request) harness.Response",
            payload_field="Amount",
            task_description=(
                "Refund a customer at most once per operation identity. "
                + _SHARED_HAZARDS
                + " Reusing an identity with a different amount must be rejected "
                "with CONFLICT and must produce no additional effect."
            ),
        ),
        Family(
            name="outbox",
            semantic_task_id="outbox-t1",
            method="Publish(Request) harness.Response",
            payload_field="Payload",
            task_description=(
                "Publish a domain event effectively-once per event identity. The "
                "core hazard is a crash between reserving the outbox record and "
                "publishing the event: a naive relay either loses the event or "
                "publishes it twice. The publish acknowledgement may be dropped, "
                "the bus may return an unknown outcome, and a second relay "
                "instance may process the same event concurrently. Reusing an "
                "event identity with a different payload must be rejected with "
                "CONFLICT and must produce no additional publish."
            ),
        ),
        Family(
            name="consumer",
            semantic_task_id="consumer-t1",
            method="Consume(Request) harness.Response",
            payload_field="Payload",
            task_description=(
                "Produce at most one effect per message identity under "
                "at-least-once delivery. The same message may be delivered more "
                "than once or out of order, the process may crash after the "
                "effect commits but before it is recorded, the provider may "
                "return an unknown (timeout) outcome, and a redelivery may be "
                "handled concurrently by another worker. A redelivery carrying "
                "the same message id but a different payload must be rejected "
                "with CONFLICT and must produce no additional effect."
            ),
        ),
        Family(
            name="ledger",
            semantic_task_id="ledger-t1",
            method="Post(Request) harness.Response",
            payload_field="Amount",
            task_description=(
                "Post a double-entry journal entry at most once per posting "
                "identity. No duplicate journal is ever posted, and no accepted "
                "posting is ever lost. "
                + _SHARED_HAZARDS
                + " Reusing a posting identity with a different amount must be "
                "rejected with CONFLICT and must produce no additional posting."
            ),
        ),
        Family(
            name="saga",
            semantic_task_id="saga-t1",
            method="Execute(Request) harness.Response",
            payload_field="Amount",
            task_description=(
                "Execute one saga step at most once per step identity, using "
                "reserve-before-effect and reconcile-first recovery, so that a "
                "crash mid-step followed by recovery neither repeats nor loses "
                "the step. "
                + _SHARED_HAZARDS
                + " Reusing a step identity with a different amount must be "
                "rejected with CONFLICT and must produce no additional effect."
            ),
        ),
        Family(
            name="reconciliation",
            semantic_task_id="recon-t1",
            method="Apply(Request) harness.Response",
            payload_field="Amount",
            task_description=(
                "Apply one provider reconciliation update at most once per "
                "update identity, under delayed, duplicate, and reordered "
                "updates. "
                + _SHARED_HAZARDS
                + " Reusing an update identity with a different content "
                "fingerprint must be rejected with CONFLICT and must produce no "
                "additional effect."
            ),
        ),
    ]
}

ALL_FAMILIES = tuple(FAMILIES)


def check_families():
    """Verify the table still matches the family packages on disk.

    Guards against silent drift: if a family renames its Service method or its
    Request payload field, the prompts would otherwise describe an interface
    that no longer exists and every candidate would fail to compile for a
    reason that has nothing to do with the model.
    """
    problems = []
    for fam in FAMILIES.values():
        src_path = os.path.join(fam.dir, f"{fam.name}.go")
        if not os.path.exists(src_path):
            problems.append(f"{fam.name}: missing {src_path}")
            continue
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()

        iface = re.search(r"type Service interface\s*\{(.*?)\}", src, re.S)
        if not iface:
            problems.append(f"{fam.name}: no `type Service interface` found")
        elif fam.method.split("(")[0] not in iface.group(1):
            problems.append(
                f"{fam.name}: Service method {fam.method_name!r} not in {iface.group(1).strip()!r}"
            )

        req = re.search(r"type Request struct\s*\{(.*?)\}", src, re.S)
        if not req:
            problems.append(f"{fam.name}: no `type Request struct` found")
        elif not re.search(rf"^\s*{fam.payload_field}\s+harness\.", req.group(1), re.M):
            problems.append(
                f"{fam.name}: Request has no {fam.payload_field!r} field; got {req.group(1).strip()!r}"
            )

        run_path = os.path.join(fam.dir, "run.go")
        if os.path.exists(run_path):
            with open(run_path, encoding="utf-8") as fh:
                if f'"{fam.semantic_task_id}"' not in fh.read():
                    problems.append(
                        f"{fam.name}: semantic_task_id {fam.semantic_task_id!r} not found in run.go"
                    )
    return problems
