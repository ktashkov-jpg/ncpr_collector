# Operations

Day-to-day commands. Policy rationale lives in `README.md`; the protocol and
the request policy come from `NCPR_SESPA_SOAP_GTIN_Runbook.docx` (§ refs).

## First run on a new host

**This step is not optional.** Compose reads `.env` for `${DATA_ROOT}`; with
no file it interpolates to empty and fails with
`invalid spec: :/data: empty section between colons`.

```bash
cp .env.example .env
```

Set `DATA_ROOT` to an absolute path, and set `HOST_UID`/`HOST_GID` so files
on the volume are owned by a real host identity:

```bash
id -u && id -g
```

### If you are running as root

Leave `HOST_UID=1000` / `HOST_GID=1000` and give the data directory to that
identity — do **not** set them to `0`. The container drops to that uid, so
a root-owned volume is unwritable:

```bash
mkdir -p /srv/ncpr-collector/data && chown -R 1000:1000 /srv/ncpr-collector/data
```

The image tolerates `HOST_UID=0` if you insist (it skips creating an account
that already exists), but running the collector as root buys nothing and
leaves root-owned files on the volume.

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

## Build the queue

Put the Annex 4 workbook on the data volume first.

```bash
docker compose run --rm ncpr-collector python -m app.queue_build --annex /data/Prilogenie-4-02-07-2026.xlsx --include-reverse
```

Add `--contested contested.csv` (a CSV with a `reg_number` column) to promote
registrations our own matchers disagree on into priority band 30.

## Collect

```bash
docker compose up -d ncpr-collector
```

```bash
docker compose logs -f ncpr-collector
```

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

Writes `/data/ncpr_gtin_crosswalk.csv` with **every field quoted**, so a
spreadsheet cannot turn `05712249101367` into `5.71225E+12`.

## If it halts

A `403` or `429` writes `/data/HALTED` and the collector refuses to start
while that file exists. This is deliberate — it is the one situation where a
human must act before another request is sent.

```bash
cat "$DATA_ROOT/HALTED"
```

1. **403** — confirm allowlist status with NCPR or the university network
   administrator. Do not retry first. Check the egress IP with `doctor`; a
   changed public address is the most common innocent cause.
2. **429** — honour `Retry-After`. If absent, wait at least 24 hours and seek
   guidance.
3. Only then delete the file to resume:

```bash
rm "$DATA_ROOT/HALTED"
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
docker compose run --rm ncpr-collector python -m pytest tests -q
```

Runs offline. `test_soap.py` checks the envelope and parser against the
runbook's verified §7 response — including that `05712249101367` keeps its
leading zero.
