"""Canonical result -> review-queue rows (``planning_parameter_extractions``, STAGING).

Only stated values of planning fields (the keys of ``planning_fields``) become review items, all
``pending_review``: parcel values on their urban parcel, block values on their block, document
rules at document scope. Each row stores the leaf as extracted in ``payload`` with the schema and
prompt versions (read back with :func:`core.extraction.schema.read_payload`), plus the flags and
the extraction method as columns the review queue filters and shows, the target as printed
(``target_label``) and normalised (``target_key``: parcel key, block key or ``document``).
Nothing is published here: the review queue decides and the publish job copies what was
approved.

A parcel or block that matches no geometry of the document is, with ``unmatched="stage"`` (the
extraction job), still staged, as a text reference: no ``urban_parcel_id`` / ``block_id``, the
printed number in ``target_label``, flagged ``target_unmatched`` for the reviewer (or
``target_staged`` when it matches geometry the GIS job staged but nobody has published yet). The
publish job never serves such an item (it has no parcel to attach to). With the default
``unmatched="skip"`` they are reported instead (``parcel_not_in_geometry`` /
``block_not_in_geometry``).

Everything else is reported, never dropped silently: values of fields that are not planning
fields (document identity, block areas, public-area relations, other conditions, utilities,
legend entries: kept in the extraction result), values whose parcel has no number or whose block
has no label, and the counts of missing values by reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core.extraction.fields import RULE_FIELDS
from core.extraction.schema import (
    EntityType,
    ExtractionResult,
    Leaf,
    MissingValue,
    RuleFields,
    StagedPayload,
    StatedValue,
    iter_leaves,
)

UnstagedReason = Literal[
    "not_a_planning_field",
    "parcel_not_in_geometry",
    "block_not_in_geometry",
    "parcel_without_number",
    "block_without_label",
]
# Item flags the staging step adds (the leaf's own flags come from the validator).
TARGET_UNMATCHED = "target_unmatched"  # no geometry of the document has this number / label
TARGET_STAGED = "target_staged"  # matches geometry staged by the GIS job, not published yet
DOCUMENT_KEY = "document"


@dataclass(frozen=True, slots=True)
class Unstaged:
    path: str
    reason: UnstagedReason


@dataclass(slots=True)
class StagingPlan:
    rows: list[dict[str, Any]] = field(default_factory=list)
    unstaged: list[Unstaged] = field(default_factory=list)
    missing: dict[str, int] = field(default_factory=dict)


def source_note(leaf: StatedValue) -> str | None:
    ref = leaf.source.table_ref
    if ref is None:
        return None
    return " – ".join(part for part in (ref.row, ref.column) if part) or None


def _printed(leaf: Leaf) -> str | None:
    return leaf.stated.value if isinstance(leaf, StatedValue) else None


def to_staging_rows(
    result: ExtractionResult,
    *,
    extracted_by: str,
    parcel_ids: Mapping[str, int],
    block_ids: Mapping[str, int],
    job_id: int | None = None,
    run_id: int | None = None,
    unmatched: Literal["skip", "stage"] = "skip",
    staged_parcels: Collection[str] = (),
    staged_blocks: Collection[str] = (),
) -> StagingPlan:
    """Rows for ``planning_parameter_extractions``. ``parcel_ids`` maps the document's urban
    parcels by :func:`~core.extraction.normalise.parcel_key`, ``block_ids`` its blocks by
    :func:`~core.extraction.normalise.block_key`; ``staged_parcels`` / ``staged_blocks`` are the
    keys of geometry staged but not yet published."""
    plan = StagingPlan()
    staged_paths: set[str] = set()

    def stage(
        leaf: Leaf,
        path: str,
        field_key: str,
        entity_type: EntityType,
        *,
        urban_parcel_id: int | None = None,
        block_id: int | None = None,
        number: str | None = None,
        block_ref: str | None = None,
        label: str | None = None,
        key: str | None = None,
        target_flag: str | None = None,
    ) -> None:
        staged_paths.add(path)
        if not isinstance(leaf, StatedValue):
            return
        numeric = isinstance(leaf.value, int | float) and not isinstance(leaf.value, bool)
        payload = StagedPayload(
            schema_version=result.schema_version,
            prompt_version=result.prompt_version,
            task=result.task,
            entity_type=entity_type,
            path=path,
            field_key=field_key,
            urban_parcel_number=number,
            block_ref=block_ref,
            leaf=leaf,
        )
        flags = [str(flag) for flag in leaf.flags]
        if target_flag:
            flags.append(target_flag)
        plan.rows.append(
            {
                "municipality_id": result.municipality_id,
                "document_id": result.document_id,
                "entity_type": entity_type,
                "urban_parcel_id": urban_parcel_id,
                "block_id": block_id,
                "zone_id": None,
                "field_key": field_key,
                "parameter_key": field_key,
                "value_text": None if numeric else str(leaf.value),
                "value_number": float(leaf.value) if numeric else None,
                "unit": leaf.unit,
                "raw_text": leaf.raw_text,
                "confidence": leaf.confidence,
                "source_page": leaf.source.page,
                "source_bbox": list(leaf.source.bbox) if leaf.source.bbox else None,
                "source_note": source_note(leaf),
                "extracted_by": extracted_by,
                "review_state": "pending_review",
                "schema_version": result.schema_version,
                "prompt_version": result.prompt_version,
                "extraction_method": leaf.extraction_method,
                "flags": flags,
                "payload": payload.model_dump(mode="json"),
                "job_id": job_id,
                "run_id": run_id,
                "target_label": label,
                "target_key": key,
            }
        )

    def skip(leaf: Leaf, path: str, reason: UnstagedReason) -> None:
        staged_paths.add(path)
        if isinstance(leaf, StatedValue):
            plan.unstaged.append(Unstaged(path, reason))

    def rules(
        rule_fields: RuleFields,
        path: str,
        target: dict[str, Any] | None,
        reason: UnstagedReason | None,
    ) -> None:
        for name in RULE_FIELDS:
            leaf = getattr(rule_fields, name)
            if target is None:
                skip(leaf, f"{path}.{name}", reason or "not_a_planning_field")
            else:
                stage(leaf, f"{path}.{name}", name, **target)

    def unmatched_flag(key: str, staged: Collection[str]) -> str | None:
        """The flag of a target without a geometry id, or None when it must be skipped."""
        if unmatched != "stage":
            return None
        return TARGET_STAGED if key in staged else TARGET_UNMATCHED

    for i, parcel in enumerate(result.urban_parcels):
        path = f"urban_parcels[{i}]"
        number = _printed(parcel.urban_parcel_number)
        key = parcel.parcel_key
        parcel_id = parcel_ids.get(key) if key else None
        reason: UnstagedReason = (
            "parcel_without_number" if key is None else "parcel_not_in_geometry"
        )
        target: dict[str, Any] | None = None
        if key is not None:
            flag = None if parcel_id is not None else unmatched_flag(key, staged_parcels)
            if parcel_id is not None or flag is not None:
                target = {
                    "entity_type": "urban_parcel",
                    "urban_parcel_id": parcel_id,
                    "number": number,
                    "block_ref": _printed(parcel.block_ref),
                    "label": number,
                    "key": key,
                    "target_flag": flag,
                }
        area_path = f"{path}.planned_parcel_area_m2"
        if target is None:
            skip(parcel.planned_parcel_area_m2, area_path, reason)
        else:
            stage(parcel.planned_parcel_area_m2, area_path, "planned_parcel_area_m2", **target)
        rules(parcel.rules, f"{path}.rules", target, reason)

    for i, block in enumerate(result.blocks):
        path = f"blocks[{i}]"
        key = block.block_key
        block_id = block_ids.get(key) if key else None
        reason = "block_without_label" if key is None else "block_not_in_geometry"
        target = None
        if key is not None:
            flag = None if block_id is not None else unmatched_flag(key, staged_blocks)
            if block_id is not None or flag is not None:
                ref = _printed(block.block_ref)
                target = {
                    "entity_type": "block",
                    "block_id": block_id,
                    "block_ref": ref,
                    "label": ref,
                    "key": key,
                    "target_flag": flag,
                }
        rules(block.rules, f"{path}.rules", target, reason)

    if result.document is not None:
        rules(
            result.document.rules,
            "document.rules",
            {"entity_type": "document", "key": DOCUMENT_KEY},
            None,
        )

    missing: Counter[str] = Counter()
    for path, leaf in iter_leaves(result):
        if isinstance(leaf, MissingValue):
            missing[leaf.reason] += 1
        elif path not in staged_paths:
            plan.unstaged.append(Unstaged(path, "not_a_planning_field"))
    plan.missing = dict(missing)
    return plan
