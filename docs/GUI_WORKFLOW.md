# Pharmacist GUI workflow

This document defines the intended operator workflow for a future private web
interface. It is a product and safety contract, not authorization to start the
collector or contact NCPR.

## Design philosophy

The GUI is a sleek, minimal pharmacist's research instrument. It is an
**Operate** surface: speed, package identity, provenance, and unambiguous state
take priority over dashboards, decoration, or aggregate metrics.

- Desktop-first, compact, and calm without becoming visually sterile.
- One dominant task per screen: find a product, request its GTIN, review it.
- Show only pharmacist-relevant search facts: ATC, INN, trade name, and national
  identifier.
- Keep network state explicit: local result, awaiting confirmation, requesting,
  resolved, no GTIN, fault, halted, or staged for export.
- Use one restrained accent for focus and successful confirmation. Warnings,
  invalid checksums, and hard stops need text and icon labels as well as color.
- Avoid nested cards, decorative charts, gamification, and automatic requests
  triggered merely by typing or changing a selection.

The final `DESIGN.md` must be generated from the implemented and visually
verified interface. It is deliberately ignored by Git for this project; this
workflow document is the committed product-level design contract.

## Primary workflow: one product

1. **Search locally.** The pharmacist enters an ATC code, INN, trade name, or
   national identifier. Search and filtering use the local `catalogue` table and
   consume no NCPR request allowance.
2. **Select a catalogue row.** Selection populates a compact product summary
   with ATC, INN, trade name, and national identifier. No SOAP call occurs yet.
3. **Confirm the package.** The pharmacist explicitly chooses **Request GTIN**.
   The action displays which national identifier will be sent and requires an
   affirmative click.
4. **Request once.** The backend uses the same audited path and safeguards as
   `app.probe`: daily-cap accounting, raw response archive, request log, timeout
   handling, and hard-stop behavior.
5. **Review underneath.** The normalized response is inserted immediately into
   a results table below the search area. The table shows national identifier,
   product name, authorization number, final pack, and GTIN. Checksum anomalies
   remain visibly flagged without modifying `gtin_raw`.
6. **Accept or reject.** A row does not enter the export batch until the
   pharmacist confirms it represents the intended product/package. Rejected
   rows remain auditable but are not exported.
7. **Flush ten confirmed rows.** Confirmed rows are staged locally. At 10 rows,
   write the batch transactionally to SQLite first, then replace or append the
   quoted CSV on the SMB-backed archive share using a temporary file and atomic
   rename where the mounted filesystem supports it. A manual **Export now**
   action flushes a smaller final batch.

## Search and result layout

The first viewport should contain:

- A single search input with an explicit selector for ATC, INN, trade name, or
  national identifier.
- A keyboard-navigable local results list with the four catalogue fields.
- A selected-product summary and one primary **Request GTIN** action.
- A narrow operational status strip showing requests used today, the daily cap,
  and any `HALTED` condition.
- The review table beginning directly underneath, without a dashboard layer in
  between.

Multiple local matches must remain separate rows. The interface must not infer a
package from a fuzzy name match or make a SOAP request for the highlighted row
without explicit confirmation.

## Prepare the local catalogue

The GUI reads `local_catalogue` from the same local SQLite database as the
collector. Building it is an offline reconciliation step and does not call the
SESPA product operations.

Required sources:

- `sources/ncpr/clean/ncpr_all_reimb_clean.csv` for the complete reimbursed
  catalogue; only rows marked currently active are loaded into the operator UI;
- `sources/ncpr/clean/ncpr_annex_clean.csv` to rank active Appendix 1 products
  ahead of the rest of the catalogue;
- optionally, a current PimChecker PostgreSQL backup for additional ATC
  coverage outside Appendix 1.

Run a strict check before replacing the catalogue:

```bash
python -m app.catalogue_build \
  --catalogue-csv /archive/input/ncpr_all_reimb_clean.csv \
  --priority-appendix /archive/input/ncpr_annex_clean.csv \
  --check-only
```

Remove `--check-only` to write the database. The default is deliberately
strict: any missing National ID, registration number, trade name, INN, or ATC
prevents replacement of `local_catalogue`. `--allow-incomplete` exists for
diagnostics only and must not be used to declare the GUI ready.

ATC precedence is exact National ID from the official Appendix extracts, then
exact National ID from PimChecker, then a PimChecker INN mapping only when that
INN resolves to exactly one ATC code. Ambiguous values are recorded as issues
and never guessed.

## Full scrape control

The GUI includes **Launch full scrape**, visually separated from individual
lookup actions. It is an operational control, not the primary call to action.

The implemented controls are labelled **Start bulk export** and **Stop bulk
export**. Start launches the existing collector process only after displaying
and reconfirming the current pending count. Stop writes a cooperative marker;
the collector finishes any in-flight HTTP call, interrupts its wait, and exits.
Queue rows, completed results, the daily counter, and the last bulk-run state
remain in SQLite, so a later start resumes pending work rather than rebuilding
or replaying completed work.

**Export all collected CSV** is separate and read-only: it downloads every
stored normalized product row through the browser without changing review or
queue state.

Before launch, show a confirmation dialog containing:

- pending task count and estimated duration at the configured daily cap;
- requests already consumed today and the operating window;
- current egress/allowlist readiness from the doctor check;
- local database and archive locations;
- whether a `HALTED` marker or collector lock exists;
- a statement that written NCPR launch/rate confirmation has been received,
  represented by an explicit operator checkbox.

The button must remain disabled when doctor checks fail, `HALTED` exists, another
collector owns the lock, or the confirmation checkbox is not selected. Launch
must call the existing collector rather than reimplementing its retry or rate
logic. The GUI must provide status and a graceful stop control, but must never
automatically clear `HALTED` or restart after a 403/429.

## Persistence boundary

- **Local filesystem:** SQLite database, daily request counter, locks, staged
  confirmations, and `HALTED` marker.
- **SMB/archive share:** raw SOAP responses and quoted CSV exports.
- GTIN columns are always text. CSV output remains UTF-8 with every field quoted.
- A failed SMB publication must leave the SQLite commit and staged batch intact
  so publication can be retried without repeating SOAP calls.

## Out of scope for the first GUI

- Public or multi-user access.
- Editing catalogue facts returned from the source data.
- Automatic SOAP requests while typing, hovering, or browsing local results.
- Silent correction of invalid GTIN check digits.
- Automatic clearing of safety stops or unattended restart after hard failures.

## Run the lightweight interface

The implemented interface is Flask with server-rendered HTML and dependency-free
JavaScript; it has no Node, PHP, Laravel, or frontend build step. Start it on the
allowlisted host with:

```bash
docker compose up -d ncpr-web
```

Gunicorn binds to `127.0.0.1:6001` so the deployment's existing reverse proxy
and IP access policy remain the public boundary. Set a stable random
`NCPR_WEB_SECRET` in `.env` before deployment. `NCPR_BULK_APPROVED` remains `0`:
the full-scrape control is shown but locked, and changing this configuration is
not by itself a launch command.
