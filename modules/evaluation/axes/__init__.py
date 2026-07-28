"""
ArchX3D — Evaluation axes
=========================
Five independent measurements of one reconstruction. Each is a pure function
of a :class:`evaluation.context.ViewContext`, returning an
:class:`evaluation.schema.AxisScore` and the findings it justifies.

Independent on purpose
----------------------
The axes deliberately do not consult each other's results. A render that is
too dark should lose points on lighting and nothing else; if the darkness also
dragged the colour score down, one problem would show up as two and a
refinement pass would be told to fix the palette when the lamps are at fault.
Where an axis *could* claim ground another owns — a colour cast caused by
lighting, a mass shift caused by a bad camera fit — it names the other
subsystem in the finding rather than scoring it itself.

What each one reads
-------------------
=========  ===========================  =====================================
Axis       Inputs                       Answers
=========  ===========================  =====================================
colour     RGB, albedo, material_id     Are the colours right, and whose
                                        fault is it if not?
material   albedo, RGB, material_id     Do surfaces read as the right
                                        substances?
lighting   RGB, albedo                  Is the room lit like the photograph?
layout     RGB, depth, scene graph      Is everything where the photograph
                                        says, and by how many centimetres?
objects    scene graph only             What was detected but not built?
=========  ===========================  =====================================

Only ``objects`` works without Pillow and numpy — which is also the axis that
needs no images at all. The rest report ``measured=False`` and say why.
"""

from . import colour, layout, lighting, material, objects  # noqa: F401

__all__ = ["colour", "material", "lighting", "layout", "objects"]
