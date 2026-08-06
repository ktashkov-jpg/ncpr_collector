# ncpr-collector

Harvests the **official** Bulgarian national-package-identifier â†” GTIN
mapping from the NCPR public SOAP service (SESPA), under a deliberately
conservative request policy.

Source of truth for the protocol and the policy:
`NCPR_SESPA_SOAP_GTIN_Runbook.docx` (verified 6 August 2026). Section
references below (Â§n) point at that document.

## Why this matters more than "more barcodes"

Every other source in `GS1_barcode` is a retailer, and every retailer match
runs through heuristics â€” `inn_key()`, `FORM_MAP`, `brand_head()`,
transliteration. This one does not. Annex 4 carries both the NCPR national
identifier (col Z) **and** the 8-digit registration number (col B), and
**100% of active Annex 4 registration numbers (2,115/2,115) are present in
`drug_ref_min.csv`**. So the chain is entirely identifier-based:

```
GTIN  â†”  NCPR SOAP  â†”  national id (col Z)  â†”  reg_number (col B)  â†”  drug_ref â†’ pfid
```

Reach: **11,740 `drug_ref` rows** (34% of the register) sit under those
registrations. `reg_number` â†’ pfid is 1-to-many for 74% of groups, so pack
selection still applies â€” but level 1 of the two-level match becomes *exact*
instead of fuzzy, and Annex 4 col C plus the service's `finalPack` give
structured input for level 2.

## Layout

```
ncpr_collector/
â”œâ”€â”€ app/
â”‚   â”œâ”€â”€ config.py        policy, all env-overridable
â”‚   â”œâ”€â”€ store.py         SQLite: queue, results, request log, daily counter
â”‚   â”œâ”€â”€ soap.py          SOAP 1.1 client + namespace-agnostic parser
â”‚   â”œâ”€â”€ queue_build.py   Annex 4 â†’ prioritised work queue
â”‚   â”œâ”€â”€ collect.py       the loop: one worker, long delays, hard stops
â”‚   â””â”€â”€ main.py          doctor / status / collect / export
â”œâ”€â”€ Dockerfile
â”œâ”€â”€ docker-compose.yml
â””â”€â”€ .env.example
```

## Run

```bash
cp .env.example .env && ${EDITOR:-nano} .env
```

```bash
docker compose build && docker compose run --rm ncpr-collector python -m app.main doctor
```

`doctor` first, always. The most likely failure on this service is running
from the wrong egress address â€” **an SSH session alone does not make requests
originate from the whitelisted host** (Â§3). `doctor` prints the actual egress
IP; compare it with the address registered with NCPR.

```bash
docker compose run --rm ncpr-collector python -m app.queue_build --annex /archive/input/Prilogenie-4-02-07-2026.xlsx --include-reverse
```

```bash
docker compose up -d ncpr-collector
```

```bash
docker compose run --rm ncpr-collector python -m app.main status
```

```bash
docker compose run --rm ncpr-collector python -m app.main export
```

## Queue priority â€” why it is not sequential

At ~80 calls/day a full sweep takes about **43 days**, so the *order* decides
when useful answers arrive. Sequential-by-id delivers the most decisive rows
last.

| band | count | what it buys |
|---|---|---|
| **10** reverse lookups of salvia's **reconstructed** GTINs | 279 | Per-row proof for barcodes we *derived* rather than read. The method has aggregate support (137/279 corroborated, **0/279** under a deliberately wrong check digit) but no individual confirmation. An authoritative reverse lookup is the only thing that can settle them â€” and it unblocks the 32 currently withheld from the map. |
| **20** forwards whose `reg_number` maps to exactly one `drug_ref` row | 575 | Zero pack ambiguity by construction â€” each answer is an immediately usable GTIN â†’ pfid link with no selection logic. |
| **30** forwards for `reg_number`s our own matchers disagree on | via `--contested` | Resolves an existing review row instead of adding a new one. |
| **40** remaining active PLS rows | 2,560 | The bulk sweep. |

Only rows with status `ÐÐºÑ‚Ð¸Ð²ÐµÐ½` are queued: the workbook carries historical
rows (32,449 total, 3,135 active) and only active ones should drive the
initial enrichment (Â§8).

## Safety properties

Each prevents a specific, real failure â€” not defensive decoration.

| property | failure it prevents |
|---|---|
| daily cap in SQLite, keyed by calendar date | an in-memory counter resets on `docker restart` and silently blows the cap |
| cap consumed **before** the request is sent | a crash mid-request would otherwise go uncounted |
| 403/429 write a `HALTED` file; startup refuses while it exists | automatic resumption against a withdrawn allowlist |
| `restart: "no"` in compose | a restart policy would fight the halt mechanism |
| single-instance lock file | two containers doubling the effective rate |
| raw XML archived **before** parsing | interrupted runs stay auditable (Â§11) |
| WSDL fetched once, hash stored | re-fetching definitions per product (Â§9); a changed hash flags a moved contract |
| GTIN stored as `TEXT`, export fully quoted | `05712249101367` â†’ `5.71225E+12`. This project has been bitten three times already (HANDOVER Â§11) |
| interruptible sleep | `SIGTERM` not waiting out a 10-minute delay |

Stop/retry rules follow Â§10 exactly: 403 stop immediately Â· 429 honour
`Retry-After`, else stop â‰¥24 h Â· 5xx wait 30 min, then 2 h, then stop for the
day Â· timeout retry once after 15 min Â· SOAP fault or empty GTIN list record
and continue.

An **empty GTIN list is not an error**. SESPA holds GTINs only for Positive
Drug List products (Â§1), so a valid non-PLS package legitimately returns none.
It is recorded as `no_gtin` and never retried.

## TLS

The runbook's successful test used `curl -k` because the host could not build
the Let's Encrypt issuer chain (Â§13) â€” a host CA problem, not a server one.
The image installs and refreshes `ca-certificates` so verification stays on.
`NCPR_INSECURE_TLS=1` exists as a last resort and logs a warning every run.

## Before the first bulk run

The runbook's own closing recommendation, restated because it is the one
thing this code cannot do for you: **request written rate guidance or an
approved collection window from NCPR.** A 43-day unattended process against a
monitored, allowlisted government service is exactly the case where a short
email beats an inferred policy. The defaults here are a conservative guess,
not an agreement.

