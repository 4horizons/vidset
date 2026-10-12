"""Source-video setting presets. Each is an independent edit of the original."""
from pathlib import Path

ENVIRONMENTS = [
    {"id": "golf-course", "name": "Golf course", "eyebrow": "A SLOW AFTERNOON", "description": "Sunlit fairways, soft hills, and a quiet putting green.", "color": "golf",
     "setting": "a photorealistic golf course in warm late-afternoon daylight, with closely cut green grass beneath the subject, a putting green and flag in the middle distance, and rolling fairways and trees behind it"},
    {"id": "festival", "name": "Music festival", "eyebrow": "IN THE CROWD", "description": "Golden-hour festival grounds and a stage in the distance.", "color": "festival",
     "setting": "a photorealistic outdoor music festival at golden hour, with a stable low wooden picnic surface beneath the subject, grass, soft out-of-focus adult festivalgoers, colorful flags, and a distant concert stage behind it; keep people in the background and never obscure the subject"},
    {"id": "courtside", "name": "Courtside seats", "eyebrow": "RIGHT BY THE ACTION", "description": "Polished hardwood, floor seats, and the basketball court.", "color": "courtside",
     "setting": "a photorealistic courtside floor-seat view at a professional-style basketball arena, with the subject resting securely on a flat courtside surface at the edge of polished hardwood, court boundary markings, floor seats, a distant basketball hoop, and softly out-of-focus spectators; no added team names, advertising, readable signs or branded graphics"},
    {"id": "airport", "name": "Airport lounge", "eyebrow": "BEFORE THE NEXT FLIGHT", "description": "A bright terminal window, departure gates, and aircraft.", "color": "airport",
     "setting": "a photorealistic modern airport gate lounge in daylight, with the subject resting on a clean matte counter beside floor-to-ceiling windows, comfortable departure seating, and parked aircraft and the runway beyond the glass; no readable gate signs or new text"},
]

PRESERVE = ("Replace only the setting and supporting surface. Preserve the original main subject, its identity, "
            "shape, colors, labels, branding, proportions, screen position and scale. Preserve the original camera "
            "movement, subject movement, entrances, exits and timing. Do not reveal the subject before it appears "
            "in the original. Keep the framing and use coherent perspective, stable scene geometry and a natural "
            "contact shadow. Adapt illumination only as needed for the setting. Do not add foreground objects, "
            "hands or text. Keep the original audio.")


def options(static_root: Path):
    result = []
    for item in ENVIRONMENTS:
        folder = static_root / "environment-examples"
        value = {key: item[key] for key in ("id", "name", "eyebrow", "description", "color")}
        value["instruction"] = f"Place this video's main subject in {item['setting']}. {PRESERVE}"
        value["poster_url"] = f"/static/environment-examples/{item['id']}.jpg" if (folder / f"{item['id']}.jpg").is_file() else None
        result.append(value)
    return result
