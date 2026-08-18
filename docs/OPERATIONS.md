# Operations

Day-to-day commands. Policy rationale lives in `README.md`; the protocol and
the request policy come from `NCPR_SESPA_SOAP_GTIN_Runbook.docx` (§ refs).

## First run on a new host

**This step is not optional.** Compose reads `.env` for `${DB_ROOT}`; with no
file it interpolates to empty and fails with
`invalid spec: :/data: empty section between colons`.

```bash
cp .env.example .env
```

### Two storage roots

| var | holds | put it on |
|---|---|---|
| `DB_ROOT` | SQLite, lock, `HALTED` | **local** ext4/xfs/zfs/btrfs |
| `ARCHIVE_ROOT` | `input/`, `raw/`, exports, WSDL | anywhere — a shared folder is the point |

`DB_ROOT` carries the daily counter that enforces the rate cap, and SQLite
needs working POSIX locks. **Never put it on a CIFS/SMB mount** — a corrupted
counter means uncontrolled request volume against an allowlisted service.
A locally-mounted ZFS dataset is fine even if OMV re-exports it over SMB;
what matters is how *this host* mounts it. Check before assuming:

```bash
findmnt -T /path/you/plan/to/use -o TARGET,SOURCE,FSTYPE,OPTIONS
```

`fstype` of `ext4`/`xfs`/`zfs`/`btrfs` is fine. `cifs` is not.
`ARCHIVE_ROOT` defaults to `DB_ROOT` if you want a single path.

### Archive layout

```
$ARCHIVE_ROOT/
├── input/      operator-supplied reference data (you put files here)
├── raw/        archived SOAP responses, one XML per call
├── service.wsdl
└── ncpr_gtin_crosswalk.csv
```

`input/` is separated so supplied data is never confused with harvest — the
same split as `bda-smpc-corpus`. It is also the reason `ARCHIVE_ROOT` wants
to be the shared folder: reference files can be dropped in over SMB without
touching the host.

### Ownership

The container drops to `HOST_UID:HOST_GID`, so **both** roots must be
writable by that identity. On OMV a shared folder is usually `root:users`,
and `users` is GID **100** on Debian — not 1000. Match what is actually
there rather than guessing:

```bash
ls -ln /path/to/shared/folder
```

Running as root does not change this: leave `HOST_UID=1000` and give the
directories to that identity rather than setting `HOST_UID=0`. The image
tolerates `0` (it skips creating an account that already exists), but
running the collector as root buys nothing and leaves root-owned files.

```bash
mkdir -p /var/lib/ncpr-collector && chown -R 1000:100 /var/lib/ncpr-collector
```

```bash
docker compose build
```

**Always `doctor` before collecting.** The most likely failure is running from
the wrong egress address — an SSH session alone does not make requests
originate from the whitelisted host (§3):

```bash
docker compose run --rm ncpr-collector python -m app.main doctor
```

Check that the printed egress IP matches the address registered with NCPR. If
it does not, stop: requests will 403 and a 403 halts the collector.
`doctor` never calls the product operations, so it cannot consume the daily
cap — run it as often as you like.

## Build the queue

Put three files in `$ARCHIVE_ROOT/input/`:

| file | without it |
|---|---|
| `Prilogenie-4-02-07-2026.xlsx` | nothing to queue |
| `drug_ref_min.csv` | band 20 cannot be identified — everything falls to band 40 |
| `salvia_products.csv` | band 10 (reconstruction validation) is empty |

```bash
docker compose run --rm ncpr-collector python -m app.queue_build --annex /archive/input/Prilogenie-4-02-07-2026.xlsx --include-reverse
```

Expect: 32,449 rows read, 3,135 active, ~3,414 tasks across bands 10/20/40.
Missing reference data is refused rather than silently degraded — a queue
with everything in band 40 runs the most decisive answers *last*, which is
the worst possible ordering for a 40-day job.

Add `--contested contested.csv` (a CSV with a `reg_number` column) to promote
registrations our own matchers disagree on into priority band 30.

