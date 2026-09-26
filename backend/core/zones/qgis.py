"""QGIS styles (QML) and the project (``.qgs`` in a ``.qgz``) for the zone working file.

Written with :mod:`xml.etree` in the QGIS 3.28 LTR format: layers with relative datasources into
the GeoPackage, a categorized renderer on ``zone_type`` in the map palette, labels, attribute forms
(value maps, the value relation from a document to its zone, check boxes, a date widget,
read-only review aids, not-null / unique / slug constraints), the zones -> documents relation,
snapping with topological editing and "avoid overlap" on the zones layer, and a "Zone review" print
layout: an atlas with one page per zone (map, name, type and summary, the zone's documents, legend,
scale bar, a sign-off line). The same QML goes into the GeoPackage's ``layer_styles``, so a layer
added by hand is styled as well. QGIS is not installed where this is generated:
``data/zones/qgis/build_project.py`` rebuilds the project inside QGIS with its own API when a
generated element needs repairing.
"""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

from core.zones import gpkg
from core.zones.schema import (
    DOCUMENT_FIELDS,
    DOCUMENT_STATUSES,
    STATUS_LABELS,
    UNCLASSIFIED_COLOUR,
    ZONE_FIELDS,
    ZONE_TYPES,
    FieldSpec,
)

QGIS_VERSION = "3.28.0-Firenze"
DOCTYPE = "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>"
SLUG_EXPRESSION = "regexp_match(\"zone_id\", '^[a-z0-9]+(-[a-z0-9]+)*$')"
PAPER_MM = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0)}  # landscape

# QGIS item type ids (QgsLayoutItemRegistry: QGraphicsItem::UserType 65536 + 100 + n)
_PAGE, _MAP, _LABEL, _LEGEND, _SCALEBAR = 65638, 65639, 65641, 65642, 65646


def rgba(colour: str, alpha: int = 255) -> str:
    """ "#B5744A" -> "181,116,74,255" (QGIS colour strings)."""
    c = colour.lstrip("#")
    return f"{int(c[0:2], 16)},{int(c[2:4], 16)},{int(c[4:6], 16)},{alpha}"


def _uid() -> str:
    return "{" + str(uuid.uuid4()) + "}"


# --- Option trees ---------------------------------------------------------------------------------


def option(value: Any, name: str | None = None) -> ET.Element:
    """A QGIS ``<Option>`` tree from Python values (dict = Map, list = List)."""
    el = ET.Element("Option")
    if name is not None:
        el.set("name", name)
    if isinstance(value, Mapping):
        el.set("type", "Map")
        for key, item in value.items():
            el.append(option(item, key))
    elif isinstance(value, list | tuple):
        el.set("type", "List")
        for item in value:
            el.append(option(item))
    elif isinstance(value, bool):
        el.set("type", "bool")
        el.set("value", "true" if value else "false")
    elif isinstance(value, int):
        el.set("type", "int")
        el.set("value", str(value))
    elif value is None:
        pass  # an invalid (null) option
    else:
        el.set("type", "QString")
        el.set("value", str(value))
    return el


def _dd_properties(tag: str = "data_defined_properties") -> ET.Element:
    el = ET.Element(tag)
    el.append(option({"name": "", "properties": None, "type": "collection"}))
    return el


# --- symbols and renderers ------------------------------------------------------------------------


def fill_symbol(
    name: str,
    fill: str,
    outline: str,
    *,
    outline_width: float = 0.26,
    style: str = "solid",
    outline_style: str = "solid",
) -> ET.Element:
    symbol = ET.Element(
        "symbol",
        {
            "type": "fill",
            "name": name,
            "alpha": "1",
            "clip_to_extent": "1",
            "force_rhr": "0",
            "is_animated": "0",
            "frame_rate": "10",
        },
    )
    symbol.append(_dd_properties())
    layer = ET.SubElement(
        symbol, "layer", {"class": "SimpleFill", "enabled": "1", "locked": "0", "pass": "0"}
    )
    layer.append(
        option(
            {
                "border_width_map_unit_scale": "3x:0,0,0,0,0,0",
                "color": fill,
                "joinstyle": "bevel",
                "offset": "0,0",
                "offset_map_unit_scale": "3x:0,0,0,0,0,0",
                "offset_unit": "MM",
                "outline_color": outline,
                "outline_style": outline_style,
                "outline_width": str(outline_width),
                "outline_width_unit": "MM",
                "style": style,
            }
        )
    )
    layer.append(_dd_properties())
    return symbol


