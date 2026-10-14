---
sidebar_position: 99
title: Exact blocked task identity
---

`hermes kanban reconcile-blocked --request-file PATH` is an opt-in operation for
an independent, explicitly configured task that must remain initially blocked.
It always emits JSON on success. Ordinary `create` keeps its key-based semantics.

The UTF-8 JSON request has exactly `version: 1` and `identity`. Identity explicitly
supplies `idempotency_key`, `title`, `body`, `assignee`, `created_by`,
`workspace_path`, `skills`, `max_runtime_seconds`, `model_override`, and
`provider_override`. Strings are exact; non-body strings must be nonempty and
trimmed. Assignee and ordered nonempty skills must already be canonical.
Workspace path is absolute and lexically canonical; checking its actual source,
permissions and ownership is the caller's responsibility. Runtime is an integer
from 1 to 1800 seconds.

The remaining identity fields are explicit: `workspace_kind: "dir"`,
`max_retries: 1`, `completion_contract: "local-only"`, `priority: 0`,
`goal_mode: false`; `project_id`, `tenant`, `branch_name`, `reasoning_effort`,
`goal_max_turns`, and `session_id` are all null. This excludes project/board
defaults and creator/dependency inheritance. Unknown and duplicate JSON fields
are refused. The request is a regular nonsymlink file of at most 2MiB.

One owned IMMEDIATE transaction reconciles all surviving key matches, including
archived rows. Only one exact blocked row with initial created/blocked events,
no runs/claims/failure/workflow state and no links/comments/attachments/notification
subscriptions can be observed as existing. Ambiguous, changed or consumed
identities refuse without resetting or replacing them. Absence creates through
the native creator with explicit inputs, then verifies the row and related state
before commit. Delegated-child mutation fences remain enforced.

Success JSON includes `version`, `outcome` (`created` or `existing`), `task`,
`identity_sha256`, `initial_event_ids` and `commit_observed_at_ns`. The digest
binds canonical JSON identity, not external authorization. The captured row is
the committed transaction's observation; another writer can change it afterward.
The operation refuses nesting so an inner savepoint cannot masquerade as durable
success. Do not print an outcome before commit.

Initialization/opening can migrate or repair storage; this command is not a
write-free query. Failure after commit, including a post-commit integrity error
or lost stdout, may leave a committed card without a certified response.
Callers must journal the attempt before launch and stop on uncertainty. Database
and filesystem receipt publication are separate transactions. Never automatically
retry a lost response. Purged legacy task/event history cannot be reconstructed:
an independent durable caller ledger must prevent replay of consumed identities.

This interface neither authorizes a task nor releases a worker. Installed runtime
adoption, schema review, receipt reconciliation and caller acceptance remain
separate. It adds no model tool, dispatcher, gateway or policy override.

## Current SQL-read-only observation

`hermes kanban inspect-blocked --request-file PATH` uses the same exact request
schema and board/profile routing, but skips initialization, migration, repair and
readiness recomputation. The selected database must already exist as a canonical
regular file. A tracked `mode=ro` connection, `query_only` and a deny-by-default
SQL authorizer permit only fixed reads and a read transaction. No live
`immutable=1` opening or raw database-byte probing is used.

One read transaction covers the row and every related-history predicate. Success
JSON has `outcome: "observed"` with the exact pristine blocked task and initial
event IDs, or `outcome: "absent"` without a task. It includes `identity_sha256`,
`transaction_observed_at_ns` and selected database path/device/inode. These are
dated observations, never a writer commit certificate or authorization. Missing,
malformed, unsupported, mismatched, ambiguous or consumed identities refuse.

SQLite WAL access may use shared-memory and lock bookkeeping even for readonly
connections. SQL-data/schema read-only is not a promise of byte-immutable storage,
a standalone snapshot-acquisition certificate or universal writer exclusion.
Other writers can change state after the observed snapshot; only the separate
atomic reconcile operation can decide and create under its writer lock. Caller
authorization, durable attempts, current-state checks and receipt publication
remain required. Do not automatically repeat a mutation after lost output.
