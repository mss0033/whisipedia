"""Geospatial metadata XML read into sections a person can read (2026-11-05, the gNATSGO archive).

Data archives carry their description as XML: ISO 19115-3 (``mdb:MD_Metadata``, gNATSGO's
``ssurgo_iso19115_3version*.xml``), ISO 19139 (``gmd:MD_Metadata``), a stand-alone ISO 19110 feature
catalogue (``gfc:FC_FeatureCatalogue``) or the US FGDC CSDGM (``<metadata><idinfo>…``). Shown raw, it is
seven megabytes of wrappers (``<cit:title><gco:CharacterString>``). :func:`read_metadata` keeps what a
reader wants:

* a title and abstract;
* one section per top-level element (identification, contacts, reference systems, data quality,
  lineage, distribution, ...), each a list of ``label: value`` fields. The ISO type wrappers
  (``CI_Citation``, ``CharacterString``) are left out of the labels; a code list value is shown as its
  code (``pointOfContact`` -> "point of contact"), not the long definition some writers put in its text;
  FGDC's abbreviated tags (``cntemail``) get their names;
* the data dictionary: every table (ISO feature type, FGDC entity) with its columns, their types,
  units and allowed values. gNATSGO's has 84,000 elements of this, the most useful part of the file.

Everything is bounded (fields per section, values per column, characters per value), so a huge file
yields a long but finite answer. Parsing uses expat via ElementTree, which refuses external entities
and (expat 2.4.1 and later) entity-expansion bombs; the caller bounds the bytes it hands in.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from wsp_api.schemas import (
    ArchiveMetadata,
    DictionaryAttribute,
    DictionaryEntity,
    MetadataField,
    MetadataSection,
)

MAX_FIELDS = 400  # per section
MAX_VALUE_CHARS = 6000  # an abstract or a lineage step; longer is cut with an ellipsis
MAX_ENTITIES = 2000
MAX_ATTRIBUTES = 1000  # per table
MAX_VALUES = 80  # allowed values listed per column
MAX_VALUE_DEFINITION = 300  # characters of each allowed value's definition
MAX_LABEL_PARTS = 3  # "Contact info > Address > City", joined by LABEL_JOIN
LABEL_JOIN = " \u203a "  # a single right-pointing angle quotation mark

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# ISO wrappers that say what type a value has, not what it is: left out of labels.
_ISO_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]*_[A-Za-z0-9]+$")  # CI_Citation, MD_Identifier, FC_ListedValue
_ISO_TYPES = frozenset(
    {
        "CharacterString",
        "Anchor",
        "LocalName",
        "ScopedName",
        "TypeName",
        "MemberName",
        "Date",
        "DateTime",
        "Integer",
        "UnlimitedInteger",
        "Decimal",
        "Real",
        "Boolean",
        "Measure",
        "Distance",
        "Length",
        "Angle",
        "Scale",
        "Record",
        "RecordType",
        "Multiplicity",
        "MultiplicityRange",
        "URL",
        "FileName",
        "MimeFileType",
        "Binary",
        "LanguageCode",
        "CountryCode",
        "PT_FreeText",
        "PT_Locale",
        "TimePeriod",
        "TimeInstant",
    }
)
# FGDC CSDGM's abbreviated element names (FGDC-STD-001-1998); None marks a wrapper left out of labels.
_FGDC: dict[str, str | None] = {
    "idinfo": "Identification",
    "citation": "Citation",
    "citeinfo": None,
    "cntinfo": None,
    "timeinfo": None,
    "origin": "Originator",
    "pubdate": "Publication date",
    "pubtime": "Publication time",
    "title": "Title",
    "edition": "Edition",
    "geoform": "Data form",
    "serinfo": "Series",
    "sername": "Series name",
    "issue": "Issue",
    "pubinfo": "Publication",
    "pubplace": "Place",
    "publish": "Publisher",
    "othercit": "Other citation details",
    "onlink": "Online link",
    "lworkcit": "Larger work",
    "descript": "Description",
    "abstract": "Abstract",
    "purpose": "Purpose",
    "supplinf": "Supplemental information",
    "timeperd": "Time period",
    "rngdates": "Date range",
    "begdate": "Begin date",
    "enddate": "End date",
    "sngdate": "Single date",
    "mdattim": "Dates",
    "caldate": "Date",
    "time": "Time",
    "current": "Currentness",
    "status": "Status",
    "progress": "Progress",
    "update": "Update frequency",
    "spdom": "Spatial domain",
    "bounding": "Bounding box",
    "westbc": "West",
    "eastbc": "East",
    "northbc": "North",
    "southbc": "South",
    "keywords": "Keywords",
    "theme": "Theme",
    "themekt": "Thesaurus",
    "themekey": "Keyword",
    "place": "Place",
    "placekt": "Thesaurus",
    "placekey": "Place",
    "temporal": "Temporal",
    "tempkt": "Thesaurus",
    "tempkey": "Keyword",
    "accconst": "Access constraints",
    "useconst": "Use constraints",
    "ptcontac": "Point of contact",
    "cntperp": "Person",
    "cntorgp": "Organization",
    "cntper": "Person",
    "cntorg": "Organization",
    "cntpos": "Position",
    "cntaddr": "Address",
    "addrtype": "Address type",
    "address": "Street",
    "city": "City",
    "state": "State",
    "postal": "Postal code",
    "country": "Country",
    "cntvoice": "Phone",
    "cntfax": "Fax",
    "cntemail": "Email",
    "hours": "Hours",
    "cntinst": "Instructions",
    "browse": "Browse graphic",
    "browsen": "File",
    "browsed": "Description",
    "browset": "Type",
    "datacred": "Credit",
    "secinfo": "Security",
    "native": "Native environment",
    "crossref": "Cross reference",
    "dataqual": "Data quality",
    "attracc": "Attribute accuracy",
    "attraccr": "Report",
    "logic": "Logical consistency",
    "complete": "Completeness",
    "posacc": "Positional accuracy",
    "horizpa": "Horizontal",
    "horizpar": "Report",
    "vertacc": "Vertical",
    "vertaccr": "Report",
    "lineage": "Lineage",
    "srcinfo": "Source",
    "srccite": "Citation",
    "srcscale": "Scale",
    "typesrc": "Media",
    "srctime": "Time",
    "srccurr": "Currentness",
    "srccitea": "Abbreviation",
    "srccontr": "Contribution",
    "procstep": "Process step",
    "procdesc": "Description",
    "procdate": "Date",
    "srcused": "Source used",
    "srcprod": "Source produced",
    "cloud": "Cloud cover",
    "spdoinfo": "Spatial data organization",
    "indspref": "Indirect reference",
    "direct": "Direct reference",
    "ptvctinf": "Point and vector",
    "sdtsterm": "SDTS terms",
    "sdtstype": "Type",
    "ptvctcnt": "Count",
    "rastinfo": "Raster",
    "rasttype": "Raster type",
    "rowcount": "Rows",
    "colcount": "Columns",
    "vrtcount": "Vertical count",
    "spref": "Spatial reference",
    "horizsys": "Horizontal system",
    "geograph": "Geographic",
    "latres": "Latitude resolution",
    "longres": "Longitude resolution",
    "geogunit": "Units",
    "planar": "Planar",
    "mapproj": "Map projection",
    "mapprojn": "Projection",
    "gridsys": "Grid system",
    "gridsysn": "Grid",
    "utm": "UTM",
    "utmzone": "Zone",
    "planci": "Coordinate information",
    "plance": "Encoding",
    "coordrep": "Coordinate representation",
    "absres": "Abscissa resolution",
    "ordres": "Ordinate resolution",
    "plandu": "Distance units",
    "geodetic": "Geodetic model",
    "horizdn": "Horizontal datum",
    "ellips": "Ellipsoid",
    "semiaxis": "Semi-major axis",
    "denflat": "Flattening ratio",
    "vertdef": "Vertical system",
    "altsys": "Altitude system",
    "altdatum": "Datum",
    "altres": "Resolution",
    "altunits": "Units",
    "altenc": "Encoding",
    "eainfo": "Entities and attributes",
    "overview": "Overview",
    "eaover": "Overview",
    "eadetcit": "Detail citation",
    "distinfo": "Distribution",
    "distrib": "Distributor",
    "resdesc": "Resource",
    "distliab": "Liability",
    "stdorder": "Standard order",
    "digform": "Digital form",
    "digtinfo": "Transfer",
    "formname": "Format",
    "formvern": "Format version",
    "formspec": "Format specification",
    "filedec": "Decompression",
    "transize": "Transfer size (MB)",
    "digtopt": "Options",
    "onlinopt": "Online",
    "computer": "Computer",
    "networka": "Network address",
    "networkr": "Address",
    "offoptn": "Offline",
    "offmedia": "Medium",
    "fees": "Fees",
    "ordering": "Ordering",
    "custom": "Custom order",
    "techpreq": "Technical prerequisites",
    "metainfo": "Metadata reference",
    "metd": "Metadata date",
    "metrd": "Review date",
    "metfrd": "Future review",
    "metc": "Metadata contact",
    "metstdn": "Standard",
    "metstdv": "Standard version",
    "mettc": "Time convention",
    "metac": "Access constraints",
    "metuc": "Use constraints",
    "metsi": "Security",
    "metextns": "Extensions",
}
# Friendly headings for ISO top-level elements; others are spelled out from their names.
_ISO_HEADINGS = {
    "identificationInfo": "Identification",
    "contact": "Metadata contact",
    "dateInfo": "Metadata dates",
    "dateStamp": "Metadata date",
    "metadataStandard": "Metadata standard",
    "defaultLocale": "Language",
    "otherLocale": "Other languages",
    "metadataScope": "Scope",
    "spatialRepresentationInfo": "Spatial representation",
    "referenceSystemInfo": "Reference systems",
    "contentInfo": "Content",
    "distributionInfo": "Distribution",
    "dataQualityInfo": "Data quality",
    "resourceLineage": "Lineage",
    "metadataMaintenance": "Metadata maintenance",
    "metadataConstraints": "Metadata constraints",
    "portrayalCatalogueInfo": "Portrayal catalogues",
    "applicationSchemaInfo": "Application schemas",
    "metadataExtensionInfo": "Metadata extensions",
}
_FIRST = ("identificationInfo", "idinfo")  # sections shown first
_GENERAL = "General"  # one-field top-level elements (fileIdentifier, language) gathered in one section
_DICTIONARY_ROOTS = frozenset({"FC_FeatureType", "detailed"})  # read into `dictionary`, not fields


def local(tag: object) -> str:
    """``{ns}name`` → ``name`` (comments and processing instructions have no string tag: "")."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def standard_of(root: ET.Element) -> str | None:
    """Which metadata standard ``root`` is written in, or ``None`` when it is not metadata."""
    name, ns = local(root.tag), root.tag.partition("}")[0]
    if name in ("MD_Metadata", "MI_Metadata"):
        if "19115/-3" in ns:
            return "ISO 19115-3"
        return "ISO 19139 (ISO 19115)"
    if name == "FC_FeatureCatalogue":
        return "ISO 19110 feature catalogue"
    if name == "metadata" and any(local(c.tag) in ("idinfo", "metainfo") for c in root):
        return "FGDC CSDGM"
    return None


