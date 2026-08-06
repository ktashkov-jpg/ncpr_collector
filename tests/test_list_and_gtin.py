# -*- coding: utf-8 -*-
"""listMedicinalProducts parsing, and GTIN facts learned from live answers.

The three manual lookups on 2026-08-06 produced two findings that constrain
how this collector must treat its own output:

  * SESPA publishes GTIN-14s whose leading digit is an INDICATOR. natid 1670
    returned 55413760279461 -- indicator 5, a higher packaging level, not the
    consumer unit. A scanned bottle will not carry that number.
  * An official source can publish an invalid check digit. natid 758 returned
    50085412959961, which fails mod-10.

Both mean the collector must record what it received and flag it, never
silently drop or "correct" it.
"""
from app import soap

LIST_RESPONSE = b"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
 <soap:Body>
  <ns2:listMedicinalProductsResponse
      xmlns:ns2="http://webservice.portal.ncprmp.sirma.com">
   <return>
    <medicinalProductListItemList>
     <registerMedicamentId>98765</registerMedicamentId>
     <medicinalProductIdentifier>1670</medicinalProductIdentifier>
     <registerCode>PDL_APPENDIX_4</registerCode>
     <registerName>Appendix 4</registerName>
     <nameBG>Glucose BAXTER</nameBG>
     <nameEN>Glucose BAXTER</nameEN>
     <inn>Glucose</inn>
     <atcCodes>B05BA03</atcCodes>
     <atcCodes>V07AB</atcCodes>
     <authorizationHolder>Baxter Holding B.V.</authorizationHolder>
     <producer>Baxter</producer>
     <medicamentForm>Solution for infusion</medicamentForm>
     <quantity>5%  - 500 ml</quantity>
     <medicamentUnit>-</medicamentUnit>
     <finalPack>20</finalPack>
     <publishedAt>2026-07-02T00:00:00+03:00</publishedAt>
    </medicinalProductListItemList>
    <allResultsCount>3135</allResultsCount>
   </return>
  </ns2:listMedicinalProductsResponse>
 </soap:Body>
</soap:Envelope>"""


def test_list_envelope_shape():
    envelope = soap.build_list_envelope("ns", "PDL_APPENDIX_4", 0, 200).decode()
    assert "<web:listMedicinalProducts>" in envelope
    assert "<registerCode>PDL_APPENDIX_4</registerCode>" in envelope
    assert "<fromRow>0</fromRow>" in envelope
    assert "<numberOfRows>200</numberOfRows>" in envelope
    # Required in the XSD (no minOccurs), so sent empty rather than omitted.
    assert "<medicinalProductName></medicinalProductName>" in envelope
    assert "<innCode></innCode>" in envelope


def test_list_envelope_without_register_searches_all():
    envelope = soap.build_list_envelope("ns", None, 40, 10).decode()
    assert "registerCode" not in envelope
    assert "<fromRow>40</fromRow>" in envelope


def test_parse_list_items_and_total():
    parsed = soap.parse_list(LIST_RESPONSE)
    assert parsed["all_results_count"] == 3135
    assert len(parsed["items"]) == 1
    item = parsed["items"][0]
    assert item["medicinalProductIdentifier"] == "1670"
    assert item["finalPack"] == "20"
    assert item["medicamentForm"] == "Solution for infusion"
    # Repeated atcCodes collapse to one pipe-joined field.
    assert item["atcCodes"] == "B05BA03|V07AB"
    assert "fault" not in parsed


def test_list_item_has_no_gtin():
    """The reason per-product calls remain necessary: the list item type has
    no gtins field, so enumeration cannot replace collection."""
    assert "gtin" not in soap.parse_list(LIST_RESPONSE)["items"][0]


def test_case_level_gtin14_is_valid_but_not_a_consumer_barcode():
    gtin = "55413760279461"          # natid 1670, live answer
    assert soap.valid_gtin(gtin)
    assert gtin[0] not in "09"        # indicator 1-8 = higher packaging level
    assert soap.ean13_from(gtin) == ""  # no GTIN-13 to derive


def test_official_source_can_publish_a_bad_check_digit():
    """natid 758 returned this. It must be stored and flagged, not dropped."""
    assert not soap.valid_gtin("50085412959961")


def test_zero_padded_gtin14_yields_the_consumer_barcode():
    gtin = "03800163730014"          # natid 69, live answer
    assert soap.valid_gtin(gtin)
    assert soap.ean13_from(gtin) == "3800163730014"
    assert soap.valid_gtin("3800163730014")
