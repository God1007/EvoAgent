# Transactional outbox and external-effect idempotency

Asynchronous review acceptance no longer performs “insert task, then publish
queue message” as two unrelated operations. EvoAgent commits the task record,
Diff payload, and one `outbox_messages` row in the same Store transaction. A
separate dispatcher leases committed rows and publishes them through the active
`TaskQueuePort`.

## Delivery sequence

```text
HTTP/webhook request
  -> Store transaction
       webhook only: bind delivery + append PR session turn
       closed/draft webhook only: end session + cancel unfinished tasks
       insert task
       insert task payload (or deferred-diff metadata)
       insert outbox message with task-id message key
     COMMIT
  -> notify dispatcher
  -> lease outbox row
  -> TaskQueue.submit(payload, message_id=task_id)
  -> mark outbox row published
  -> queue worker executes checkpointed review
```

The API returns `202 PENDING` after the database commit. Queue unavailability
does not orphan or discard the accepted task: the pending row remains the source
of recovery and the dispatcher retries with capped exponential delay.

## Failure boundaries

| Failure point | Recovery behavior |
| --- | --- |
| Before Store commit | Task, payload, and outbox all roll back |
| Webhook failure during intake | Delivery, session turn, task, and outbox all roll back |
| Concurrent duplicate webhook | Unique delivery id returns the one already-bound task |
| Delayed older PR webhook | Its delivery is recorded, but the session event-time fence prevents state reversal or task creation |
| PR closes, returns to draft, or advances again | The delivery, session state and unfinished-task cancellation commit together; comment delivery requires the latest open turn |
| Concurrent duplicate API intake | Tenant-scoped `Idempotency-Key` returns the original task; changed content is rejected |
| After commit, before queue publish | Dispatcher/restarted process leases the pending row |
| After publish, before `published` update | Same message key is submitted again; queue dedupe prevents a second delivery |
| Dispatcher dies while leasing | Another dispatcher reclaims the row after `lease_until` |
| Dispatcher dies on the final allowed lease | The expired row moves atomically to `dead` instead of remaining `publishing` forever |
| Queue remains unavailable | Attempt budget moves the row to `dead`; metrics and `/ready` expose it for recovery |
| Worker fails after queue acceptance | Queue retry/lease/DLQ semantics apply; graph checkpoints support resume |

Memory-queue dedupe is process-local and retained for a bounded one-hour
window. Redis uses one Lua operation to check the message key, append the Stream
entry, and set a one-hour dedupe key. This closes both “marker without message”
and “message without marker” process-crash windows for the configured retry
horizon. Dead-letter replay intentionally bypasses submission dedupe because it
is an explicit operator action.

The queue remains at-least-once. EvoAgent does not claim exactly-once execution:
review computation may repeat after a worker crash. External publication is
made retry-safe instead:

- PR comments use a stable task/session marker and GitHub upsert;
- `effect_receipts` serialize concurrent comment/fix publication and cache
  completed repair results;
- repair branches are derived from the tenant/task effect key;
- a retry reuses an existing repair branch only after its content-addressed tree
  and sole parent match the newly verified repair, then reuses only its matching
  open draft PR.

## Operations

Readiness exposes dispatcher state and counts:

```json
{
  "checks": {
    "outbox": {
      "dispatcher_running": true,
      "pending": 0,
      "publishing": 0,
      "dead": 0,
      "last_error": ""
    }
  }
}
```

Metrics include:

- `evoagent_outbox_pending` and `evoagent_outbox_dead` gauges;
- `evoagent_outbox_published_total`;
- `evoagent_outbox_publish_failures_total`;
- `evoagent_outbox_dispatch_failures_total`;
- `evoagent_outbox_lease_conflicts_total`;
- `evoagent_effect_lease_conflicts_total` for lost comment/repair ownership;
- `evoagent_review_idempotent_replays_total` counts API retries that reused the
  original tenant task without consuming another admission slot.

Administrators can inspect dead rows and explicitly replay one after correcting
the dependency failure. Tenant scope comes from the authoritative task row, not
the message payload; the replay attempt and its actor audit commit together:

```bash
curl '/api/outbox?status=dead&limit=100' -H 'Authorization: Bearer ...'
curl -X POST '/v1/outbox/replay' \
  -H 'Authorization: Bearer ...' \
  -H 'Content-Type: application/json' \
  -d '{"message_id":"review:<task-id>"}'
```

Replay resets the publication-attempt budget and wakes the dispatcher. It does
not delete history and is written to the tenant audit log.

Configuration:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `EVOAGENT_OUTBOX_POLL_SECONDS` | `0.25` | Idle polling interval; notify wakes sooner |
| `EVOAGENT_OUTBOX_BATCH_SIZE` | `50` | Rows leased per dispatch pass |
| `EVOAGENT_OUTBOX_LEASE_SECONDS` | `30` | Publisher ownership lease |
| `EVOAGENT_OUTBOX_MAX_ATTEMPTS` | `20` | Attempts before `dead` |
| `EVOAGENT_EFFECT_LEASE_SECONDS` | `300` | GitHub comment and repair publication ownership lease; minimum `300` |

Each review task owns a distinct comment effect receipt, while all turns in one
PR session reuse the same hidden GitHub marker. Retries of one task are deduped;
a newer completed turn updates the existing session comment instead of being
mistaken for an already-published old turn.
Repair verification may outlive the initial lease without writing externally.
Immediately before each GitHub POST/PATCH attempt, the worker atomically renews
the same owner lease; a reclaimed worker therefore stops before publishing. The
same fence protects review-comment creation and updates. The transport retries
only GET/HEAD; an ambiguous write failure is reconciled by the durable effect
replay instead of blindly repeating a non-idempotent request.

## Verification evidence

`tests/test_outbox.py` injects failures at transaction insert, post-publish
pre-ack, lease ownership, retry-budget, and effect-receipt boundaries. PostgreSQL
contract tests cover the task/outbox/effect transaction behavior. The mandatory
external-service CI matrix additionally kills a Redis
consumer process after delivery but before ACK, then proves lease reclaim,
dedupe across queue restart, durable DLQ storage, and worker recovery after a
socket disconnect. Local memory tests are not presented as Redis durability
evidence.