def read_metadata(data: bytes) -> ArchiveMetadata | None:
    """``data`` as readable sections, or ``None`` when it is not XML metadata of a known standard."""
    try:
        root = ET.fromstring(data)  # noqa: S314 - expat: no external entities, no expansion bombs
    except ET.ParseError:
        return None
    standard = standard_of(root)
    if standard is None:
        return None
    fgdc = standard == "FGDC CSDGM"
    entities = list(_fgdc_entities(root) if fgdc else _iso_entities(root))
    return ArchiveMetadata(
        standard=standard,
        title=_clip(
            _path_text(root, ("idinfo", "citation", "citeinfo", "title")) if fgdc else _iso_title(root)
        ),
        abstract=_clip(_path_text(root, ("idinfo", "descript", "abstract")) if fgdc else _iso_abstract(root)),
        sections=_sections(root, fgdc=fgdc),
        dictionary=entities[:MAX_ENTITIES],
        more_entities=max(0, len(entities) - MAX_ENTITIES),
    )


# --- Sections -----------------------------------------------------------------------------------


def _sections(root: ET.Element, *, fgdc: bool) -> list[MetadataSection]:
    grouped: dict[str, list[MetadataField]] = {}
    for child in root:
        name = local(child.tag)
        if not name:
            continue
        heading = _heading(name, fgdc=fgdc)
        fields = list(_fields(child, [], fgdc=fgdc))
        if not fields:
            continue
        if len(fields) == 1 and heading not in grouped and name not in _ISO_HEADINGS and name not in _FGDC:
            # A lone value (fileIdentifier, language, dateStamp): one line of "General".
            grouped.setdefault(_GENERAL, []).append(
                MetadataField(label=_join([heading, fields[0].label]), value=fields[0].value)
            )
            continue
        grouped.setdefault(heading, []).extend(fields)
    first = [_heading(n, fgdc=fgdc) for n in _FIRST]
    order = sorted(grouped, key=lambda h: (h not in first, h != _GENERAL))  # stable: document order
    merged = {h: _merge(grouped[h]) for h in order}  # five referenceSystemInfo codes: one field
    return [
        MetadataSection(heading=h, fields=merged[h][:MAX_FIELDS], more=max(0, len(merged[h]) - MAX_FIELDS))
        for h in order
    ]


