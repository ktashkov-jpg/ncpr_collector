# -*- coding: utf-8 -*-
"""SOAP 1.1 client for the NCPR MedicinalProductsRegistersService.

Two operations (runbook §5, §6):
  getMedicinalProductDataWithGTIN(medicinalProductIdentifier)  national id -> GTIN
  getMedicinalProductDataByGTIN(product_code)                  GTIN -> national id

Deliberately stdlib-only, matching the rest of this project. The response is
parsed namespace-agnostically by local tag name: the runbook records the
operation namespace but not the full response schema, and a hard-coded
namespace would break silently against a service revision.
"""
from __future__ import annotations

import hashlib
import re
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

FORWARD = "getMedicinalProductDataWithGTIN"
REVERSE = "getMedicinalProductDataByGTIN"
LIST = "listMedicinalProducts"

# RegisterCode enumeration from the XSD. The GTIN scope is "ЛП включени в
# ПЛС" -- the whole Positive Drug List -- so restricting enumeration to
# PDL_APPENDIX_4 (the Annex 4 workbook) may undercount the universe.
REGISTER_CODES = ("PDL_APPENDIX_1", "PDL_APPENDIX_2", "PDL_APPENDIX_3",
                  "PDL_APPENDIX_4", "CEILING_PRICES", "MAX_PRICES")

LIST_ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:web="{ns}">
  <soapenv:Header/>
  <soapenv:Body>
    <web:listMedicinalProducts>
      <filter>
{register}        <medicinalProductName></medicinalProductName>
        <innCode></innCode>
      </filter>
      <fromRow>{from_row}</fromRow>
      <numberOfRows>{rows}</numberOfRows>
    </web:listMedicinalProducts>
  </soapenv:Body>
</soapenv:Envelope>"""

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:web="{ns}">
  <soapenv:Header/>
  <soapenv:Body>
    <web:{op}>
      <{arg}>{value}</{arg}>
    </web:{op}>
  </soapenv:Body>
</soapenv:Envelope>"""


class HardStop(Exception):
    """Raised for statuses the runbook says must stop the run (§10)."""


class Transient(Exception):
    """Raised for failures that should be retried on a later schedule."""


def build_envelope(namespace: str, operation: str, value: str) -> bytes:
    arg = ("medicinalProductIdentifier" if operation == FORWARD else "product_code")
    body = ENVELOPE.format(ns=namespace, op=operation, arg=arg,
                           value=_escape(value))
    return body.encode("utf-8")


def build_list_envelope(namespace: str, register_code: str | None,
                        from_row: int, rows: int) -> bytes:
    """listMedicinalProducts(filter, fromRow, numberOfRows).

    `medicinalProductName` and `innCode` have no minOccurs in the XSD, so
    they are sent empty rather than omitted. `registerCode` is nillable and
    is left out entirely when not filtering, which searches every register.
    Child elements are unqualified: the schema sets no elementFormDefault,
    matching the runbook's verified call shape.
    """
    register = ""
    if register_code:
        register = f"        <registerCode>{_escape(register_code)}</registerCode>\n"
    body = LIST_ENVELOPE.format(ns=namespace, register=register,
                                from_row=int(from_row), rows=int(rows))
    return body.encode("utf-8")


def parse_list(body: bytes) -> dict:
    """{'items': [...], 'all_results_count': int, 'fault': str|None}.

    `allResultsCount` is the total matching the filter, not the page size --
    it is what makes paging terminable without guessing.
    """
    root = ET.fromstring(body)
    out: dict = {"items": [], "all_results_count": None}

    for node in root.iter():
        name = local(node.tag)
        text = (node.text or "").strip()
        if name.lower() == "faultstring" and text:
            out["fault"] = text
        elif name.lower() == "faultcode" and text:
            out.setdefault("fault", text)
        elif name == "allResultsCount" and text.isdigit():
            out["all_results_count"] = int(text)

    # The XSD names the repeating ELEMENT `medicinalProductListItemList`
    # while its TYPE is `medicinalProductListItem`. Matching the type name
    # finds nothing; both are accepted here so a future rename cannot
    # silently return an empty page that looks like the end of the results.
    for item in root.iter():
        if local(item.tag) not in ("medicinalProductListItemList",
                                   "medicinalProductListItem"):
            continue
        record: dict = {}
        atc: list[str] = []
        for child in item:
            key, value = local(child.tag), (child.text or "").strip()
            if key == "atcCodes":
                if value:
                    atc.append(value)
            else:
                record[key] = value
        record["atcCodes"] = "|".join(atc)
        out["items"].append(record)
    return out


