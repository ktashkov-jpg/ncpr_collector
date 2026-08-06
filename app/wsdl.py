# -*- coding: utf-8 -*-
"""Enumerate everything the service advertises.

The runbook documents two operations (§2.5), both of which take a single
identifier. That does not mean the service only has two: the published WSDL
is a small stub that imports the real definitions, and the service is named
MedicinalProductsRegister*s*Service.

Worth knowing before committing to 3,414 individual calls: if a
list/search/export operation exists, the whole collection strategy changes.

Fetches WSDL and XSD only. These are metadata, not product operations, so
nothing here consumes the daily cap or touches the collection queue.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from app import soap
from app.config import Config

WSDL_NS = "{http://schemas.xmlsoap.org/wsdl/}"
XSD_NS = "{http://www.w3.org/2001/XMLSchema}"


def fetch(opener, url: str, timeout: int) -> bytes:
    with opener.open(urllib.request.Request(url), timeout=timeout) as response:
        return response.read()


def imports_of(body: bytes, base: str) -> list[str]:
    """Both wsdl:import/@location and xsd:import|include/@schemaLocation."""
    out: list[str] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    for node in root.iter():
        for attr in ("location", "schemaLocation"):
            value = node.get(attr)
            if value:
                out.append(urllib.parse.urljoin(base, value))
    return list(dict.fromkeys(out))


def main() -> int:
    config = Config()
    opener = soap.make_opener(config.insecure_tls)
    root_url = config.endpoint + "?wsdl"

    seen: dict[str, bytes] = {}
    queue = [root_url]
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        try:
            body = fetch(opener, url, config.timeout_s)
        except Exception as exc:                    # noqa: BLE001 - diagnostic
            print(f"  ! could not fetch {url}: {type(exc).__name__}: {exc}")
            seen[url] = b""
            continue
        seen[url] = body
        print(f"fetched {url}  ({len(body)} bytes, sha256={soap.sha256(body)[:12]}…)")
        queue += imports_of(body, url)

    operations: list[tuple[str, str, str]] = []
    elements: dict[str, list[str]] = {}

    for url, body in seen.items():
        if not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue

        for port_type in root.iter(f"{WSDL_NS}portType"):
            for operation in port_type.findall(f"{WSDL_NS}operation"):
                name = operation.get("name", "")
                inp = operation.find(f"{WSDL_NS}input")
                out = operation.find(f"{WSDL_NS}output")
                operations.append((
                    name,
                    (inp.get("message", "") if inp is not None else "").split(":")[-1],
                    (out.get("message", "") if out is not None else "").split(":")[-1],
                ))

        # Top-level schema elements name the request/response wrappers, and
        # their child element names are the actual arguments.
        for schema in root.iter(f"{XSD_NS}schema"):
            for element in schema.findall(f"{XSD_NS}element"):
                name = element.get("name")
                if not name:
                    continue
                children = [e.get("name") for e in element.iter(f"{XSD_NS}element")
                            if e.get("name") and e.get("name") != name]
                elements[name] = children

    print(f"\n{'=' * 68}\nOPERATIONS ({len(operations)})\n{'=' * 68}")
    if not operations:
        print("  none found in a portType — check the imported WSDL manually")
    for name, msg_in, msg_out in sorted(operations):
        args = elements.get(name)
        arg_text = ", ".join(a for a in (args or [])[:6]) or "(no named arguments)"
        print(f"  {name}")
        print(f"      in={msg_in or '-'}  out={msg_out or '-'}")
        print(f"      args: {arg_text}")

    print(f"\n{'=' * 68}\nBULK-LOOKING OPERATIONS\n{'=' * 68}")
    bulk = [n for n, _, _ in operations
            if re.search(r"all|list|search|export|find|query|register",
                         n, re.I)]
    if bulk:
        print("  Candidates worth reading the spec for:")
        for name in sorted(set(bulk)):
            print(f"    {name}  args: "
                  f"{', '.join(elements.get(name, []) or ['(none)'])}")
        print("\n  An operation taking no product identifier, or a date/range,\n"
              "  would replace the 3,414-call plan entirely.")
    else:
        print("  None. Every operation appears to take a single identifier,\n"
              "  so a bulk pull is not available through this service and an\n"
              "  export would have to come from NCPR directly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