def _heading(name: str, *, fgdc: bool) -> str:
    if fgdc:
        return _FGDC.get(name) or name
    return _ISO_HEADINGS.get(name) or _words(name)


def _fields(el: ET.Element, labels: list[str], *, fgdc: bool) -> Iterator[MetadataField]:
    """``label: value`` for every value under ``el`` (not ``el``'s own name: that is the section)."""
    value = _value(el)
    if value is not None:
        yield MetadataField(label=_join(labels), value=value)
    for child in el:
        name = local(child.tag)
        if not name or name in _DICTIONARY_ROOTS or child.get("nilReason") is not None:
            continue
        word = _label(name, fgdc=fgdc)
        yield from _fields(child, [*labels, word] if word else labels, fgdc=fgdc)


def _value(el: ET.Element) -> str | None:
    """What ``el`` itself says: its text, a code list's code, or a link it only points to."""
    code = el.get("codeListValue")
    if code:
        camel = code.isascii() and code.isalnum() and not code.isupper() and not code.islower()
        return _words(code).lower() if camel else code  # pointOfContact -> "point of contact"; US stays
    text = (el.text or "").strip()
    if text:
        return _clip(text)
    href = el.get(XLINK_HREF)
    if href and not len(el):
        return href
    return None


def _label(name: str, *, fgdc: bool) -> str | None:
    if fgdc:
        return _FGDC.get(name, name)
    if name in _ISO_TYPES or _ISO_CLASS.match(name):
        return None
    return _words(name)