def categorized_renderer(
    attribute: str, categories: Sequence[tuple[str, str, ET.Element]]
) -> ET.Element:
    renderer = ET.Element(
        "renderer-v2",
        {
            "type": "categorizedSymbol",
            "attr": attribute,
            "symbollevels": "0",
            "enableorderby": "0",
            "forceraster": "0",
            "referencescale": "-1",
        },
    )
    cats = ET.SubElement(renderer, "categories")
    symbols = ET.SubElement(renderer, "symbols")
    for index, (value, label, symbol) in enumerate(categories):
        ET.SubElement(
            cats,
            "category",
            {
                "value": value,
                "symbol": str(index),
                "label": label,
                "render": "true",
                "type": "string",
                "uuid": _uid(),
            },
        )
        symbol.set("name", str(index))
        symbols.append(symbol)
    source = ET.SubElement(renderer, "source-symbol")
    source.append(fill_symbol("0", rgba(UNCLASSIFIED_COLOUR, 90), rgba(UNCLASSIFIED_COLOUR)))
    ET.SubElement(renderer, "rotation")
    ET.SubElement(renderer, "sizescale")
    return renderer


def single_renderer(symbol: ET.Element) -> ET.Element:
    renderer = ET.Element(
        "renderer-v2",
        {
            "type": "singleSymbol",
            "symbollevels": "0",
            "enableorderby": "0",
            "forceraster": "0",
            "referencescale": "-1",
        },
    )
    symbols = ET.SubElement(renderer, "symbols")
    symbol.set("name", "0")
    symbols.append(symbol)
    ET.SubElement(renderer, "rotation")
    ET.SubElement(renderer, "sizescale")
    return renderer


def labeling(field_name: str, *, size: float = 10, bold: bool = True) -> ET.Element:
    el = ET.Element("labeling", {"type": "simple"})
    settings = ET.SubElement(el, "settings", {"calloutType": "simple"})
    style = ET.SubElement(
        settings,
        "text-style",
        {
            "fieldName": field_name,
            "isExpression": "0",
            "fontFamily": "Arial",
            "fontSize": str(size),
            "fontSizeUnit": "Point",
            "fontWeight": "75" if bold else "50",
            "fontItalic": "0",
            "textColor": "30,30,30,255",
            "textOpacity": "1",
            "namedStyle": "Bold" if bold else "Regular",
            "fontKerning": "1",
            "allowHtml": "0",
            "multilineHeight": "1",
            "multilineHeightUnit": "Percentage",
            "blendMode": "0",
            "previewBkgrdColor": "255,255,255,255",
            "capitalization": "0",
            "fontLetterSpacing": "0",
            "fontWordSpacing": "0",
            "textOrientation": "horizontal",
            "useSubstitutions": "0",
            "legendString": "Aa",
        },
    )
    ET.SubElement(style, "families")
    ET.SubElement(
        style,
        "text-buffer",
        {
            "bufferDraw": "1",
            "bufferSize": "1",
            "bufferSizeUnits": "MM",
            "bufferColor": "255,255,255,255",
            "bufferOpacity": "1",
            "bufferJoinStyle": "128",
            "bufferNoFill": "1",
            "bufferBlendMode": "0",
            "bufferSizeMapUnitScale": "3x:0,0,0,0,0,0",
        },
    )
    ET.SubElement(style, "substitutions")
    ET.SubElement(
        settings,
        "text-format",
        {
            "wrapChar": "",
            "autoWrapLength": "18",
            "useMaxLineLengthForAutoWrap": "1",
            "multilineAlign": "1",
            "addDirectionSymbol": "0",
            "formatNumbers": "0",
            "decimals": "3",
            "plussign": "0",
            "placeDirectionSymbol": "0",
            "reverseDirectionSymbol": "0",
            "leftDirectionSymbol": "<",
            "rightDirectionSymbol": ">",
        },
    )
    ET.SubElement(
        settings,
        "placement",
        {
            "placement": "1",
            "centroidInside": "1",
            "centroidWhole": "0",
            "fitInPolygonOnly": "0",
            "offsetType": "0",
            "priority": "5",
            "dist": "0",
            "distUnits": "MM",
            "quadOffset": "4",
            "xOffset": "0",
            "yOffset": "0",
            "offsetUnits": "MM",
            "rotationAngle": "0",
            "maxCurvedCharAngleIn": "25",
            "maxCurvedCharAngleOut": "-25",
            "repeatDistance": "0",
            "repeatDistanceUnits": "MM",
            "overrunDistance": "0",
            "overrunDistanceUnit": "MM",
            "placementFlags": "10",
            "polygonPlacementFlags": "2",
            "layerType": "PolygonGeometry",
        },
    )
    ET.SubElement(
        settings,
        "rendering",
        {
            "drawLabels": "1",
            "obstacle": "1",
            "obstacleFactor": "1",
            "obstacleType": "1",
            "fontLimitPixelSize": "0",
            "fontMinPixelSize": "3",
            "fontMaxPixelSize": "10000",
            "scaleVisibility": "0",
            "scaleMin": "0",
            "scaleMax": "0",
            "upsidedownLabels": "0",
            "limitNumLabels": "0",
            "maxNumLabels": "2000",
            "minFeatureSize": "0",
            "labelPerPart": "0",
            "mergeLines": "0",
            "displayAll": "0",
            "zIndex": "0",
            "unplacedVisibility": "0",
        },
    )
    settings.append(_dd_properties("dd_properties"))
    return el


