# Agent reconciliation API

Beancount-import now exposes a sequential JSON API alongside the existing web
UI.  It reuses the same loaded journal, sources, candidate objects, and write
path as the UI, so an agent can reconcile transactions without scraping or
driving a browser.

The API is intentionally conservative:

- transaction matching remains the existing deterministic fuzzy-merge system;
- the decision-tree model is used only to predict unknown accounts;
- a ranked-first candidate is **not** automatically treated as high confidence;
- fuzzy merged candidates require agent review by default because the legacy
  match counters are ranking heuristics, not calibrated confidence;
- automatic acceptance is limited by default to unmerged imports with strong,
  verified account-prediction evidence;
- every manual write requires an exact preview and an idempotency key.

## Start the server

Existing launcher scripts do not need to change.  For example:

```shell
python3 my_import_script.py --read-only
```

Startup prints two URLs:

```text
Listening at http://127.0.0.1:8101
Agent API at http://127.0.0.1:8101/BEANCOUNT_IMPORT_SECRET_KEY_.../api/v1
```

The random path is the same cross-origin protection used by the web UI.  Save
the complete second URL as `API` for the current process:

```shell
API='http://127.0.0.1:8101/BEANCOUNT_IMPORT_SECRET_KEY_.../api/v1'
curl "$API"
curl "$API/state"
curl "$API/current"
```

Use `--read-only` while inspecting a real journal.  It blocks journal writes,
web-editor writes, and classifier-cache writes.  A third-party source plugin can
still have its own side effects, so source implementations must also be safe to
run in inspection mode.

## Endpoints

### `GET /state`

Returns load status, pending/error counts, classifier trust status, and the
current opaque revision.

### `GET /pending?start=0&limit=50`

Returns paginated pending entries.  The maximum page size is 200.

### `GET /current`

Returns the current pending entry, candidate summaries, used transactions, match
evidence, account-prediction evidence, diff risk, and the default policy's
recommendation.  To keep large journals responsive, this is a summary response;
each candidate includes a `details_url` for its exact full diff and associated
source data.

### `GET /current/candidates/{candidate_id}`

Returns the selected current candidate's exact staged diff, entries with final
journal metadata, and associated source data.  Candidate details are lazy so an
agent does not pay the cost of materializing every diff before choosing one.

Every decision must echo the complete `revision` object from this response.
The revision includes the server epoch, pending/candidate generations, pending
index and ID, and a hash of the exact candidate set.  A stale revision receives
HTTP `409 stale_state`.

### `POST /decision`

Preview an accept decision first:

```json
{
  "revision": { "copy": "the complete revision from GET /current" },
  "candidate_id": "candidate id from GET /current",
  "action": "accept",
  "changes": {
    "accounts": ["Expenses:Food"]
  },
  "dry_run": true
}
```

The response contains the exact diff, input file hashes, and a
`preview_token`.  Commit that same decision with `dry_run: false`, the returned
token, and a unique idempotency key:

```shell
curl -X POST "$API/decision" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 7f76a4e8-unique-per-decision' \
  --data @decision.json
```

Supported actions are:

- `accept`: apply the selected candidate;
- `ignore`: record only the raw, unmerged pending transaction in the ignored
  journal, restoring unknown accounts rather than persisting ML predictions;
- `defer`: move to a later pending item for the current process without writing
  files.

The same idempotency key with the same commit request replays the first result.
Reusing it with a different request returns `409 idempotency_conflict`.
Idempotency receipts live in memory and therefore do not survive a process
restart.

If journal files are successfully replaced but an in-memory index update,
next-case calculation, or response encoding then fails, the API still returns
HTTP 200 with `applied: true` and `postprocess_failed: true`.  That result is
stored under the supplied idempotency key before it is returned, and the server
forces a journal reload.  Poll the response's `recovery.poll` URL; do **not**
repeat the decision with a new key, because the journal write already happened.

### `POST /auto-accept`

The endpoint defaults to a dry run and evaluates only the current case:

```json
{
  "revision": { "copy": "the complete revision from GET /current" },
  "dry_run": true,
  "max_cases": 100
}
```

