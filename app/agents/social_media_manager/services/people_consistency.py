"""
Keeping the same people looking like the same people across a generated video.

Faces are where picture and video models fail: each storyboard frame is drawn separately, so a person is
re-invented every scene, and an image-to-video model then melts a small or fast-moving face. Three places
need to agree, so the wording lives here once:

  * the script writers describe each person once, in full, and copy that description into every scene,
    keep faces large, and keep people's motion gentle (PEOPLE_ON_SCREEN_RULES);
  * the frame step is handed the first frame the person appeared in (and the owner's own photo, if any) to
    copy from, not only the previous frame, so a face is not re-drawn from a re-drawn copy (IDENTITY_LOCK);
  * each clip's prompt asks the video model to hold the face steady (with_face_stability).
"""
import re
from typing import Any, Dict, Optional

# Spliced into the script writers' system prompts by .replace(), not str.format(): the prompts contain JSON braces.
PEOPLE_RULES_PLACEHOLDER = "{{PEOPLE_RULES}}"

PEOPLE_ON_SCREEN_RULES = """PEOPLE ON SCREEN — faces are where video models fail, so write for them:
- Keep the cast small: at most two recurring people. Anyone else is a soft, out-of-focus background figure whose
  face is never the subject of a shot.
- The first time a person appears, describe them fully in that scene's video_prompt: approximate age, gender,
  skin tone, face shape, hair (style, length, colour), facial hair, build, and ONE fixed outfit with its colours.
  In EVERY later scene they appear, copy that description word for word — do not paraphrase it, shorten it, or
  change the outfit unless the story needs it.
- Frame people so the face is large, sharp and well lit: a medium shot or close-up, facing the camera or in
  three-quarter view. Avoid wide shots where a face is only a few pixels, extreme profiles, faces turned away, and
  anything covering the face (hands, food, phones, hair, deep shadow).
- Keep a person's motion gentle: a small gesture, a smile, a slow push-in or slow pan. No fast head turns, spins,
  jumps, running or whip-pans on a person, and no crowds of distinct faces — fast motion is what makes faces
  melt and morph."""

# Added to a frame prompt when the frame is drawn from earlier frames / a photo that show the same people.
IDENTITY_LOCK = (
    "IDENTITY LOCK: every person in this frame is the SAME individual as in the identity reference image(s) — the "
    "identical face shape, eyes, nose, mouth, skin tone, hairstyle, facial hair, age, build and outfit. Do not "
    "redesign, beautify, age or blend faces. Faces stay natural, symmetrical and undistorted, with correct eyes, "
    "teeth and hands."
)

FACE_STABILITY = (
    "Any person keeps the exact same face, hairstyle, skin tone and outfit for the whole clip; the face stays "
    "natural, sharp and undistorted, with gentle natural movement and no morphing, warping or flickering."
)

# Words that mean a person is in the shot. Deliberately plain: it only decides whether to add face guidance,
# so a miss costs nothing and a false hit adds one harmless sentence. "model", "host", "they" are left out on
# purpose (3D model, web host, a pronoun for anything).
_PEOPLE = re.compile(
    r"\b(man|men|woman|women|person|people|girl|girls|boy|boys|guy|guys|lady|ladies|kid|kids|child|children|baby|"
    r"customer|customers|staff|worker|workers|chef|cook|owner|founder|friend|friends|family|couple|mother|father|"
    r"mum|mom|dad|teen|teenager|student|presenter|athlete|speaker|he|she|his|her|him|face|faces|smile|smiles|"
    r"smiling|portrait|selfie)\b",
    re.IGNORECASE,
)


def mentions_people(text: Optional[str]) -> bool:
    return bool(text) and _PEOPLE.search(str(text)) is not None


def scene_has_people(scene: Dict[str, Any]) -> bool:
    return mentions_people((scene or {}).get("video_prompt"))


def with_face_stability(prompt: Optional[str]) -> str:
    """The clip prompt, with a sentence asking the video model to hold faces steady when the scene has people."""
    prompt = prompt or ""
    if not mentions_people(prompt):
        return prompt
    return f"{prompt.rstrip()} {FACE_STABILITY}"