# --- forms ----------------------------------------------------------------------------------------


def _widget(field_name: str, kind: str, config: Mapping[str, Any]) -> ET.Element:
    fld = ET.Element("field", {"name": field_name, "configurationFlags": "None"})
    widget = ET.SubElement(fld, "editWidget", {"type": kind})
    ET.SubElement(widget, "config").append(option(dict(config)))
    return fld


def _checkbox(field_name: str) -> ET.Element:
    return _widget(
        field_name, "CheckBox", {"CheckedState": "", "TextDisplayMethod": 0, "UncheckedState": ""}
    )


def _text(field_name: str, *, multiline: bool = False) -> ET.Element:
    return _widget(field_name, "TextEdit", {"IsMultiline": multiline, "UseHtml": False})


def _value_map(field_name: str, pairs: Sequence[tuple[str, str]]) -> ET.Element:
    return _widget(field_name, "ValueMap", {"map": [{label: value} for label, value in pairs]})


def _form_elements(
    fields: Sequence[FieldSpec],
    widgets: Mapping[str, ET.Element],
    *,
    constraints: Mapping[str, tuple[int, str | None, str]] | None = None,
    read_only: Sequence[str] = (),
    display_expression: str,
) -> list[ET.Element]:
    """fieldConfiguration, aliases, defaults, constraints, editable flags; field indexes count
    the GeoPackage's ``fid`` as field 0."""
    constraints = constraints or {}
    config = ET.Element("fieldConfiguration")
    aliases = ET.Element("aliases")
    defaults = ET.Element("defaults")
    cons = ET.Element("constraints")
    exprs = ET.Element("constraintExpressions")
    editable = ET.Element("editable")
    for index, spec in enumerate(fields, start=1):
        config.append(widgets.get(spec.name) or _text(spec.name))
        ET.SubElement(
            aliases, "alias", {"index": str(index), "field": spec.name, "name": spec.alias}
        )
        ET.SubElement(
            defaults, "default", {"field": spec.name, "expression": "", "applyOnUpdate": "0"}
        )
        mask, expression, description = constraints.get(spec.name, (0, None, ""))
        ET.SubElement(
            cons,
            "constraint",
            {
                "field": spec.name,
                "constraints": str(mask),
                "notnull_strength": "1" if mask & 1 else "0",
                "unique_strength": "1" if mask & 2 else "0",
                "exp_strength": "1" if mask & 4 else "0",
            },
        )
        ET.SubElement(
            exprs, "constraint", {"field": spec.name, "exp": expression or "", "desc": description}
        )
        ET.SubElement(
            editable,
            "field",
            {"name": spec.name, "editable": "0" if spec.name in read_only else "1"},
        )
    preview = ET.Element("previewExpression")
    preview.text = display_expression
    form = ET.Element("editorlayout")
    form.text = "generatedlayout"
    return [config, aliases, defaults, cons, exprs, editable, form, preview]


# --- layer styles ---------------------------------------------------------------------------------


def zone_style_elements() -> list[ET.Element]:
    categories = [
        (
            t.value,
            t.label_en,
            fill_symbol("", rgba(t.colour, 89), rgba(t.colour), outline_width=0.6),
        )
        for t in ZONE_TYPES
    ]
    categories.append(
        (
            "",
            "Not classified",
            fill_symbol(
                "", rgba(UNCLASSIFIED_COLOUR, 64), rgba(UNCLASSIFIED_COLOUR), outline_width=0.6
            ),
        )
    )
    widgets = {
        "zone_id": _text("zone_id"),
        "name": _text("name"),
        "zone_type": _value_map(
            "zone_type", [("Not classified", "")] + [(t.label_en, t.value) for t in ZONE_TYPES]
        ),
        "general_planning_summary": _text("general_planning_summary", multiline=True),
        "notes": _text("notes", multiline=True),
        "no_adopted_plan": _checkbox("no_adopted_plan"),
    }
    constraints = {
        "zone_id": (7, SLUG_EXPRESSION, "lower-case letters, digits and hyphens (a stable slug)"),
        "name": (3, None, ""),
    }
    return [
        categorized_renderer("zone_type", categories),
        labeling("name"),
        *_form_elements(ZONE_FIELDS, widgets, constraints=constraints, display_expression='"name"'),
    ]


