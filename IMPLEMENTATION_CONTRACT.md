# Module 01 implementation contract (v0.1)

Python 3.11+, standard library only, package `sdl_m01`. Data are JSON-compatible dictionaries. SQLite persistence. No arbitrary code/expression execution. Public `Module01(db_path, token)` authenticates bearer tokens (hashed in storage), never trusts a caller-supplied role. `initialize(db_path)` creates a new vault and returns four role tokens: `custodian`, `explorer`, `confirmer`, `auditor`. Local database and custodian credentials are trusted administrator assets, not an OS-level security boundary.

## Data configuration

```
{
 "task": {"objects":"...", "environments":["lab-A"], "time_scope":"...",
          "claim_types":["prediction"], "target_quantity":"batch mean loss gain",
          "weighting":"equal_batch", "eligibility":"all registered batches"},
 "schema": {"version":"1", "fields": {
    "X1":{"type":"number","role":"feature","unit":"mol/L", "required":true,
          "missing_allowed":true,"minimum":0,"maximum":null,"range_action":"flag"},
    "Y":{"type":"number","role":"target","unit":"mol/(L*s)","required":true}
 }, "missing_codes":[null,"NA"], "source_required":true},
 "dependence": {"record_unit":"reading", "split_unit":"batch", "inference_unit":"batch",
                "group_fields":["batch"], "namespace":"experiment-A", "assumptions":["independent batches"]},
 "split": {"strategy":"grouped", "allocation":{"E":0.6,"V":0.2,"C1":0.2}, "seed":42},
 "quality": {"version":"1", "notes":[]},
 "confirmation": {"total_alpha":0.05, "sampling_plan":"new independent batches",
                  "stopping_rule":"fixed batch count"},
 "identification_gaps": []
}
```

Raw record shape: `record_id`, `group_ids` (dict), `environment` (string), `event_time` (timezone-aware ISO8601), `available_time` (ISO string or per-field dict), `prediction_time` optional ISO string, `values` dict, `units` dict, `source` nonempty string or dict, `missing_mask` optional field->reason dict. Unknown fields must be preserved. Missing values never silently imputed, unknown units quarantine. Out-of-range defaults to flag, not delete.

## Owned data engine interface (`models.py`, `data.py`)

`ValidationError(ValueError)` lives in `errors.py` (root owns).

* `validate_spec(spec) -> dict`: validate complete task and schema; normalize documented defaults, copy inputs.
* `assess_records(records, spec) -> list[dict]`: each item `{record, status, reasons, fingerprint, unit_keys}`. `status` usable/flagged/quarantined/excluded; `reasons` list of strings; fingerprint stable content excluding `record_id` (do not strip provenance). `unit_keys` stable namespaced group identities; missing identity => empty and quarantine. Repeated record IDs quarantine all duplicates.
* `partition_records(assessed, spec) -> dict[str,list[dict]]`: E/V/C1... plus Q (unassigned). Group membership assigned before filtering quality; bad row cannot shift its group. Grouped partitioning must preserve connected groups across all declared group fields. Reproducible independently of input ordering. Allocation must sum to 1 and allocate at least one group per nonzero partition, or reject.
* `quality_report(assessed, spec) -> dict`: counts by status, missing rates with explicit denominator, unit conflicts, group counts, notes (not an effective independent n).

Additional strategies: `environment` groups by environment, optionally `environment_assignments` mapping environment->purpose instead of allocation; `temporal` uses ordered `cutoffs` mapping E/V/C1...->exclusive upper ISO bound, optional `gap_seconds` before every internal boundary. All observation event/availability checks use parsed times, not string comparisons. Prediction-time feature availability checked when `prediction_time` exists; late target availability is allowed and recorded. Unknown identity/time enters Q or quarantine, not silently train. Temporal split is for within-subject future prediction; same group across time is permitted. Feature availability and labels used for earlier training must respect that partition's information cutoff.

## Root-owned service/storage API

`initialize(db_path) -> dict[str,str]` refuses an existing database. `Module01(db_path, token)` rejects bad tokens. Methods raise `ValidationError`, `AccessDenied`, `StateError`, `IntegrityError` (errors.py), log failed authenticated operations without raw secret payloads.

