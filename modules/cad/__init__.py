"""
ArchX3D — CAD Understanding
===========================
Reads an architectural DXF into a complete, typed, semantically-labelled
document: walls *and* the doors, windows, fixtures, furniture blocks, room
labels, dimensions, hatches, layer conventions, units and north direction that
the drawing also contains.

This package implements trust tiers 1-5 of the project's design philosophy
("never guess if reliable information exists"). The vision layer implements
tier 6. Keeping them in separate packages is what makes the priority explicit
rather than emergent: ``semantic`` asks CAD first and falls back to imagery,
instead of imagery being the only thing that was ever asked.

Typical use::

    from cad import read_dxf

    document = read_dxf("plan.dxf")
    for label in document.room_labels():
        print(label.text, label.room_type, label.insert)

``reader`` owns the only ezdxf dependency; ``schema`` is stdlib-only so a
document can be round-tripped anywhere, including inside Blender's bundled
Python.
"""

from __future__ import annotations

from .schema import (
    SCHEMA_VERSION,
    CadBlockRef,
    CadDimension,
    CadDocument,
    CadHatch,
    CadLayer,
    CadPolyline,
    CadRecord,
    CadSegment,
    CadText,
    DrawingUnits,
    NorthArrow,
    Source,
    make_uid,
    point_in_polygon,
)

__all__ = [
    "SCHEMA_VERSION",
    "CadBlockRef",
    "CadDimension",
    "CadDocument",
    "CadHatch",
    "CadLayer",
    "CadPolyline",
    "CadRecord",
    "CadSegment",
    "CadText",
    "DrawingUnits",
    "NorthArrow",
    "Source",
    "make_uid",
    "point_in_polygon",
    "read_dxf",
]


def read_dxf(path: str, **kwargs) -> CadDocument:
    """Parse a DXF file into a ``CadDocument``.

    Imported lazily so that ``import cad`` stays cheap and stdlib-only for
    consumers that merely need to load a serialised document.
    """
    from .reader import read

    return read(path, **kwargs)
