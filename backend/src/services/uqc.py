"""Mapping the product master's free-text unit onto a GST unit quantity code (UQC)."""

import re

from src.core.eway_bill_codes import NIC_UQC_CODES

_UNIT_SEPARATORS = re.compile(r"[\s.]+")

# Table 12 of GSTR-1 and the e-way bill both take UQC from a fixed list, so the
# product master's free-text unit has to be mapped onto it. Anything unmapped falls
# back to OTH (Others), which is on GST's UQC list; NOS would claim the quantity
# is a count, which is wrong for a weight or a volume.
HSN_UQC_BY_UNIT = {
    "pieces": "NOS", "piece": "NOS", "pc": "PCS", "pcs": "PCS",
    "no": "NOS", "nos": "NOS", "number": "NOS", "numbers": "NOS",
    "unit": "UNT", "units": "UNT",
    "kg": "KGS", "kgs": "KGS", "kilogram": "KGS", "kilograms": "KGS",
    "kilo": "KGS", "kilos": "KGS",
    "g": "GMS", "gm": "GMS", "gms": "GMS", "gram": "GMS", "grams": "GMS",
    "m": "MTR", "metre": "MTR", "meter": "MTR", "mtr": "MTR",
    "metres": "MTR", "meters": "MTR", "mtrs": "MTR",
    "cm": "CMS", "centimetre": "CMS", "centimeter": "CMS",
    "centimetres": "CMS", "centimeters": "CMS",
    "l": "LTR", "litre": "LTR", "liter": "LTR", "ltr": "LTR",
    "litres": "LTR", "liters": "LTR", "ltrs": "LTR",
    "ml": "MLT", "millilitre": "MLT", "milliliter": "MLT",
    "millilitres": "MLT", "milliliters": "MLT",
    "sq ft": "SQF", "sqft": "SQF", "square foot": "SQF", "square feet": "SQF",
    "sq m": "SQM", "sqm": "SQM", "square metre": "SQM", "square meter": "SQM",
    "square metres": "SQM", "square meters": "SQM",
    "box": "BOX", "boxes": "BOX", "set": "SET", "sets": "SET",
    "pair": "PRS", "pairs": "PRS", "dozen": "DOZ", "dozens": "DOZ",
    "ton": "TON", "tonne": "TON", "tonnes": "TON",
    "bag": "BAG", "bags": "BAG", "bottle": "BTL", "bottles": "BTL",
    "pack": "PAC", "packs": "PAC", "packet": "PAC", "packets": "PAC",
    "pkt": "PAC", "pkts": "PAC",
    "quintal": "QTL", "quintals": "QTL",
    "carton": "CTN", "cartons": "CTN", "roll": "ROL", "rolls": "ROL",
    "bundle": "BDL", "bundles": "BDL",
}


def _normalize_unit(unit: str | None) -> str:
    """Lowercase, collapse whitespace and dots, and strip surrounding spaces/dots."""
    return _UNIT_SEPARATORS.sub(" ", (unit or "").lower()).strip()


def uqc_for_unit(unit: str | None) -> str:
    """The UQC for a product's free-text unit.

    A unit that already is a valid UQC code (BAG, CTN, QTL) is used as is.
    Anything else unmapped falls back to OTH.
    """
    key = _normalize_unit(unit)
    if key in HSN_UQC_BY_UNIT:
        return HSN_UQC_BY_UNIT[key]
    code = key.upper()
    if code in NIC_UQC_CODES:
        return code
    return "OTH"


def hsn_uqc(hsn: str, unit: str | None) -> str:
    """The UQC to declare for an HSN line of GSTR-1 Table 12."""
    # Services carry no quantity, so the GSTN convention is UQC "NA" with qty 0.
    if hsn.startswith("99"):
        return "NA"
    return uqc_for_unit(unit)
