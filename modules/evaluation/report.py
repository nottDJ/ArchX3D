"""
ArchX3D — Evaluation report
===========================
The same evaluation the JSON carries, arranged so a person can check it in
thirty seconds: reference, render, difference overlay, axis scores, findings.

Why a report at all when there is JSON
--------------------------------------
Because a score nobody looks at goes wrong quietly. "Lighting 0.62" is a
number you either trust or ignore; the same number beside the two images it
came from is a claim you can immediately confirm or reject. The overlay exists
for the same reason — it shows *where* the two frames disagree, which is the
one question the numbers cannot answer.

Self-contained by construction
------------------------------
One HTML file with inline CSS, no scripts, no fonts, no CDN. It is opened from
disk, mailed, and attached to bug reports; anything that needs a network to
render would fail in exactly those situations. Images are referenced by
relative path rather than embedded, so the file stays small and the images
stay inspectable on their own.
"""

from __future__ import annotations

import html
import os
from typing import Any, Dict, List, Optional, Sequence

from . import imaging
from .schema import AXES, AxisScore, EvaluationResult, Finding, rank

#: Difference overlays are written here, beside the report.
OVERLAY_DIR = "overlays"


# ---------------------------------------------------------------------------
# Difference overlay
# ---------------------------------------------------------------------------