def documents_style_elements(
    document_types: Mapping[str, str], zones_layer: LayerRef | None = None
) -> list[ET.Element]:
    zones = zones_layer or LayerRef("", "Zones", "", "ogr")
    widgets = {
        "zone_id": _widget(
            "zone_id",
            "ValueRelation",
            {
                "AllowMulti": False,
                "AllowNull": False,
                "Description": "",
                "FilterExpression": "",
                "Key": "zone_id",
                "Layer": zones.id,
                "LayerName": zones.name,
                "LayerProviderName": zones.provider,
                "LayerSource": zones.source,
                "NofColumns": 1,
                "OrderByValue": True,
                "UseCompleter": False,
                "Value": "name",
            },
        ),
        "document_type": _value_map(
            "document_type",
            [(f"{code} — {name}" if name else code, code) for code, name in document_types.items()],
        ),
        "status": _value_map("status", [(STATUS_LABELS[s], s) for s in DOCUMENT_STATUSES]),
        "adoption_date": _widget(
            "adoption_date",
            "DateTime",
            {
                "allow_null": True,
                "calendar_popup": True,
                "display_format": "yyyy-MM-dd",
                "field_format": "yyyy-MM-dd",
                "field_iso_format": False,
            },
        ),
        "notes": _text("notes", multiline=True),
        "poc_coverage": _checkbox("poc_coverage"),
        "confirmed": _checkbox("confirmed"),
    }
    constraints = {spec.name: (1, None, "") for spec in DOCUMENT_FIELDS if spec.required}
    read_only = [spec.name for spec in DOCUMENT_FIELDS if spec.review_aid]
    return _form_elements(
        DOCUMENT_FIELDS,
        widgets,
        constraints=constraints,
        read_only=read_only,
        display_expression='"document_type" || \' \' || "document_name"',
    )


REFERENCE_STYLES: dict[str, tuple[str, str, float, str, str]] = {
    # layer -> fill, outline, width, fill style, outline style
    "cadastral_municipalities": ("0,0,0,0", "60,60,60,255", 0.8, "no", "solid"),
    "cadastral_parcels": ("0,0,0,0", "150,150,150,160", 0.1, "no", "solid"),
    "document_coverage": ("94,138,130,40", "94,138,130,255", 0.4, "solid", "dash"),
    "current_zones": ("120,120,120,90", "120,120,120,200", 0.3, "b_diagonal", "dot"),
}


def reference_style_elements(layer: str) -> list[ET.Element]:
    fill, outline, width, style, outline_style = REFERENCE_STYLES.get(
        layer, ("0,0,0,0", "100,100,100,255", 0.3, "no", "solid")
    )
    symbol = fill_symbol(
        "0", fill, outline, outline_width=width, style=style, outline_style=outline_style
    )
    elements = [single_renderer(symbol)]
    if layer in ("cadastral_municipalities", "current_zones"):
        elements.append(
            labeling(
                "ko_name" if layer == "cadastral_municipalities" else "name", size=8, bold=False
            )
        )
    return elements


def qml(elements: Sequence[ET.Element], *, labels: bool = False) -> str:
    """A stand-alone QML document (the ``layer_styles`` default style)."""
    root = ET.Element(
        "qgis",
        {
            "version": QGIS_VERSION,
            "styleCategories": "AllStyleCategories",
            "labelsEnabled": "1" if labels else "0",
            "readOnly": "0",
        },
    )
    for el in elements:
        root.append(el)
    return DOCTYPE + "\n" + ET.tostring(root, encoding="unicode")


# --- the project --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LayerRef:
    id: str
    name: str
    source: str
    provider: str


@dataclass(slots=True)
class ProjectLayer:
    id: str
    name: str
    kind: str  # vector | raster
    source: str
    provider: str  # ogr | wms
    srs: int
    geometry: str | None  # Polygon | None (a table)
    style: list[ET.Element] = field(default_factory=list)
    read_only: bool = False
    labels: bool = False
    visible: bool = True
    extent: tuple[float, float, float, float] | None = None

    @property
    def ref(self) -> LayerRef:
        return LayerRef(self.id, self.name, self.source, self.provider)


def layer_id(name: str) -> str:
    return f"{name}_{uuid.uuid4().hex[:12]}"


def gpkg_source(geopackage: Path, table: str) -> str:
    return f"./{geopackage.name}|layername={table}"


def xyz_source(url: str, zmax: int) -> str:
    return f"type=xyz&url={quote(url, safe=':/')}&zmax={zmax}&zmin=0"