* `build(spec, records) -> public_protocol`: custodian; transactionally creates immutable raw/partition snapshots and private QC; returns `{protocol_id, version, resources:{E:ref,V:ref,C1:ref,...}, readiness,...}`. Q ref is restricted. Sealed refs are opaque; no sealed counts/reports/fingerprints in public results. Store tracks prior record fingerprints and unit assignments across protocols; rebuild or reingest cannot reset old identities.
* `describe(protocol_id) -> public_protocol`: all roles, no sealed QC, raw data or fingerprint listings.
* `read_dataset(ref) -> list[dict]`: explorer/custodian on E,V,historical only, records usable/flagged with `_quality`; forbidden on sealed/bound/used confirmation. Confirmer uses consume. Quarantined/excluded records are retained privately.
* `quality(ref) -> dict`: explorer only E,V,historical; custodian/auditor can inspect restricted QC (audit is not an exploration role).
* `bind_confirmation(ref, plan) -> {binding_id,plan_digest,alpha,state}`: confirmer only. Frozen plan below; binds only sealed nonempty valid batches; validates purpose, protocol identity, unit and policy declarations, fit refs, complete finite tests, round uniqueness. Alpha auto `total_alpha/2**round_index`. This validates declared compatibility, not statistical truth.
* `consume_confirmation(binding_id, replay=False) -> list[dict]`: confirmer only. Atomic `used` transition+ledger commit BEFORE any returned content. First use once; explicit replay only same immutable binding (not new evidence). Snapshot hash verified.
* `record_evaluation(binding_id, result) -> dict`: confirmer only, after use. result `{status:supported|refuted|inconclusive|failed, metrics:{}, notes:"...", code_version:"..."}`. This records external M6 results, does not compute p values or certify truth. Failed attempts preserved; final result immutable.
* `release_results(binding_id) -> dict`: confirmer; publishes recorded results for explorer; never raw sealed records. `results(binding_id)` only returns released results to explorer.
* `archive_confirmation(binding_id) -> {historical_ref}`: custodian after recorded+released result, preserve original consumed binding; creates historical exploration view (not new confirmation).
* `mark_compromised(ref, reason)`: custodian; sealed/bound -> compromised, irreversible. `inspect_confirmation(ref, reason)` custodian marks compromised before returning content (for emergency inspection).
* `add_confirmation(protocol_id, records, purpose) -> {ref}`: custodian; new C2,C3... (not old fingerprints; grouped designs not old units; temporal additions strictly later than previous time for shared units); private QC, sealed state.
* `ledger() -> list[dict]`: custodian/auditor only. `verify_integrity() -> dict`: custodian/auditor; checks all snapshot digests and ledger chain.

Frozen plan:
```
{"protocol_id":"...","round_index":1,
 "hypotheses":[{"id":"h1","version":"1","statement":"...","prediction":"...",
                 "scope":"...","representation":{},"model":"...","parameters":{}}],
 "preprocessing":{"steps":[],"fit_dataset_refs":["E ref"]},
 "primary_metric":"...", "test_family":[{"id":"t1","hypothesis_id":"h1",
     "null":"...","alternative":"...","method":"..."}],
 "effect_threshold":0.1,"sampling_plan":"same as spec", "stopping_rule":"same as spec",
 "inference_unit":"same as spec", "eligibility":"same as task", "quality_rules_version":"1",
 "assumptions":[{"name":"independent batches","justification":"..."}]}
```

No mutable plan escapes into stored state; canonical JSON freeze digest. Explicit actor keys are administrative credentials, never shipped in an explorer-facing protocol. All CLI inputs/outputs UTF-8 JSON; no external service dependency.

## Deliverables

`python -m unittest discover -s tests -v`; `python -m sdl_m01 demo --output <new directory>` produces a 120x8 grouped synthetic example, public protocol, E/V views and an illustrative external evaluation event through the full lifecycle. Demo clearly synthetic, not actual statistical confirmation. CLI `init/build/describe/read/quality/bind/consume/record/release/results/archive/add-confirmation/compromise/audit/verify`, auth via token file containing one token or env `SDL_M01_TOKEN`, never token in argv. README maps implementation to theory and states filesystem/security and statistical limits.