**Re-running does not re-prioritise.** Tasks are inserted with
`INSERT OR IGNORE`, so an existing row keeps its original band. To rebuild an
ordering, delete the database first — safe only while nothing has been
collected:

```bash
rm "$DB_ROOT/ncpr.sqlite3"
```

## Build the pharmacist GUI catalogue

This is separate from the collection queue and makes no SESPA product calls.
The authoritative full source is `sources/ncpr/clean/ncpr_all_reimb_clean.csv`.
Only its currently active rows are imported; active Appendix 1 products receive
the highest search priority. PimChecker remains the ATC fallback for products
outside Appendix 1.

```bash
docker compose run --rm ncpr-collector python -m app.catalogue_build \
  --catalogue-csv /archive/input/ncpr_all_reimb_clean.csv \
  --priority-appendix /archive/input/ncpr_annex_clean.csv \
  --check-only
```

A successful check reports zero issues and the counts for all source rows,
active catalogue rows, and Appendix 1 priority rows. Re-run without
`--check-only` to replace `local_catalogue` transactionally. The legacy
`--annex` mode remains available for reproducibility, but it is no longer the
production catalogue source.

PimChecker SQL is optional in full-source mode. When omitted, Appendix 1 keeps
its official ATC coverage and products outside Appendix 1 remain searchable by
National ID, BDA number, trade name, and INN. Pass `--pim-sql` only when a
current snapshot is available and additional ATC enrichment is desired.

## Collect

```bash
docker compose up -d ncpr-collector
```

```bash
docker compose logs -f ncpr-collector
```

## Audit trail

Each manual lookup and collector result creates a compact SQLite `audit_event`
record with timestamp, operation, product identifier, SOAP action, outcome,
HTTP status, and whether local data changed. It never stores credentials or raw
SOAP payloads; raw responses remain in the archive volume. If Pangolin/Newt is
configured to strip and set `X-Forwarded-User` after authentication, set
`NCPR_TRUST_PROXY_IDENTITY=1` to retain that actor in audit records. Leave it
at `0` otherwise.

```bash
docker compose run --rm ncpr-collector python -m app.main status
```

Stopping is safe at any point — state is checkpointed per response and
`SIGTERM` interrupts the sleep rather than waiting it out:

```bash
docker compose down
```

## Export

```bash
docker compose run --rm ncpr-collector python -m app.main export
```

Writes `$ARCHIVE_ROOT/ncpr_gtin_crosswalk.csv` with **every field quoted**, so
a spreadsheet cannot turn `05712249101367` into `5.71225E+12`.

The web UI also provides **Export all collected CSV**. That CSRF-protected
download contains all rows currently stored in `product` and goes to the
operator browser's Downloads folder. It does not mark review rows exported.

When `NCPR_BULK_APPROVED=1`, **Start bulk export** launches the same collector
after an explicit pending-count confirmation. **Stop bulk export** requests a
cooperative stop. Do not remove the SQLite database between runs: it is the
persistent memory that prevents completed queue items from being repeated.

## If it halts

A `403` or `429` writes `HALTED` into `DB_ROOT` and the collector refuses to
start while that file exists. This is deliberate — it is the one situation
where a human must act before another request is sent.

```bash
cat "$DB_ROOT/HALTED"
```

1. **403** — confirm allowlist status with NCPR or the university network
   administrator. Do not retry first. Check the egress IP with `doctor`; a
   changed public address is the most common innocent cause.
2. **429** — honour `Retry-After`. If absent, wait at least 24 hours and seek
   guidance.
3. Only then delete the file to resume:

```bash
rm "$DB_ROOT/HALTED"
```

The queue is unchanged: the interrupted task stays `pending` and is retried
in priority order.

## Changing the rate

Edit `.env`, not the code. `app/config.py` refuses a delay below 60 s or a
daily cap above 500 — both are guards against a careless override, not
suggestions. Raising the rate without written guidance from NCPR risks the
allowlist, which is the only access path.

## Tests

```bash
docker compose run --rm ncpr-collector python -m pytest -q
```

Runs offline. `test_soap.py` checks the envelope and parser against the
runbook's verified §7 response — including that `05712249101367` keeps its
leading zero.