def write_overlay(reference_path: str, render_path: str, output_path: str) -> str:
    """A heat map of where the two frames disagree, in CIELAB.

    Lab rather than raw RGB difference because the overlay should highlight
    what *looks* different: an RGB difference makes dark regions look
    identical when they are not, and blows up saturated ones that barely read.

    Returns the path written, or ``""`` when it could not be.
    """
    parts = imaging.backend()
    if parts is None:
        return ""
    numpy, Image = parts

    pair = imaging.load_pair(reference_path, render_path)
    if not pair.ok:
        return ""

    difference = numpy.sqrt(
        ((imaging.to_lab(pair.reference) - imaging.to_lab(pair.render)) ** 2).sum(axis=-1)
    )
    # Normalised against a fixed 40 dE rather than the image's own maximum:
    # a per-image scale would make a nearly perfect render look as broken as a
    # badly wrong one, since both would saturate their own range.
    intensity = numpy.clip(difference / 40.0, 0.0, 1.0)

    # Black -> red -> yellow -> white. Monotone in luminance, so severity
    # survives being printed or viewed by someone colour-blind.
    red = numpy.clip(intensity * 3.0, 0, 1)
    green = numpy.clip(intensity * 3.0 - 1.0, 0, 1)
    blue = numpy.clip(intensity * 3.0 - 2.0, 0, 1)
    heat = numpy.stack([red, green, blue], axis=-1)

    # Keep the render faintly visible underneath, so a hot spot can be located
    # in the scene rather than floating in a black frame.
    base = imaging.luma(pair.render)[..., None] * 0.25
    blended = numpy.clip(heat * 0.85 + base, 0.0, 1.0)

    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        Image.fromarray((blended * 255).astype("uint8"), "RGB").save(output_path)
    except (OSError, ValueError):
        return ""
    return output_path


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def write(result: EvaluationResult, config) -> List[str]:
    """Write ``report.html`` and its overlays. Returns the paths written."""
    written: List[str] = []
    overlays: Dict[str, str] = {}

    if config.write_overlays:
        overlay_root = os.path.join(config.output_dir, OVERLAY_DIR)
        for viewpoint in result.viewpoints:
            if not (viewpoint.reference and viewpoint.render):
                continue
            target = os.path.join(overlay_root, f"{_safe(viewpoint.viewpoint_id)}.png")
            path = write_overlay(viewpoint.reference, viewpoint.render, target)
            if path:
                overlays[viewpoint.viewpoint_id] = path
                written.append(path)

    path = os.path.join(config.output_dir, "report.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_html(result, config, overlays))
    written.append(path)
    return written


def render_html(result: EvaluationResult, config, overlays: Dict[str, str]) -> str:
    """The whole report as one string. Pure — no I/O, so it is testable."""
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>ArchX3D — reconstruction evaluation</title>",
        f"<style>{_CSS}</style></head><body>",
        _header(result),
        _building(result),
        _rooms(result),
        _viewpoints(result, config, overlays),
        _footer(result),
        "</body></html>",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(result: EvaluationResult) -> str:
    totals = result.building.totals
    return f"""
<header>
  <h1>Reconstruction evaluation</h1>
  <p class="sub">{_e(result.generated_at)} &middot; schema {_e(result.schema_version)}</p>
  <div class="headline">
    <div class="big {_band(totals.score)}">{totals.score:.2f}</div>
    <div class="headline-meta">
      <div><strong>overall score</strong></div>
      <div>confidence {totals.confidence:.2f} &middot;
           {len(totals.measured_axes)} of {len(AXES)} axes measured</div>
      <div>{len(result.viewpoints)} viewpoint(s) &middot;
           {len(result.rooms)} room(s) &middot;
           {len(result.findings)} finding(s)</div>
    </div>
  </div>
</header>"""


def _building(result: EvaluationResult) -> str:
    summary = result.building
    rows = "".join(_axis_row(summary.axes.get(axis), axis) for axis in AXES)
    pressure = "".join(
        f'<li><span class="tag">{_e(name)}</span>'
        f'<span class="bar"><i style="width:{min(100, value * 100 / max(1e-6, max(summary.subsystem_pressure.values())))}%"></i></span>'
        f'<span class="num">{value:.2f}</span></li>'
        for name, value in sorted(summary.subsystem_pressure.items(),
                                  key=lambda kv: (-kv[1], kv[0]))[:8]
    )
    coverage = summary.coverage or {}
    return f"""
<section>
  <h2>Building</h2>
  <div class="cols">
    <div>
      <table class="axes"><thead><tr><th>axis</th><th>score</th>
        <th>confidence</th><th>notes</th></tr></thead><tbody>{rows}</tbody></table>
      <p class="note">Unmeasured axes are excluded from the score rather than
      counted as zero; {summary.totals.weight_used * 100:.0f}% of the axis
      weight was measurable.</p>
    </div>
    <div>
      <h3>Where the evidence points</h3>
      <ul class="pressure">{pressure or '<li class="empty">no findings</li>'}</ul>
      <h3>Coverage</h3>
      <p class="note">
        {coverage.get('viewpoints_with_reference', 0)} of
        {coverage.get('viewpoints_total', 0)} viewpoints had a reference
        photograph. Passes available:
        {_e(', '.join(coverage.get('passes_available') or []) or 'none')}.
      </p>
    </div>
  </div>
  {_findings_table(summary.findings, "Top findings")}
</section>"""


def _rooms(result: EvaluationResult) -> str:
    if not result.rooms:
        return ""
    rows = []
    for room in result.rooms:
        axes = " ".join(
            f'<span class="chip {_band(room.axes[a].score) if room.axes.get(a) and room.axes[a].measured else "na"}">'
            f'{a[:3]} {room.axes[a].score:.2f}</span>'
            if room.axes.get(a) and room.axes[a].measured
            else f'<span class="chip na">{a[:3]} &mdash;</span>'
            for a in AXES
        )
        rows.append(
            f"<tr><td><strong>{_e(room.room_id)}</strong>"
            f'<div class="muted">{_e(room.room_type or "unknown")} &middot; '
            f'{_e(room.style or "unknown")}</div></td>'
            f'<td class="score {_band(room.totals.score)}">{room.totals.score:.2f}</td>'
            f"<td>{room.totals.confidence:.2f}</td>"
            f"<td>{len(room.viewpoint_ids)}</td>"
            f'<td class="chips">{axes}</td></tr>'
        )
    return f"""
<section>
  <h2>Rooms</h2>
  <table class="rooms"><thead><tr><th>room</th><th>score</th><th>conf.</th>
    <th>views</th><th>axes</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
</section>"""


def _viewpoints(result: EvaluationResult, config, overlays: Dict[str, str]) -> str:
    if not result.viewpoints:
        return '<section><h2>Viewpoints</h2><p class="empty">nothing rendered</p></section>'

    blocks = []
    for viewpoint in result.viewpoints:
        images = _image_row(viewpoint, config, overlays.get(viewpoint.viewpoint_id))
        rows = "".join(_axis_row(viewpoint.axes.get(axis), axis) for axis in AXES)
        notes = "".join(f"<li>{_e(note)}</li>" for note in viewpoint.notes)
        blocks.append(f"""
<article class="viewpoint">
  <h3>{_e(viewpoint.viewpoint_id)}
    <span class="muted">{_e(viewpoint.room)}</span>
    <span class="score {_band(viewpoint.totals.score)}">{viewpoint.totals.score:.2f}</span>
  </h3>
  {images}
  <div class="cols">
    <table class="axes"><thead><tr><th>axis</th><th>score</th>
      <th>confidence</th><th>notes</th></tr></thead><tbody>{rows}</tbody></table>
    <div>
      <p class="note">passes used:
        {_e(', '.join(viewpoint.passes_used) or 'none')}</p>
      {f'<ul class="notes">{notes}</ul>' if notes else ''}
    </div>
  </div>
  {_findings_table(rank(viewpoint.findings), "Findings", compact=True)}
</article>""")
    return f"<section><h2>Viewpoints</h2>{''.join(blocks)}</section>"


def _image_row(viewpoint, config, overlay: Optional[str]) -> str:
    cells = []
    for label, path in (("reference", viewpoint.reference),
                        ("generated", viewpoint.render),
                        ("difference", overlay)):
        if path and os.path.exists(path):
            relative = _relative(path, config.output_dir)
            cells.append(
                f'<figure><img src="{_e(relative)}" alt="{label}" loading="lazy">'
                f"<figcaption>{label}</figcaption></figure>"
            )
        else:
            cells.append(
                f'<figure class="absent"><div class="placeholder">not available</div>'
                f"<figcaption>{label}</figcaption></figure>"
            )
    return f'<div class="images">{"".join(cells)}</div>'


def _axis_row(score: Optional[AxisScore], axis: str) -> str:
    if score is None or not score.measured:
        reason = score.reason if score is not None else "not evaluated"
        return (f'<tr class="na"><td>{_e(axis)}</td><td>&mdash;</td><td>&mdash;</td>'
                f'<td class="muted">{_e(reason)}</td></tr>')
    return (f"<tr><td>{_e(axis)}</td>"
            f'<td class="score {_band(score.score)}">{score.score:.2f}</td>'
            f"<td>{score.confidence:.2f}</td>"
            f'<td class="muted">{_e(_summarise_detail(score))}</td></tr>')


def _summarise_detail(score: AxisScore) -> str:
    """One line of the measurements behind a score, so it can be checked."""
    detail = score.detail or {}
    if score.axis == "colour":
        return (f"cast {detail.get('cast_delta_e', 0):.1f} dE, "
                f"histogram {detail.get('histogram_agreement', 0):.2f}")
    if score.axis == "lighting":
        exposure = detail.get("exposure") or {}
        return (f"luminance {exposure.get('render', 0):.3f} vs "
                f"{exposure.get('reference', 0):.3f}")
    if score.axis == "material":
        saturation = detail.get("saturation") or {}
        return (f"saturation ratio {saturation.get('ratio', 1):.2f}, "
                f"from {detail.get('source', '?')}")
    if score.axis == "layout":
        displacement = detail.get("displacement") or {}
        if displacement.get("measured_objects"):
            return (f"{displacement['measured_objects']} object(s), mean "
                    f"{displacement.get('mean_m', 0) * 100:.0f} cm off")
        return f"mass agreement {detail.get('mass_agreement', 0):.2f}"
    if score.axis == "objects":
        return (f"{detail.get('built', 0)}/{detail.get('observed', 0)} built, "
                f"{len(detail.get('missing') or [])} missing")
    return ""


def _findings_table(findings: Sequence[Finding], title: str,
                    compact: bool = False) -> str:
    if not findings:
        return f'<h4>{_e(title)}</h4><p class="empty">none</p>'
    rows = []
    for finding in findings:
        scope = " ".join(
            filter(None, [finding.room, finding.viewpoint])
        )
        entities = ", ".join(finding.objects + finding.materials)
        rows.append(f"""
<tr class="sev-{_severity_band(finding.severity)}">
  <td><span class="chip axis">{_e(finding.axis)}</span></td>
  <td><strong>{_e(finding.summary)}</strong>
      <div class="why">{_e(finding.why)}</div>
      {f'<div class="remedy">&rarr; {_e(finding.remedy)}</div>' if finding.remedy else ''}
  </td>
  <td class="num">{finding.difference:.2f}{_e(finding.unit)}</td>
  <td class="num">{finding.severity:.2f}</td>
  <td class="muted">{_e(finding.subsystem)}</td>
  <td class="muted">{_e(entities or scope)}</td>
</tr>""")
    return f"""
<h4>{_e(title)}</h4>
<table class="findings"><thead><tr><th>axis</th><th>finding</th>
  <th>diff</th><th>sev.</th><th>subsystem</th><th>affects</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""


def _footer(result: EvaluationResult) -> str:
    notes = "".join(f"<li>{_e(note)}</li>" for note in result.notes)
    metadata = result.metadata or {}
    return f"""
<footer>
  {f'<h4>Limitations of this run</h4><ul class="notes">{notes}</ul>' if notes else ''}
  <p class="muted">evaluation {_e(str(metadata.get('evaluation_version', '')))}
     &middot; {_e(str(metadata.get('pixel_backend', '')))}
     &middot; {metadata.get('duration_ms', 0)} ms</p>
  <p class="muted">The scene graph was not modified. This engine only measures.</p>
</footer>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _relative(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        # Different drives on Windows: an absolute file URL still opens.
        return path.replace("\\", "/")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name)) or "view"


