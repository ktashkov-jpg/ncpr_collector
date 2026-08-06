# Operations

Day-to-day commands. Policy rationale lives in `README.md`; the protocol and
the request policy come from `NCPR_SESPA_SOAP_GTIN_Runbook.docx` (Â§ refs).

## First run on a new host

**This step is not optional.** Compose reads `.env` for `${DATA_ROOT}`; with
no file it interpolates to empty and fails with
`invalid spec: :/data: empty section between colons`.

```bash
cp .env.example .env
```

### Two storage roots

| var | holds | put it on |
|---|---|---|
| `DB_ROOT` | SQLite, lock, `HALTED` | **local** ext4/xfs/zfs/btrfs |
| `ARCHIVE_ROOT` | raw XML, exports, WSDL | anywhere â€” a shared folder is the point |

`DB_ROOT` carries the daily counter that enforces the rate cap, and SQLite
needs working POSIX locks. **Never put it on a CIFS/SMB mount** â€” a corrupted
counter means uncontrolled request volume against an allowlisted service.
A locally-mounted ZFS dataset is fine even if OMV re-exports it over SMB;
what matters is how *this host* mounts it. Check before assuming:

```bash
findmnt -T /path/you/plan/to/use -o TARGET,SOURCE,FSTYPE,OPTIONS
```

`fstype` of `ext4`/`xfs`/`zfs`/`btrfs` is fine. `cifs` is not.
`ARCHIVE_ROOT` defaults to `DB_ROOT` if you want a single path.

### Ownership

The container drops to `HOST_UID:HOST_GID`, so **both** directories must be
writable by that identity. On OMV a shared folder is usually `root:users`,
and `users` is GID **100** on Debian â€” not 1000. Match what is actually
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
the wrong egress address â€” an SSH session alone does not make requests
originate from the whitelisted host (Â§3):

```bash
docker compose run --rm ncpr-collector python -m app.main doctor
```

Check that the printed egress IP matches the address registered with NCPR. If
it does not, stop: requests will 403 and a 403 halts the collector.

## Build the queue

Put the Annex 4 workbook on the **archive** volume â€” that is the shared
folder, so it can be dropped there over SMB without touching the host.

```bash
docker compose run --rm ncpr-collector python -m app.queue_build --annex /archive/input/Prilogenie-4-02-07-2026.xlsx --include-reverse
```

Expect roughly: 32,449 rows read, 3,135 active, and a queue of ~3,414 tasks
across bands 10/20/40. If `active` comes back near zero, the workbook's
declared range is the cause â€” see the `reset_dimensions()` note in
`queue_build.read_annex`.

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

Stopping is safe at any point â€” state is checkpointed per response and
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
while that file exists. This is deliberate â€” it is the one situation where a
human must act before another request is sent.

```bash
cat "$DATA_ROOT/HALTED"
```

1. **403** â€” confirm allowlist status with NCPR or the university network
   administrator. Do not retry first. Check the egress IP with `doctor`; a
   changed public address is the most common innocent cause.
2. **429** â€” honour `Retry-After`. If absent, wait at least 24 hours and seek
   guidance.
3. Only then delete the file to resume:

```bash
rm "$DATA_ROOT/HALTED"
```

The queue is unchanged: the interrupted task stays `pending` and is retried
in priority order.

## Changing the rate

Edit `.env`, not the code. `app/config.py` refuses a delay below 60 s or a
daily cap above 500 â€” both are guards against a careless override, not
suggestions. Raising the rate without written guidance from NCPR risks the
allowlist, which is the only access path.

## Tests

```bash
docker compose run --rm ncpr-collector python -m pytest tests -q
```

Runs offline. `test_soap.py` checks the envelope and parser against the
runbook's verified Â§7 response â€” including that `05712249101367` keeps its
leading zero.