def _spatialrefsys(srs_id: int) -> ET.Element:
    srs = gpkg.srs_for(srs_id)
    el = ET.Element("spatialrefsys", {"nativeFormat": "Wkt"})
    for tag, value in (
        ("wkt", srs.definition),
        ("proj4", srs.proj4),
        ("srsid", "0"),
        ("srid", str(srs.srs_id)),
        ("authid", srs.authid),
        ("description", srs.name),
        ("projectionacronym", "longlat" if srs_id == 4326 else "utm" if srs_id == 25834 else ""),
        ("ellipsoidacronym", ""),
        ("geographicflag", "true" if srs_id == 4326 else "false"),
    ):
        ET.SubElement(el, tag).text = value
    return el


def _extent(tag: str, extent: tuple[float, float, float, float]) -> ET.Element:
    el = ET.Element(tag)
    for name, value in zip(("xmin", "ymin", "xmax", "ymax"), extent, strict=True):
        ET.SubElement(el, name).text = repr(float(value))
    return el


def _maplayer(layer: ProjectLayer) -> ET.Element:
    attrs = {
        "type": layer.kind,
        "hasScaleBasedVisibilityFlag": "0",
        "minScale": "100000000",
        "maxScale": "0",
        "styleCategories": "AllStyleCategories",
        "autoRefreshEnabled": "0",
        "autoRefreshTime": "0",
        "refreshOnNotifyEnabled": "0",
        "refreshOnNotifyMessage": "",
    }
    if layer.kind == "vector":
        attrs.update(
            geometry=layer.geometry or "No geometry",
            wkbType="MultiPolygon" if layer.geometry else "NoGeometry",
            readOnly="1" if layer.read_only else "0",
            labelsEnabled="1" if layer.labels else "0",
            simplifyAlgorithm="0",
            simplifyDrawingHints="1" if layer.geometry else "0",
            simplifyDrawingTol="1",
            simplifyLocal="1",
            simplifyMaxScale="1",
            symbologyReferenceScale="-1",
        )
    el = ET.Element("maplayer", attrs)
    if layer.extent:
        el.append(_extent("extent", layer.extent))
    ET.SubElement(el, "id").text = layer.id
    ET.SubElement(el, "datasource").text = layer.source
    keywords = ET.SubElement(el, "keywordList")
    ET.SubElement(keywords, "value").text = ""
    ET.SubElement(el, "layername").text = layer.name
    ET.SubElement(el, "srs").append(_spatialrefsys(layer.srs))
    provider = ET.SubElement(
        el, "provider", {"encoding": "UTF-8"} if layer.kind == "vector" else {}
    )
    provider.text = layer.provider
    manager = ET.SubElement(el, "map-layer-style-manager", {"current": "default"})
    ET.SubElement(manager, "map-layer-style", {"name": "default"})
    flags = ET.SubElement(el, "flags")
    for tag, value in (
        ("Identifiable", "1"),
        ("Removable", "1"),
        ("Searchable", "1"),
        ("Private", "0"),
    ):
        ET.SubElement(flags, tag).text = value
    if layer.kind == "vector":
        for tag in ("vectorjoins", "layerDependencies", "dataDependencies", "expressionfields"):
            ET.SubElement(el, tag)
        el.extend(layer.style)
        ET.SubElement(el, "blendMode").text = "0"
        ET.SubElement(el, "featureBlendMode").text = "0"
        ET.SubElement(el, "layerOpacity").text = "1"
    else:
        pipe = ET.SubElement(el, "pipe")
        renderer = ET.SubElement(
            pipe,
            "rasterrenderer",
            {
                "type": "singlebandcolordata",
                "opacity": "0.85",
                "alphaBand": "-1",
                "band": "1",
                "nodataColor": "",
            },
        )
        ET.SubElement(renderer, "rasterTransparency")
        ET.SubElement(
            pipe, "brightnesscontrast", {"brightness": "0", "contrast": "0", "gamma": "1"}
        )
        ET.SubElement(
            pipe,
            "huesaturation",
            {"saturation": "-60", "grayscaleMode": "0", "colorizeOn": "0", "invertColors": "0"},
        )
        ET.SubElement(pipe, "rasterresampler", {"maxOversampling": "2"})
        ET.SubElement(pipe, "resamplingStage").text = "resamplingFilter"
        ET.SubElement(el, "blendMode").text = "0"
    return el