def _band(score: float) -> str:
    if score >= 0.8:
        return "good"
    if score >= 0.6:
        return "fair"
    return "poor"


def _severity_band(severity: float) -> str:
    if severity >= 0.6:
        return "high"
    if severity >= 0.3:
        return "mid"
    return "low"


#: Deliberately plain. A report is read under bad conditions — a laptop in a
#: bright room, a printout, someone else's browser — so the priority is
#: legibility and honest colour coding, not decoration.
_CSS = """
:root{--fg:#1c1e21;--muted:#6b7280;--line:#e3e5e8;--bg:#fff;--panel:#f7f8fa;
--good:#1a7f5a;--fair:#b26a00;--poor:#b3261e;}
@media (prefers-color-scheme:dark){:root{--fg:#e8eaed;--muted:#9aa0a6;
--line:#33363b;--bg:#16181c;--panel:#1e2126;--good:#4ec49a;--fair:#e0a34a;
--poor:#f28b82;}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
max-width:1180px;margin-inline:auto}
h1{font-size:1.6rem;margin:0} h2{font-size:1.2rem;margin:2.5rem 0 .75rem;
padding-bottom:.35rem;border-bottom:1px solid var(--line)}
h3{font-size:1rem;margin:1.5rem 0 .5rem} h4{font-size:.9rem;margin:1.25rem 0 .4rem;
text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.sub,.muted,.note{color:var(--muted)} .sub{margin:.25rem 0 1.5rem;font-size:.9rem}
.note{font-size:.85rem} .empty{color:var(--muted);font-style:italic}
.headline{display:flex;gap:1.25rem;align-items:center;background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:1.25rem}
.big{font-size:2.8rem;font-weight:650;line-height:1;font-variant-numeric:tabular-nums}
.headline-meta{font-size:.9rem;color:var(--muted)}
.headline-meta strong{color:var(--fg)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.78rem;
text-transform:uppercase;letter-spacing:.04em;padding:.4rem .5rem;
border-bottom:1px solid var(--line)}
td{padding:.5rem;border-bottom:1px solid var(--line);vertical-align:top}
td.num,.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.na td{opacity:.65}
.score{font-weight:650;font-variant-numeric:tabular-nums}
.good{color:var(--good)} .fair{color:var(--fair)} .poor{color:var(--poor)}
.chip{display:inline-block;padding:.1rem .45rem;border-radius:99px;font-size:.75rem;
border:1px solid var(--line);margin-right:.25rem;white-space:nowrap}
.chip.na{color:var(--muted)} .chip.axis{background:var(--panel)}
.chips{white-space:nowrap}
.tag{display:inline-block;min-width:11rem;font-size:.82rem}
.pressure{list-style:none;padding:0;margin:.5rem 0}
.pressure li{display:flex;align-items:center;gap:.5rem;margin:.3rem 0;font-size:.85rem}
.bar{flex:1;height:7px;background:var(--panel);border:1px solid var(--line);
border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--fair)}
.images{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem;margin:.75rem 0}
@media(max-width:820px){.images{grid-template-columns:1fr}}
figure{margin:0} figure img{width:100%;height:auto;display:block;
border:1px solid var(--line);border-radius:6px;background:var(--panel)}
figcaption{font-size:.75rem;color:var(--muted);margin-top:.3rem;
text-transform:uppercase;letter-spacing:.04em}
.placeholder{aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;
border:1px dashed var(--line);border-radius:6px;color:var(--muted);font-size:.8rem}
.viewpoint{border:1px solid var(--line);border-radius:10px;padding:1rem 1.25rem;
margin:1rem 0;background:var(--panel)}
.viewpoint h3{display:flex;align-items:baseline;gap:.6rem;margin-top:0}
.why{color:var(--muted);font-size:.83rem;margin-top:.2rem}
.remedy{font-size:.83rem;margin-top:.25rem}
.notes{margin:.4rem 0;padding-left:1.1rem;font-size:.85rem;color:var(--muted)}
tr.sev-high td{border-left:3px solid var(--poor)}
tr.sev-mid td{border-left:3px solid var(--fair)}
tr.sev-high td:not(:first-child),tr.sev-mid td:not(:first-child){border-left:none}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);font-size:.85rem}
"""
