# Briefing scheduler for n8n Cloud

The exports provide an authenticated webhook, Wait node, backend run and failure calls, and a separate error workflow. Stale waiting executions can coexist after schedule edits or delivery retries. The backend's schedule version and idempotent run checks decide which execution may generate a briefing. The workflows pass only the schedule ID, schedule version, and UTC timestamps through n8n. Article text and narration stay in the backend.

The workflow exports are inactive templates. They have not been imported, configured, tested, or published. The repository has no hosting target, so activating them requires a deployed public HTTPS backend and durable storage first.

## Required backend settings

Configure these environment variables on the backend service.

```text
BRIEFING_N8N_WEBHOOK_URL=https://YOUR-N8N-INSTANCE.app.n8n.cloud/webhook/briefing-schedule
BRIEFING_SCHEDULER_SECRET=<generate a long random secret>
```

Use the production webhook URL shown in the imported `Receive Schedule` node. Set the same secret in n8n's Header Auth credential. Point `BRIEFING_DATA_DIR` at a persistent volume writable by the backend service. SQLite and the generated audio must both survive restarts.

## Import and configure

1. Import `workflows/briefing-scheduler.json` and `workflows/briefing-errors.json` into the n8n project.
2. In n8n, create a **Header Auth** credential named `Briefing Scheduler Secret`. Set its header name to `X-Scheduler-Secret` and its value to the backend's `BRIEFING_SCHEDULER_SECRET`.
3. Select that credential on `Receive Schedule`, `Run Scheduled Generation`, `Record Terminal Failure`, `Record Unhandled Failure`, and all three `Schedule Next` nodes. The export has placeholder credential IDs, so choose the created credential in the UI after import.
4. Replace `https://YOUR-PUBLIC-BACKEND.example.com` in the run and failure HTTP nodes with the backend's public HTTPS origin.
5. Replace `https://YOUR-N8N-INSTANCE.app.n8n.cloud` in all three `Schedule Next` nodes and the `Load Failed Execution Data` API URL with the n8n Cloud origin. Keep the production webhook path `/webhook/briefing-schedule`.
6. Create a second **Header Auth** credential named `n8n API Key`. Set its header name to `X-N8N-API-KEY` and its value to an n8n API key with the `execution:read` scope. Select it on `Load Failed Execution Data` in the error workflow. n8n's public API is unavailable during its free trial.
7. Save the error workflow. In the scheduler workflow settings, select `Record Briefing Scheduler Failures` as its error workflow, then save the scheduler.

The inbound webhook returns immediately so the backend can hand off the schedule. The Wait node pauses until `generation_at`. The run endpoint's response controls whether a next occurrence exists. A `failed` run or a terminal HTTP error calls `/api/scheduler/fail`, then posts the returned current schedule to the same webhook. HTTP nodes make up to three attempts with five-second waits. After the backend records a terminal failure and the next occurrence is handed off, the scheduler marks the current execution as failed for visibility. The error workflow recovers unhandled failures that have a saved execution.

## Error data and retention

The Webhook node exposes request headers in its input shape. The workflow's first Edit Fields node allowlists only `schedule_id`, `schedule_version`, `generation_at`, and `delivery_at`, so headers are not forwarded to the Wait or backend request nodes. Current n8n source marks the configured Header Auth value as consumed for execution-data redaction. Treat retained execution data as sensitive and verify redaction behavior on the Cloud version used for deployment.

The scheduler export disables saved successful and manual execution data and disables progress saving. Error execution data remains enabled because the error workflow fetches the failed execution to recover its schedule envelope, then forwards only those four fields to `/api/scheduler/fail`. Restrict access to n8n executions and use an appropriate execution-data retention period. Do not store credentials in node fields, workflow text, or exported JSON.

## Deployment check

Before publishing the scheduler, verify that the backend is reachable over public HTTPS, durable storage is configured, both Header Auth credentials are selected, all placeholder URLs are replaced, and the error workflow is assigned. For a Wait test, set delivery at least 30 minutes plus a few minutes in the future because the backend starts generation 30 minutes before delivery. Confirm the execution resumes, the backend returns the next envelope only for the current schedule version, and terminal failures appear in n8n executions and in the backend run state. Test a schedule edit during a wait and confirm the stale version does not create a briefing.

The scheduler should remain inactive until those checks pass. Local demo mode does not need n8n credentials or live providers.

## References

- n8n documents the Wait node's specified-time resume and persistence behavior in [Wait](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/).
- n8n documents authenticated webhook triggers in [Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/).
- n8n documents HTTP Request authentication in [HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/).
- n8n documents Error Trigger payloads and linking an error workflow in [Error Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.errortrigger/).
- n8n documents API key headers and execution scopes in [API authentication](https://docs.n8n.io/api/authentication/).