def _layout_item(
    kind: int,
    item_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    frame: bool = False,
    background: bool = True,
    z: int = 1,
    **attrs: str,
) -> ET.Element:
    el = ET.Element(
        "LayoutItem",
        {
            "type": str(kind),
            "uuid": _uid(),
            "templateUuid": "",
            "id": item_id,
            "size": f"{w},{h},mm",
            "position": f"{x},{y},mm",
            "positionOnPage": f"{x},{y},mm",
            "referencePoint": "0",
            "itemRotation": "0",
            "zValue": str(z),
            "visibility": "1",
            "frame": "true" if frame else "false",
            "frameJoinStyle": "miter",
            "background": "true" if background else "false",
            "blendMode": "0",
            "excludeFromExports": "0",
            "opacity": "1",
            "groupUuid": "",
            "positionLock": "false",
            "outlineWidthM": "0.3,mm",
            **attrs,
        },
    )
    ET.SubElement(el, "FrameColor", {"red": "0", "green": "0", "blue": "0", "alpha": "255"})
    ET.SubElement(el, "BGColor", {"red": "255", "green": "255", "blue": "255", "alpha": "255"})
    obj = ET.SubElement(el, "LayoutObject")
    obj.append(_dd_properties("dataDefinedProperties"))
    ET.SubElement(obj, "customproperties").append(option({}))
    return el


def _label(
    item_id: str,
    text: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    size: float,
    bold: bool = False,
    html: bool = False,
) -> ET.Element:
    el = _layout_item(
        _LABEL,
        item_id,
        x,
        y,
        w,
        h,
        background=False,
        z=3,
        labelText=text,
        htmlState="1" if html else "0",
        marginX="1",
        marginY="1",
        halign="1",
        valign="32",
    )
    ET.SubElement(
        el,
        "text-style",
        {
            "fontFamily": "Arial",
            "fontSize": str(size),
            "fontSizeUnit": "Point",
            "fontWeight": "75" if bold else "50",
            "fontItalic": "0",
            "textColor": "30,30,30,255",
            "textOpacity": "1",
            "namedStyle": "Bold" if bold else "Regular",
            "fontKerning": "1",
            "allowHtml": "1" if html else "0",
            "multilineHeight": "1",
            "multilineHeightUnit": "Percentage",
            "blendMode": "0",
            "capitalization": "0",
            "textOrientation": "horizontal",
        },
    )
    return el