def _join(labels: list[str]) -> str:
    parts = [p for p in labels if p]
    return LABEL_JOIN.join(parts[-MAX_LABEL_PARTS:])


def _merge(fields: list[MetadataField]) -> list[MetadataField]:
    """Runs of one short label (ten scale denominators) become one field: "8000; 12000; …"."""
    out: list[MetadataField] = []
    for f in fields:
        last = out[-1] if out else None
        if last is not None and last.label == f.label and len(last.value) + len(f.value) < 400:
            if f.value not in last.value.split("; "):
                out[-1] = MetadataField(label=f.label, value=f"{last.value}; {f.value}")
            continue
        out.append(f)
    return out


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _words(name: str) -> str:
    """``electronicMailAddress`` → "Electronic mail address"; ``MD_Format`` → "Format"."""
    name = name.split("_", 1)[1] if _ISO_CLASS.match(name) else name
    spaced = _CAMEL.sub(" ", name).replace("_", " ").strip()
    words = [w if w.isupper() and len(w) > 1 else w.lower() for w in spaced.split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def _clip(text: str | None, limit: int = MAX_VALUE_CHARS) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_text(el: ET.Element | None) -> str | None:
    """The first non-empty text in ``el`` or under it."""
    if el is None:
        return None
    for node in el.iter():
        text = (node.text or "").strip()
        if text:
            return text
    return None


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if local(c.tag) == name), None)


def _children(el: ET.Element, name: str) -> Iterator[ET.Element]:
    return (c for c in el if local(c.tag) == name)


def _path_text(root: ET.Element, path: tuple[str, ...]) -> str | None:
    el: ET.Element | None = root
    for name in path:
        el = _child(el, name) if el is not None else None
    return _first_text(el)


def _iso_identification(root: ET.Element) -> ET.Element | None:
    info = _child(root, "identificationInfo")
    return next(iter(info), None) if info is not None else None


def _iso_title(root: ET.Element) -> str | None:
    ident = _iso_identification(root)
    citation = _child(ident, "citation") if ident is not None else None
    if citation is not None:
        cited = next(iter(citation), citation)  # CI_Citation
        return _first_text(_child(cited, "title"))
    if local(root.tag) == "FC_FeatureCatalogue":
        return _first_text(_child(root, "name"))
    return None


def _iso_abstract(root: ET.Element) -> str | None:
    ident = _iso_identification(root)
    if ident is None:
        return _first_text(_child(root, "scope")) if local(root.tag) == "FC_FeatureCatalogue" else None
    return _first_text(_child(ident, "abstract"))


# --- Data dictionary ----------------------------------------------------------------------------