For a commit, set `dry_run` to `false` and provide an `Idempotency-Key` header.
The server accepts one case at a time and recomputes matching after every write.
It stops at the first case that requires review, when the maximum is reached,
or when reconciliation is complete.

Journal/source errors and invalid source references block automatic acceptance
by default.  They cannot be bypassed in an individual request.  A human can
authorize either exception for the whole server process at startup with
`--agent-auto-accept-with-errors` or
`--agent-auto-accept-with-invalid-references`.  Per-request `policy` values may
only tighten the server defaults (for example, increasing the probability
threshold or lowering the maximum transaction count); low-confidence cases
must use `/decision`.

### `POST /retrain`

Returns a dry-run summary by default.  A committed request requires the current
`revision` and an `Idempotency-Key`, then retrains the account classifier and
returns immediately with `status: "loading"`; poll `/state` for completion.
In read-only mode the model is retrained in memory and the cache is not written.

Any accepted journal change marks the old model as untrusted until this explicit
retrain, which rebuilds examples from the current journal.  This prevents a long
automatic run from retaining labels from replaced/deleted transactions or from
silently treating its own prior predictions as freshly-verified training data.

## Default automatic-acceptance policy

All relevant conditions must pass:

- the selected candidate is unmerged under the default server policy;
- one strict best candidate; heuristic ties always require review;
- no truncated matching search;
- no more than two consumed transactions;
- no unknown posting removal and no remaining `Expenses:FIXME` account;
- no removed existing transaction and at most one modified transaction;
- one output file, no automatically-created account;
- every predicted account is already open;
- classifier cache/model fingerprint and scikit-learn version match the current
  training data and runtime;
- prediction probability at least `0.99`;
- top-to-runner-up margin at least `0.95`;
- decision-tree leaf support at least `5`;
- at least one non-account/non-amount value feature is recognized.

The policy contains a server-side `allow_merged_transactions` escape hatch for
controlled experimentation, but it defaults to false and cannot be enabled by
a request.  If enabled in server configuration/code, the additional legacy
requirements still apply: one strict best merge, at least one cleared-posting
match, no truncation, and no unknown-posting removal.  These are conservative
heuristics rather than a calibrated match probability.

The account thresholds can be changed at startup:

```shell
--agent-account-probability-threshold 0.995 \
--agent-account-margin-threshold 0.98 \
--agent-account-min-leaf-samples 10
```

They can also be overridden per `/auto-accept` request under `policy`.

## Matching search limit

The compatibility default `--max-matches 0` preserves the original unlimited
merge search used by the existing Web UI and launcher scripts.  Large journals
can opt into a bound such as `--max-matches 10` to prevent exponential search.
If a limit actually truncates a search—even when it produces no merged
candidate—the candidate set is exposed to the agent but is never automatically
accepted.

## Write guarantees

- Preview tokens bind the revision, candidate fingerprint, normalized changes,
  exact diff, and current input file hashes.
- The journal editor still performs atomic replacement per file.
- A decision that modifies several files is **not** globally transactional
  across those files; the default auto policy therefore permits only one output
  file.
- If only a subset of a manual multi-file decision is replaced, the cached
  response uses `write_status: "partial"`, `fully_applied: false`, and lists
  both `applied_filenames` and `intended_filenames`.  Manual repair is required;
  retrying with a new key is unsafe.
- The API serializes work through the existing Tornado event loop.  On POSIX
  systems, writes also acquire deterministic per-journal-file advisory locks in
  the system temporary directory and re-check nanosecond mtime/size/inode
  signatures after locking.  Processes that bypass these locks are still
  detected by the signature/preview checks where possible.
- Windows falls back to a process-local write lock; do not run two writable
  import servers against the same journal there.
- In-process failures after a confirmed file replacement are reported as
  applied and cached for idempotent replay, then recovered by reloading from
  disk.  The reloaded classifier remains untrusted until `/retrain` is called.
- Idempotency receipts are not a durable write-ahead log.  A process or machine
  crash between the filesystem replacement and receipt creation can still
  require inspection of the journal before retrying.