def layout_xml(
    *,
    title: str,
    zones: ProjectLayer,
    documents: ProjectLayer,
    map_layers: Sequence[ProjectLayer],
    srs_id: int,
    extent: tuple[float, float, float, float] | None,
    paper: str = "A3",
) -> ET.Element:
    """The "Zone review" atlas: one page per zone, sorted by name."""
    width, height = PAPER_MM.get(paper, PAPER_MM["A3"])
    layouts = ET.Element("Layouts")
    layout = ET.SubElement(
        layouts,
        "Layout",
        {"name": "Zone review", "units": "mm", "worldFileMap": "", "printResolution": "300"},
    )
    ET.SubElement(
        layout,
        "Snapper",
        {"tolerance": "5", "snapToItems": "1", "snapToGrid": "0", "snapToGuides": "1"},
    )
    ET.SubElement(
        layout,
        "Grid",
        {"offsetX": "0", "offsetY": "0", "offsetUnits": "mm", "resolution": "10", "resUnits": "mm"},
    )
    pages = ET.SubElement(layout, "PageCollection")
    pages.append(fill_symbol("", "255,255,255,255", "35,35,35,255", outline_style="no"))
    pages.append(_layout_item(_PAGE, "", 0, 0, width, height, z=0))
    ET.SubElement(pages, "GuideCollection", {"visible": "1"})

    margin = 10.0
    side = 118.0
    map_w, map_h = width - side - 3 * margin, height - 2 * margin - 22
    map_item = _layout_item(
        _MAP,
        "map",
        margin,
        margin + 22,
        map_w,
        map_h,
        frame=True,
        z=1,
        keepLayerSet="true",
        followPreset="false",
        followPresetName="",
        drawCanvasItems="true",
        mapRotation="0",
        labelMargin="0,mm",
        mapFlags="03",
        isTemporal="0",
    )
    if extent:
        ET.SubElement(
            map_item,
            "Extent",
            {
                k: repr(float(v))
                for k, v in zip(("xmin", "ymin", "xmax", "ymax"), extent, strict=True)
            },
        )
    ET.SubElement(map_item, "crs").append(_spatialrefsys(srs_id))
    layer_set = ET.SubElement(map_item, "LayerSet")
    for lyr in map_layers:
        ET.SubElement(
            layer_set, "Layer", {"source": lyr.source, "provider": lyr.provider, "name": lyr.name}
        ).text = lyr.id
    ET.SubElement(
        map_item,
        "AtlasMap",
        {"atlasDriven": "1", "scalingMode": "2", "margin": "0.10000000000000001"},
    )
    ET.SubElement(map_item, "labelBlockingItems")
    layout.append(map_item)
    map_uuid = map_item.get("uuid", "")

    right = margin * 2 + map_w
    layout.append(
        _label(
            "title", '[% "name" %] ([% "zone_id" %])', margin, margin, map_w, 18, size=20, bold=True
        )
    )
    layout.append(
        _label(
            "zone type",
            "[% map_get(map('residential','Residential','commercial','Commercial','mixed',"
            "'Mixed use','public_institutional','Public / institutional','green_recreation',"
            "'Green / recreation'), coalesce(\"zone_type\", '')) %]"
            "[% if(\"no_adopted_plan\", ' · no adopted plan', '') %]"
            "\n[% coalesce(\"general_planning_summary\", '') %]",
            right,
            margin,
            side,
            32,
            size=10,
        )
    )
    documents_expr = (
        "[% aggregate(layer:='" + documents.id + "', aggregate:='concatenate', "
        'expression:="document_type" || \' \' || "document_name" || \' — \' || "status" || '
        "if(\"confirmed\", ' ✓', ''), filter:=\"zone_id\" = attribute(@atlas_feature, "
        "'zone_id'), concatenator:='<br>') %]"
    )
    layout.append(
        _label(
            "documents",
            "<b>Planning documents</b><br>" + documents_expr,
            right,
            margin + 36,
            side,
            height - 2 * margin - 36 - 70,
            size=8,
            html=True,
        )
    )
    legend = _layout_item(
        _LEGEND,
        "legend",
        right,
        height - margin - 66,
        side,
        40,
        z=3,
        background=False,
        title="Zone types",
        map_uuid=map_uuid,
        legendFilterByAtlas="0",
        resizeToContents="1",
        columnCount="1",
        splitLayer="0",
        equalColumnWidth="0",
        wrapChar="",
        symbolWidth="7",
        symbolHeight="4",
        wmsLegendWidth="50",
        wmsLegendHeight="25",
        rasterBorder="1",
        rasterBorderWidth="0",
        rasterBorderColor="0,0,0,255",
        titleAlignment="1",
    )
    tree = ET.SubElement(legend, "layer-tree-group")
    ET.SubElement(tree, "customproperties").append(option({}))
    ET.SubElement(
        tree,
        "layer-tree-layer",
        {
            "id": zones.id,
            "name": zones.name,
            "source": zones.source,
            "providerKey": "ogr",
            "checked": "Qt::Checked",
            "expanded": "1",
        },
    )
    layout.append(legend)
    layout.append(
        _layout_item(
            _SCALEBAR,
            "scale bar",
            margin,
            height - margin - 12,
            70,
            10,
            z=3,
            background=False,
            map_uuid=map_uuid,
            style="Single Box",
            units="meters",
            unitType="meters",
            numSegments="2",
            numSegmentsLeft="0",
            segmentSizeMode="1",
            minBarWidth="40",
            maxBarWidth="70",
            unitLabel="m",
            numMapUnitsPerScaleBarUnit="1",
            height="3",
            labelBarSpace="3",
            boxContentSpace="1",
            alignment="0",
            numUnitsPerSegment="500",
        )
    )
    layout.append(
        _label(
            "sign-off",
            "Confirmed for UrbanView by ____________________    date ____________",
            right,
            height - margin - 20,
            side,
            12,
            size=9,
        )
    )
    layout.append(
        _label(
            "footer",
            f"{title} · [% @atlas_featurenumber %] / [% @atlas_totalfeatures %]",
            margin,
            height - margin - 2,
            map_w,
            6,
            size=7,
        )
    )
    ET.SubElement(
        layout,
        "Atlas",
        {
            "enabled": "1",
            "coverageLayer": zones.id,
            "coverageLayerName": zones.name,
            "coverageLayerSource": zones.source,
            "coverageLayerProvider": "ogr",
            "hideCoverage": "0",
            "pageNameExpression": '"name"',
            "filenamePattern": "'zone_' || \"zone_id\"",
            "sortFeatures": "1",
            "sortKey": "name",
            "sortAscending": "1",
            "filterFeatures": "0",
        },
    )
    return layouts


