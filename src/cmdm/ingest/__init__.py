"""Vectorized ingestion: raw policy extracts into canonical staging frames.

The pipeline is landing zone, then shred, and every stage between is a Polars
expression rather than a Python loop:

    raw extract -> source_record (immutable, content-addressed)
                -> Policy / Person / Relationship staging frames

:mod:`cmdm.ingest.mapping` holds the declarative source-to-canonical mapping,
:mod:`cmdm.ingest.normalize` the vectorized normalization kernels, and
:mod:`cmdm.ingest.shred` the policy-grain-to-entity-grain split.
"""
