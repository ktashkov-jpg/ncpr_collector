# -*- coding: utf-8 -*-
"""Offline checks against the runbook's verified §7 response.

No network. These exist so a fresh clone can be trusted before the first live
call is made from the whitelisted host.
"""
from app import soap

# Shape mirrors the runbook's verified round trip: national id 15955 ->
# Xultophy, EU/1/14/947/002, 3 pre-filled pens, GTIN 05712249101367.
RESPONSE = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body>
  <ns2:getMedicinalProductDataWithGTINResponse
      xmlns:ns2="http://webservice.portal.ncprmp.sirma.com">
   <return>
    <medicinalProductIdentifier>15955</medicinalProductIdentifier>
    <nameBG>Xultophy</nameBG>
    <nameEN>Xultophy</nameEN>
    <authorizationNumber>EU/1/14/947/002</authorizationNumber>
    <finalPack>3 pre-filled pens</finalPack>
    <gtins>05712249101367</gtins>
   </return>
  </ns2:getMedicinalProductDataWithGTINResponse>
 </soap:Body>
</soap:Envelope>"""

FAULT = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body><soap:Fault>
  <faultcode>soap:Server</faultcode>
  <faultstring>No product found</faultstring>
 </soap:Fault></soap:Body></soap:Envelope>"""

NO_GTIN = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body><ns2:resp xmlns:ns2="x"><return>
   <medicinalProductIdentifier>99999</medicinalProductIdentifier>
   <nameBG>Non-PLS product</nameBG>
 </return></ns2:resp></soap:Body></soap:Envelope>"""


def test_envelope_matches_runbook():
    envelope = soap.build_envelope(
        "http://webservice.portal.ncprmp.sirma.com", soap.FORWARD, "15955").decode()
    assert "<web:getMedicinalProductDataWithGTIN>" in envelope
    assert "<medicinalProductIdentifier>15955</medicinalProductIdentifier>" in envelope
    assert 'xmlns:web="http://webservice.portal.ncprmp.sirma.com"' in envelope


def test_reverse_envelope_uses_product_code():
    envelope = soap.build_envelope("ns", soap.REVERSE, "05712249101367").decode()
    assert "<product_code>05712249101367</product_code>" in envelope


def test_parses_verified_record():
    parsed = soap.parse(RESPONSE)
    assert parsed["medicinal_product_identifier"] == "15955"
    assert parsed["name_bg"] == "Xultophy"
    assert parsed["authorization_number"] == "EU/1/14/947/002"
    assert parsed["final_pack"] == "3 pre-filled pens"
    assert parsed["gtins"] == ["05712249101367"]
    assert "fault" not in parsed


def test_leading_zero_survives():
    """The whole point of storing GTINs as text (runbook §7)."""
    gtin = "05712249101367"
    assert soap.gtin14(gtin) == "05712249101367"
    assert soap.gtin14(gtin).startswith("0")
    assert soap.ean13_from(gtin) == "5712249101367"
    assert soap.valid_gtin(gtin)
    assert soap.valid_gtin(soap.ean13_from(gtin))


def test_fault_prefers_faultstring():
    """faultcode is always 'soap:Server'; faultstring carries the reason."""
    assert soap.parse(FAULT)["fault"] == "No product found"


def test_missing_gtin_is_not_a_fault():
    """Normal for non-PLS packages -- SESPA holds GTINs only for the
    Positive Drug List (runbook §1). Must not look like an error."""
    parsed = soap.parse(NO_GTIN)
    assert parsed["gtins"] == []
    assert "fault" not in parsed
    assert parsed["medicinal_product_identifier"] == "99999"


def test_rejects_non_gtin_digit_strings():
    body = RESPONSE.replace(b"05712249101367", b"123")
    assert soap.parse(body)["gtins"] == []