def project_xml(
    *,
    title: str,
    layers: Sequence[ProjectLayer],
    srs_id: int,
    extent: tuple[float, float, float, float] | None,
    paper: str = "A3",
) -> str:
    """The .qgs document. ``layers`` top to bottom; must contain ``zones`` and
    ``zone_documents`` (by name of their table in the source)."""
    by_table = {
        lyr.source.rsplit("layername=", 1)[-1]: lyr for lyr in layers if lyr.kind == "vector"
    }
    zones, documents = by_table["zones"], by_table["zone_documents"]
    root = ET.Element(
        "qgis",
        {
            "projectname": title,
            "version": QGIS_VERSION,
            "saveUser": "urbanview",
            "saveUserFull": "UrbanView zone tooling",
            "saveDateTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    ET.SubElement(root, "homePath", {"path": ""})
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "transaction", {"mode": "Disabled"})
    ET.SubElement(root, "projectFlags", {"set": ""})
    ET.SubElement(root, "projectCrs").append(_spatialrefsys(srs_id))

    tree = ET.SubElement(root, "layer-tree-group")
    ET.SubElement(tree, "customproperties").append(option({}))
    for lyr in layers:
        node = ET.SubElement(
            tree,
            "layer-tree-layer",
            {
                "id": lyr.id,
                "name": lyr.name,
                "source": lyr.source,
                "providerKey": lyr.provider,
                "checked": "Qt::Checked" if lyr.visible else "Qt::Unchecked",
                "expanded": "1",
                "legend_exp": "",
                "patch_size": "-1,-1",
                "legend_split_behavior": "0",
            },
        )
        ET.SubElement(node, "customproperties").append(option({}))
    order = ET.SubElement(tree, "custom-order", {"enabled": "0"})
    for lyr in layers:
        ET.SubElement(order, "item").text = lyr.id

    snapping = ET.SubElement(
        root,
        "snapping-settings",
        {
            "enabled": "1",
            "mode": "3",
            "type": "3",
            "tolerance": "10",
            "unit": "1",
            "intersection-snapping": "0",
            "self-snapping": "0",
            "minScale": "0",
            "maxScale": "0",
            "scaleDependencyMode": "0",
        },
    )
    individual = ET.SubElement(snapping, "individual-layer-settings")
    for lyr in layers:
        if lyr.kind == "vector" and lyr.geometry:
            ET.SubElement(
                individual,
                "layer-setting",
                {
                    "id": lyr.id,
                    "enabled": "1" if lyr is zones else "0",
                    "type": "3",
                    "tolerance": "10",
                    "units": "1",
                    "minScale": "0",
                    "maxScale": "0",
                },
            )

    relations = ET.SubElement(root, "relations")
    relation = ET.SubElement(
        relations,
        "relation",
        {
            "id": "zone_documents_zone",
            "name": "Documents of the zone",
            "referencingLayer": documents.id,
            "referencedLayer": zones.id,
            "strength": "Association",
        },
    )
    ET.SubElement(
        relation, "fieldRef", {"referencingField": "zone_id", "referencedField": "zone_id"}
    )
    ET.SubElement(root, "polymorphicRelations")

    canvas = ET.SubElement(root, "mapcanvas", {"annotationsVisible": "1", "name": "theMapCanvas"})
    ET.SubElement(canvas, "units").text = "meters"
    if extent:
        canvas.append(_extent("extent", extent))
    ET.SubElement(canvas, "rotation").text = "0"
    ET.SubElement(canvas, "destinationsrs").append(_spatialrefsys(srs_id))
    ET.SubElement(canvas, "rendermaptile").text = "0"

    project_layers = ET.SubElement(root, "projectlayers")
    for lyr in layers:
        project_layers.append(_maplayer(lyr))
    layer_order = ET.SubElement(root, "layerorder")
    for lyr in layers:
        ET.SubElement(layer_order, "layer", {"id": lyr.id})

    properties = ET.SubElement(root, "properties")
    paths = ET.SubElement(properties, "Paths")
    ET.SubElement(paths, "Absolute", {"type": "bool"}).text = "false"
    digitizing = ET.SubElement(properties, "Digitizing")
    ET.SubElement(digitizing, "TopologicalEditing", {"type": "int"}).text = "1"
    ET.SubElement(digitizing, "AvoidIntersectionsMode", {"type": "int"}).text = "2"
    measurement = ET.SubElement(properties, "Measurement")
    ET.SubElement(measurement, "DistanceUnits", {"type": "QString"}).text = "meters"
    ET.SubElement(measurement, "AreaUnits", {"type": "QString"}).text = "m2"
    avoid = ET.SubElement(root, "avoidIntersectionsLayers")
    ET.SubElement(avoid, "layer", {"id": zones.id})

    map_layers = [lyr for lyr in layers if lyr.visible and lyr.kind == "vector" and lyr.geometry]
    map_layers += [lyr for lyr in layers if lyr.kind == "raster"]
    root.append(
        layout_xml(
            title=title,
            zones=zones,
            documents=documents,
            map_layers=map_layers,
            srs_id=srs_id,
            extent=extent,
            paper=paper,
        )
    )
    ET.indent(root, space=" ")
    return DOCTYPE + "\n" + ET.tostring(root, encoding="unicode") + "\n"


def write_qgz(path: Path, qgs_xml: str, *, stem: str | None = None) -> Path:
    """A .qgz is a zip holding the .qgs (QGIS adds its auxiliary .qgd when it saves)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem or path.stem}.qgs", qgs_xml.encode("utf-8"))
    return path