def _iso_entities(root: ET.Element) -> Iterator[DictionaryEntity]:
    """ISO 19110 feature types, wherever they sit (19115-3 ``contentInfo``, a 19139 catalogue)."""
    for ft in root.iter():
        if local(ft.tag) != "FC_FeatureType":
            continue
        attrs = [
            _iso_attribute(a)
            for carrier in _children(ft, "carrierOfCharacteristics")
            for a in carrier
            if local(a.tag) in ("FC_FeatureAttribute", "FC_PropertyType")
        ]
        yield DictionaryEntity(
            name=_first_text(_child(ft, "typeName")) or "(unnamed)",
            label=_first_text(_child(ft, "aliases")),
            definition=_clip(_first_text(_child(ft, "definition"))),
            attributes=attrs[:MAX_ATTRIBUTES],
            more_attributes=max(0, len(attrs) - MAX_ATTRIBUTES),
        )


def _iso_attribute(a: ET.Element) -> DictionaryAttribute:
    values: list[str] = []
    for lv in _children(a, "listedValue"):
        item = next(iter(lv), None)  # FC_ListedValue
        if item is None:
            continue
        line = _value_line(
            _first_text(_child(item, "code")),
            _first_text(_child(item, "label")),
            _first_text(_child(item, "definition")),
        )
        if line:
            values.append(line)
    unit = _child(a, "valueMeasurementUnit")
    return DictionaryAttribute(
        name=_first_text(_child(a, "memberName")) or "(unnamed)",
        definition=_clip(_first_text(_child(a, "definition"))),
        type=_first_text(_child(a, "valueType")),
        unit=_unit(unit),
        values=values[:MAX_VALUES],
        more_values=max(0, len(values) - MAX_VALUES),
    )


def _unit(el: ET.Element | None) -> str | None:
    if el is None or el.get("nilReason") is not None:
        return None
    text = _first_text(el)
    if text:
        return text
    for node in el.iter():  # gml:UnitDefinition gml:id="m", or a link to a unit
        for key in (
            "uom",
            XLINK_HREF,
            "{http://www.opengis.net/gml/3.2}id",
            "{http://www.opengis.net/gml}id",
        ):
            if node.get(key):
                return node.get(key)
    return None


def _fgdc_entities(root: ET.Element) -> Iterator[DictionaryEntity]:
    eainfo = _child(root, "eainfo")
    if eainfo is None:
        return
    for detailed in _children(eainfo, "detailed"):
        ent = _child(detailed, "enttyp")
        attrs = [_fgdc_attribute(a) for a in _children(detailed, "attr")]
        yield DictionaryEntity(
            name=(_first_text(_child(ent, "enttypl")) if ent is not None else None) or "(unnamed)",
            label=None,
            definition=_clip(_first_text(_child(ent, "enttypd"))) if ent is not None else None,
            attributes=attrs[:MAX_ATTRIBUTES],
            more_attributes=max(0, len(attrs) - MAX_ATTRIBUTES),
        )


def _fgdc_attribute(a: ET.Element) -> DictionaryAttribute:
    values: list[str] = []
    unit: str | None = None
    kinds: list[str] = []
    for dom in _children(a, "attrdomv"):
        for d in dom:
            name = local(d.tag)
            if name == "edom":
                line = _value_line(_first_text(_child(d, "edomv")), None, _first_text(_child(d, "edomvd")))
                if line:
                    values.append(line)
            elif name == "rdom":
                low, high = _first_text(_child(d, "rdommin")), _first_text(_child(d, "rdommax"))
                kinds.append(f"range {low} to {high}")
                unit = unit or _first_text(_child(d, "attrunit"))
            elif name == "codesetd":
                kinds.append(f"codes: {_first_text(_child(d, 'codesetn'))}")
            elif name == "udom" and (text := _first_text(d)):
                kinds.append(_clip(text, MAX_VALUE_DEFINITION) or "")
    return DictionaryAttribute(
        name=_first_text(_child(a, "attrlabl")) or "(unnamed)",
        definition=_clip(_first_text(_child(a, "attrdef"))),
        type="; ".join(kinds) or None,
        unit=unit,
        values=values[:MAX_VALUES],
        more_values=max(0, len(values) - MAX_VALUES),
    )


def _value_line(code: str | None, label: str | None, definition: str | None) -> str | None:
    """``label (code): definition``, leaving out what is missing or repeats."""
    head = label or code
    if not head:
        return None
    if code and label and label != code:
        head = f"{label} ({code})"
    if definition and definition not in (code, label):
        return f"{head}: {_clip(definition, MAX_VALUE_DEFINITION)}"
    return head
