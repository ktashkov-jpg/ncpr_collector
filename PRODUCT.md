# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The sole user is a pharmacist operating the application on a private remote
deployment whose outbound address is allowlisted by NCPR. The primary job is
to identify a medicinal product from the local catalogue, request its official
SESPA GTIN, verify the returned package, and retain the confirmed mapping.

## Product Purpose

Provide a controlled interface over the NCPR catalogue and SOAP service so a
pharmacist can resolve individual products without starting the full catalogue
scrape. Success means the correct package can be found locally, queried once,
reviewed with its provenance visible, and exported without losing GTIN leading
zeroes.

## Positioning

Local catalogue discovery is separated from the authoritative network lookup:
browsing and filtering consume no NCPR quota, while an explicit product choice
triggers one audited SOAP request for that national identifier.

## Operating Context

- The application runs remotely behind the public IP allowlisted by NCPR.
- The normal workflow is local catalogue search, one explicit SOAP lookup,
  pharmacist review, then staged export.
- Search uses pharmacist-facing identifiers: ATC, INN, trade name, and national
  identifier.
- A separate, guarded **Launch full scrape** action exposes the existing bulk
  collector. It must not bypass the collector's confirmation, rate, halt,
  locking, or audit safeguards.
- Confirmed rows are accumulated and flushed in batches of 10.

## Capabilities and Constraints

- Populate ATC, INN, trade name, and national identifier from the local
  catalogue before making a network request.
- Send the selected national identifier through the audited SOAP request path.
- Show each result immediately in a review table below the search surface.
- The existing persisted/exported result fields are sufficient: national
  identifier, Bulgarian/English names, authorization number, final pack, and
  GTIN fields.
- Preserve `gtin_raw` exactly as returned, including leading zeroes; checksum
  validity is evidence and must never silently rewrite the source value.
- SQLite, request counters, locks, and the `HALTED` marker remain on a local
  filesystem and never on CIFS/SMB.
- CSV and other append-only exports may be published to the SMB-backed archive
  share.
- The scraper remains unlaunched until the operator receives confirmation or
  written rate guidance from NCPR.
- The web framework and authentication boundary are open implementation
  decisions. The interface is private and must not turn the allowlisted host
  into a public SOAP proxy.

## Evidence on Hand

- `README.md` and `docs/OPERATIONS.md` define the collector's safety policy,
  storage split, queue, export, and halt behavior.
- `app/store.py` contains the catalogue and normalized SOAP result schemas.
- `app/probe.py` provides the existing audited one-product request path.
- `app/collect.py` provides the guarded full-scrape path.
- The verified NCPR/SESPA runbook is retained outside this repository under
  `C:\Users\kosi_\Documents\Codex\2026-08-04\i\outputs`.
- No launch approval or written bulk-request policy is currently on hand.

## Product Principles

1. Search locally before spending a network request.
2. Make every SOAP call explicit, attributable, and auditable.
3. Put package identity and source evidence ahead of visual decoration.
4. Require human review before a mapping becomes exportable.
5. Preserve the collector's rate and halt safeguards across every interface.

## Accessibility & Inclusion

The desktop-first interface must remain keyboard-operable, use explicit field
labels and visible focus states, and never communicate request, checksum, or
export status through color alone. Bulgarian product text and long authorization
values must wrap without truncating critical identifiers.
