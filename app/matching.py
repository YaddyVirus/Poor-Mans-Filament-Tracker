"""Match the printer-reported external-spool filament to a spool in the DB.

The printer reports whatever the user set when loading (type, color hex,
profile name). We score spools by material compatibility + RGB color
distance and pick the best; a slight bonus keeps the currently active spool
selected on ties (e.g. two identical black PLA+ spools).
"""
import json
import re

MATERIALS = ("PLA+", "PLA-CF", "PETG", "PLA", "ABS", "ASA", "TPU", "PC", "PA", "PVA", "HIPS")


def norm(material):
    return re.sub(r"[^A-Z0-9+]", "", (material or "").upper())


def rgb(hex_color):
    h = (hex_color or "").lstrip("#")[:6]  # printer may append an alpha byte
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def color_distance(a, b):
    ca, cb = rgb(a), rgb(b)
    if ca is None or cb is None:
        return None
    return sum((x - y) ** 2 for x, y in zip(ca, cb)) ** 0.5


def detected_material(detected):
    mat = norm(detected.get("type"))
    if mat:
        return mat
    name = norm(detected.get("name"))  # e.g. "Generic PLA" -> "GENERICPLA"
    for m in MATERIALS:
        if norm(m) in name:
            return norm(m)
    return ""


# Candidates scoring within this much of the best are considered twins:
# too close to tell apart, so the user should verify.
AMBIGUITY_GAP = 15


def find_matches(detected, spools, max_color_dist=90):
    """Return [(score, spool)] for all plausible spools, best first."""
    dmat = detected_material(detected)
    if not dmat:
        return []
    scored = []
    for s in spools:
        if s.get("archived"):
            continue
        smat = norm(s["material"])
        if smat == dmat:
            penalty = 0  # exact material beats prefix match (PLA vs PLA+)
        elif smat.startswith(dmat) or dmat.startswith(smat):
            penalty = 30
        else:
            continue
        dist = color_distance(detected.get("color"), s["color_hex"])
        if dist is None:
            dist = max_color_dist  # color unknown: allow, but rank last
        elif dist > max_color_dist:
            continue
        scored.append((dist + penalty - (5 if s["active"] else 0), s))
    scored.sort(key=lambda t: t[0])
    return scored


def find_matching_spool(detected, spools, max_color_dist=90):
    """Return the best-matching non-archived spool, or None."""
    matches = find_matches(detected, spools, max_color_dist)
    return matches[0][1] if matches else None


def match_result(detected, spools, max_color_dist=90):
    """Full match outcome: best spool, whether it's ambiguous (twin spools),
    and the candidate list for the user to pick from."""
    matches = find_matches(detected, spools, max_color_dist)
    if not matches:
        return {"spool": None, "ambiguous": False, "candidates": []}
    best_score = matches[0][0]
    candidates = [s for score, s in matches if score - best_score < AMBIGUITY_GAP]
    return {
        "spool": matches[0][1],
        "ambiguous": len(candidates) > 1,
        "candidates": candidates,
    }


def detection_signature(detected, candidates):
    """Stable key for one ambiguous situation, so a user confirmation keeps
    holding until the situation actually changes. Keyed on the material and
    the candidate set — not the exact color, which may wobble slightly
    without changing which spools are in the running."""
    return json.dumps({
        "material": detected_material(detected),
        "candidates": sorted(s["id"] for s in candidates),
    }, sort_keys=True)
