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
| After commit, before queue publish | Dispatcher/restarted process leases the pending row |
| After publish, before `published` update | Same message key is submitted again; queue dedupe prevents a second delivery |
| Dispatcher dies while leasing | Another dispatcher reclaims the row after `lease_until` |
| Queue remains unavailable | Attempt budget moves the row to `dead`; readiness becomes not-ready |
| Worker fails after queue acceptance | Queue retry/lease/DLQ semantics apply; graph checkpoints support resume |

Memory-queue dedupe is process-local and retained for a bounded seven-day
window. Redis uses one Lua operation to check the message key, append the Stream
entry, and set a seven-day dedupe key. This closes both “marker without message”
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
- a retry discovers and reuses an existing repair branch and draft PR.

## Operations

Readiness exposes dispatcher state and counts:

```json
{
  "checks": {
    "outbox": {
      "dispatcher_running": true,
      "pending": 0,
      "publishing": 0,
      "dead": 0
    }
  }
}
```

Metrics include:

- `evoagent_outbox_pending` and `evoagent_outbox_dead` gauges;
- `evoagent_outbox_published_total`;
- `evoagent_outbox_publish_failures_total`;
- `evoagent_outbox_dispatch_failures_total`;
- `evoagent_outbox_lease_conflicts_total`.

Administrators can inspect dead rows and explicitly replay one after correcting
the dependency failure:

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
| `EVOAGENT_EFFECT_LEASE_SECONDS` | `300` | Repair publication ownership lease |

## Verification evidence

`tests/test_outbox.py` injects failures at transaction insert, post-publish
pre-ack, lease ownership, retry-budget, and effect-receipt boundaries. PostgreSQL
contract tests cover the task/outbox/effect transaction behavior. The mandatory
external-service CI matrix additionally kills a Redis
consumer process after delivery but before ACK, then proves lease reclaim,
dedupe across queue restart, durable DLQ replay, and worker recovery after a
socket disconnect. Local memory tests are not presented as Redis durability
evidence.
