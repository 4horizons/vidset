"""An intentionally illustrated deterministic fixture. No AI calls or faux model output."""
import math
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS, DURATION = 960, 540, 24, 12


def font(size):
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def scene(t, edited=False, instruction=""):
    im = Image.new("RGB", (WIDTH, HEIGHT), "#dce8e2")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 315, WIDTH, 390), fill="#afbd96")
    d.rectangle((0, 390, WIDTH, HEIGHT), fill="#e9e2d3")
    d.rectangle((0, 471, WIDTH, HEIGHT), fill="#c4c3bb")
    for x, color in ((10, "#e5cfb5"), (290, "#c8d7d0"), (750, "#d9ba9e")):
        d.rectangle((x, 185, x + 220, 350), fill=color)
        d.polygon(((x - 12, 185), (x + 110, 100), (x + 232, 185)), fill="#727f75")
        for wx in (x + 30, x + 143):
            d.rectangle((wx, 219, wx + 43, 270), fill="#f2efe4", outline="#86978f", width=4)
            d.line((wx + 22, 220, wx + 22, 270), fill="#86978f", width=3)
        d.rectangle((x + 89, 279, x + 132, 350), fill="#899f95")
    d.rectangle((595, 169, 619, 363), fill="#8e8265")
    d.line((608, 210, 679, 178), fill="#8e8265", width=13)
    for cx, cy, r in ((565, 139, 67), (631, 109, 75), (685, 139, 63), (619, 164, 69)):
        d.ellipse((cx-r, cy-r, cx+r, cy+r), fill="#758c68")
    if edited and "swing" in instruction.lower():
        sway = math.sin(t * 1.5) * 8
        d.line((651, 193, 651+sway, 300), fill="#544a39", width=3)
        d.line((695, 176, 695+sway, 300), fill="#544a39", width=3)
        d.rounded_rectangle((643+sway, 296, 703+sway, 305), 3, fill="#c39458")
    hydrant = "#dfbc39" if edited and any(s in instruction.lower() for s in ("yellow", "hydrant")) else "#ba614c"
    d.rounded_rectangle((767, 350, 791, 401), 6, fill=hydrant)
    d.rounded_rectangle((755, 365, 802, 377), 5, fill=hydrant)
    d.ellipse((764, 341, 794, 359), fill=hydrant)
    d.rectangle((761, 397, 797, 405), fill=hydrant)
    # Walker pauses over the tool at 4–5 seconds, then carries it to the right.
    x = 80 + min(t, 4) * 53 + max(0, t - 5) * 61
    phase = math.sin(t * 9) * 12 if t < 4 or t > 5 else 0
    bending = math.sin((t-4)*math.pi) * 32 if 4 <= t <= 5 else 0
    d.ellipse((x-25, 443, x+48, 457), fill="#b9b7aa")
    d.line((x+6, 410, x-9+phase, 449), fill="#344540", width=10)
    d.line((x+6, 410, x+20-phase, 449), fill="#344540", width=10)
    d.line((x+6, 363+bending, x+6, 413), fill="#395d55", width=25)
    d.ellipse((x-9, 335+bending, x+21, 365+bending), fill="#be997d")
    d.line((x+10, 373+bending, x+32, 408+bending/2), fill="#be997d", width=8)
    dog_x = x+99
    d.line((x+32, 404, dog_x, 421), fill="#665b4f", width=2)
    d.ellipse((dog_x-20, 416, dog_x+32, 439), fill="#b89568")
    d.ellipse((dog_x+21, 407, dog_x+45, 433), fill="#b89568")
    d.polygon(((dog_x+20, 410), (dog_x+26, 428), (dog_x+33, 410)), fill="#8f714f")
    for xx in (dog_x-8, dog_x+21):
        d.line((xx, 431, xx+phase/3, 449), fill="#8f714f", width=5)
    d.line((dog_x-16, 421, dog_x-31, 408), fill="#b89568", width=6)
    tx, ty = (326, 440) if t < 5 else (x+35, 408)
    d.line((tx, ty, tx+22, ty-15), fill="#c99552", width=5)
    d.line((tx+14, ty-23, tx+28, ty-8), fill="#69736d", width=7)
    d.rounded_rectangle((25, 24, 239, 56), 6, fill="#253b32")
    d.text((37, 31), "ILLUSTRATED DEMO · 12s", font=font(15), fill="#e8f0dc")
    d.text((28, 492), "NEIGHBORHOOD WALK", font=font(21), fill="#414e46")
    d.text((816, 494), f"00:{int(t):02d}.{int(t % 1 * 10)}", font=font(18), fill="#414e46")
    return im


def render(destination: Path, start=0.0, end=DURATION, instruction=""):
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = round((end - start) * FPS)
    command = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
               "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for i in range(count):
            proc.stdin.write(scene(start+i/FPS, bool(instruction), instruction).tobytes())
        proc.stdin.close()
        proc.wait(timeout=60)
        if proc.returncode:
            raise RuntimeError("Unable to render the illustrated demo with FFmpeg.")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def analysis():
    return {"summary": "An illustrated walker and dog pass houses, a tree, and a red hydrant. The walker pauses, picks up a tool at 5 seconds, and carries it to the right.",
            "events": [{"start": 0, "end": 4, "label": "Walking toward the tool", "description": "A person walks a dog toward a tool on the sidewalk."},
                       {"start": 4, "end": 5, "label": "Tool pickup", "description": "The walker bends; the tool is in hand at 5.0 seconds."},
                       {"start": 5, "end": 12, "label": "Walking with the tool", "description": "The person carries the tool past the tree and red hydrant."}],
            "limitations": "Scripted fixture metadata, not AI analysis. The demo has no audio.", "provider": "local", "model": "Scripted demo"}


def answer(question):
    lower = question.lower()
    if any(x in lower for x in ("tool", "pick", "carry")):
        answer = "The walker bends down during 00:04–00:05. At 00:05, the tool moves from the sidewalk into their hand, and they carry it for the rest of the clip."
        stamps = [(4.0, "Before pickup: tool on the sidewalk"), (5.25, "After pickup: tool in hand")]
        selected = {"start": 4.0, "end": 6.0}
    elif any(x in lower for x in ("change", "beginning", "end", "difference")):
        answer = "At the beginning the walker is on the left and the tool is on the ground. At the end the walker is on the right carrying the tool. The dog follows, while the houses, tree, and red hydrant remain in place."
        stamps, selected = [(0.0, "Beginning"), (11.5, "End")], None
    elif any(x in lower for x in ("hydrant", "tree", "swing")):
        answer = "The red hydrant and the tree are visible in the background throughout the demo. There is no swing. A 00:06–00:10 selection is a useful place to preview a yellow hydrant and a swing."
        stamps, selected = [(6.0, "Tree and red hydrant"), (9.5, "Walker passes the background")], {"start": 6.0, "end": 10.0}
    else:
        answer = "This is a scripted demo: a walker and dog pass houses, a tree, and a red hydrant, picking up a tool at 00:05. Try asking when the tool is picked up, what changes, or about the hydrant. Upload your own clip and connect a model for open-ended questions."
        stamps, selected = [(0.0, "Opening scene"), (5.25, "Tool carried in hand")], None
    return {"answer": answer, "evidence": [{"timestamp": t, "caption": c} for t,c in stamps],
            "suggested_range": selected, "provider": "local", "model": "Scripted demo",
            "limitations": "Scripted response for the illustrated sample; no model was called."}
