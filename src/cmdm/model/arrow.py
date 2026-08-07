"""Arrow / Polars projection of the canonical model.

The vectorized layer works in Arrow memory: ingestion, normalization, blocking,
comparator scoring and survivorship are all column operations over batches, not
row loops. That only holds together if every stage agrees on the exact physical
schema, which is why the schema is generated from the registry here rather than
being written out by hand in each worker.

Type choices worth stating:

*   Enum-backed columns become ``dictionary(int32, string)``. Group-bys, joins
    and equality filters then compare small integers against a shared
    dictionary instead of comparing strings across millions of rows.
*   Money becomes ``decimal128(18, 4)``. Premiums and sums assured are summed
    and reconciled against carrier ledgers; a float would make those sums
    disagree in the last places and the disagreement would be irreproducible.
*   ``name_tokens`` is a genuine ``list<string>``, not a delimited string, so
    token-set similarity is an array kernel rather than a re-split per pair.
*   UUID columns are ``fixed_size_binary(16)``, half the width of the hex text
    and directly comparable as bytes. The API layer renders them as text at the
    boundary; nothing internal pays for that representation.

``pyarrow`` is imported lazily inside the functions. The registry is imported by
processes that never touch Arrow, and none of them should be made to install it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cmdm.model.enums import LogicalType as LT
from cmdm.model.fields import MONEY_PRECISION, MONEY_SCALE, EntitySpec, FieldSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pyarrow as pa

__all__ = ["arrow_type", "arrow_schema", "polars_schema", "empty_table"]


def arrow_type(field: FieldSpec) -> pa.DataType:
    """Map one field spec to its Arrow type."""
    import pyarrow as pa

    if field.enum_name is not None:
        # Dictionary-encoded: the point of a closed vocabulary is that the
        # column costs an int32 per row regardless of label length.
        return pa.dictionary(pa.int32(), pa.string())

    match field.dtype:
        case LT.STRING:
            return pa.large_string()
        case LT.BOOL:
            return pa.bool_()
        case LT.INT16:
            return pa.int16()
        case LT.INT32:
            return pa.int32()
        case LT.INT64:
            return pa.int64()
        case LT.FLOAT32:
            return pa.float32()
        case LT.FLOAT64 | LT.RATIO:
            return pa.float64()
        case LT.MONEY:
            return pa.decimal128(MONEY_PRECISION, MONEY_SCALE)
        case LT.DATE:
            return pa.date32()
        case LT.TIMESTAMP_TZ:
            # Microseconds in UTC. Every timestamp in the model is an instant;
            # local time is a rendering concern handled at the API boundary.
            return pa.timestamp("us", tz="UTC")
        case LT.UUID:
            return pa.binary(16)
        case LT.LIST_STRING:
            return pa.large_list(pa.large_string())
        case LT.LIST_UUID:
            return pa.large_list(pa.binary(16))
        case LT.JSON:
            return pa.large_string()
        case _:  # pragma: no cover - exhaustive over the enum
            raise ValueError(f"no Arrow mapping for {field.dtype}")


def _field_metadata(field: FieldSpec) -> dict[bytes, bytes]:
    """Attach model metadata to the Arrow field.

    Arrow carries this through Parquet and IPC, so a batch written by ingestion
    and read by a matching worker in another process still knows which columns
    are blocking keys and which are direct PII, without that worker importing
    the registry or being configured separately.
    """
    meta = {
        b"survivorship": field.survivorship.value.encode(),
        b"match_role": field.match_role.value.encode(),
        b"pii": field.pii.value.encode(),
        b"derived": b"true" if field.derived else b"false",
    }
    if field.enum_name:
        meta[b"enum"] = field.enum_name.encode()
    return meta


def arrow_schema(spec: EntitySpec, *, include_lineage: bool = True) -> pa.Schema:
    """Build the Arrow schema for an entity.

    ``include_lineage=False`` yields the staging schema: the business attributes
    without the SCD-2 and audit columns, which are assigned by the golden writer
    inside the transaction rather than carried through the pipeline.
    """
    import pyarrow as pa

    fields = [
        f
        for f in spec.fields
        if include_lineage or f.in_record_hash or f.name == spec.primary_key
    ]
    return pa.schema(
        [
            pa.field(f.name, arrow_type(f), nullable=f.nullable, metadata=_field_metadata(f))
            for f in fields
        ],
        metadata={
            b"entity": spec.name.encode(),
            b"table": spec.table.encode(),
            b"primary_key": spec.primary_key.encode(),
        },
    )


def polars_schema(spec: EntitySpec, *, include_lineage: bool = True) -> dict[str, Any]:
    """Return the equivalent Polars schema mapping.

    Derived from the Arrow schema rather than declared independently, so the two
    representations cannot drift. Polars uses Arrow underneath, making this a
    type translation and not a conversion.
    """
    import polars as pl

    schema = arrow_schema(spec, include_lineage=include_lineage)
    return {f.name: pl.datatypes.convert.dtype_short_repr_to_dtype(str(f.type)) or pl.Object
            for f in schema}


def empty_table(spec: EntitySpec, *, include_lineage: bool = True) -> pa.Table:
    """An empty Arrow table with the entity's schema.

    Used to seed a pipeline stage so that an empty batch still carries the full
    schema downstream. A zero-row batch with no schema forces every consumer to
    special-case emptiness; a zero-row batch with a schema does not.
    """
    import pyarrow as pa

    return pa.Table.from_pylist([], schema=arrow_schema(spec, include_lineage=include_lineage))