def _escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def make_opener(insecure: bool) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        # Python 3.13 strict X.509 rejects some otherwise-valid chains; the
        # rest of this project hits the same wall (HANDOVER §12).
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def call(opener, endpoint: str, namespace: str, operation: str,
         value: str, timeout: int) -> tuple[int, bytes, int]:
    """Returns (http_status, body, elapsed_ms). Raises HardStop / Transient."""
    payload = build_envelope(namespace, operation, value)
    request = urllib.request.Request(endpoint, data=payload, method="POST")
    request.add_header("Content-Type", "text/xml; charset=UTF-8")
    request.add_header("SOAPAction", '""')          # runbook: empty SOAPAction

    started = time.time()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            elapsed = int((time.time() - started) * 1000)
            ctype = (response.headers.get("Content-Type") or "").lower()
            # Runbook §10: an HTML body means the request fell through to the
            # JSF portal instead of the SOAP service. Never treat that as data.
            if "html" in ctype or body.lstrip()[:15].lower().startswith(b"<!doctype"):
                raise HardStop("HTML response - request reached the portal, "
                               "not the SOAP endpoint")
            return response.status, body, elapsed
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise HardStop("HTTP 403 - allowlist status must be confirmed "
                           "with NCPR before any retry") from exc
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After")
            raise HardStop(f"HTTP 429 - rate limited (Retry-After={retry_after})") from exc
        if 500 <= exc.code <= 599:
            raise Transient(f"HTTP {exc.code}") from exc
        raise HardStop(f"HTTP {exc.code}") from exc
    except (TimeoutError, OSError) as exc:
        raise Transient(f"{type(exc).__name__}: {exc}") from exc


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse(body: bytes) -> dict:
    """Namespace-agnostic extraction of the fields the runbook documents (§7).

    Returns {} plus 'fault' when the service reports a SOAP fault, and
    'gtins': [] when the product exists but has no GTIN -- which is normal,
    not an error: SESPA holds GTINs only for Positive Drug List products
    (runbook §1).
    """
    root = ET.fromstring(body)
    out: dict = {"gtins": []}

    for node in root.iter():
        name = local(node.tag)
        text = (node.text or "").strip()
        # faultstring carries the human-readable reason ("No product found");
        # faultcode is only ever "soap:Server". Prefer the former, but keep
        # the latter so a fault is never silently reported as a success.
        if name.lower() == "faultstring" and text:
            out["fault"] = text
        elif name.lower() == "faultcode" and text:
            out.setdefault("fault", text)
        if not text:
            continue
        if name == "medicinalProductIdentifier":
            out.setdefault("medicinal_product_identifier", text)
        elif name in ("nameBG", "nameBg"):
            out.setdefault("name_bg", text)
        elif name in ("nameEN", "nameEn"):
            out.setdefault("name_en", text)
        elif name.lower() == "authorizationnumber":
            out.setdefault("authorization_number", text)
        elif name.lower() == "finalpack":
            out.setdefault("final_pack", text)
        elif name.lower() in ("gtin", "gtins", "product_code", "productcode"):
            if re.fullmatch(r"\d{8,14}", text) and text not in out["gtins"]:
                out["gtins"].append(text)
    return out


def gtin14(value: str) -> str:
    return value.zfill(14)


def ean13_from(value: str) -> str:
    """A GTIN-14 that is a zero-padded EAN-13 can be shown as 13 digits.
    Derivation only -- the raw value is what gets stored (runbook §7)."""
    padded = gtin14(value)
    return padded[1:] if padded.startswith("0") else ""


def valid_gtin(value: str) -> bool:
    if not value.isdigit() or len(value) not in (8, 12, 13, 14):
        return False
    total = 0
    for position, char in enumerate(reversed(value[:-1]), start=1):
        total += int(char) * (3 if position % 2 else 1)
    return (10 - total % 10) % 10 == int(value[-1])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
