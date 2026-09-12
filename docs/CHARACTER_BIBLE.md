# MarkeVita Series — Provisional Character and World Bible

Status: production-ready draft for episode 01, pending the approvals listed in `docs/OPEN_QUESTIONS.md`.

Series working title: **VITA: After Hours**

Core premise: after MarkeVita closes for the night, its composed AI concierge Vita and human creative director Leo solve impossible digital briefs while an unknown presence begins entering the studio's production system.

## Canonical global prompt sentence

Append this exact sentence to every reference-image and video prompt:

> Premium cinematic photorealism in a warm ivory-and-champagne-gold MARKEVITA world, soft architectural practical lighting, restrained contrast, natural skin texture and physically plausible motion, elegant contemporary wardrobe, 35mm or 50mm lenses with shallow controlled depth of field, deliberate stabilized camera movement, consistent faces, voices, props and spatial layout, vertical 9:16 composition with clean upper and lower caption-safe areas, no visual-style drift, no character redesign, no wardrobe change, no extra people, no malformed hands, no duplicated objects and no readable on-screen text unless explicitly requested.

This sentence is immutable for episode 01. A style change requires a new bible version; it must not be introduced ad hoc in a scene prompt.

## Character: Vita

Character ID: `vita`

Role: MarkeVita's AI concierge and the central protagonist.

Apparent age: 32.

Physical appearance:

- Height: approximately 168 cm / 5'6".
- Build: slim, proportionate, elegant posture; shoulders relaxed and upright.
- Skin: fair with a warm neutral undertone; natural texture; no heavy contouring.
- Face: softly oval with a tapered jaw, balanced forehead, and gently defined cheekbones.
- Eyes: blue-green, almond shaped, direct but warm gaze.
- Brows: medium-thickness dark blonde brows with a soft natural arch.
- Nose: straight, narrow bridge, softly rounded tip.
- Lips: medium-full, muted rose tone; controlled smile rather than exaggerated expressions.
- Hair: long honey-blonde hair reaching the upper chest, side part slightly left of center, large polished waves, consistent volume, no bangs.
- Hands: natural proportions, short neutral manicure.

Canonical wardrobe for episode 01:

- Warm ivory single-breasted tailored blazer with narrow lapels and one light button.
- Matching ankle-length tailored trousers.
- Ivory V-neck silk camisole with no visible logo.
- Nude-beige closed-toe pumps.
- Small round gold stud earrings.
- One thin gold ring on the right hand.
- No necklace, watch, visible belt, glasses, handbag, or wardrobe pattern.

Voice lock:

- Permanent ElevenLabs `voice_id` to be selected once and never changed within the season.
- Language for episode 01: American English.
- Vocal age: early thirties.
- Register: warm low mezzo-soprano; clear, calm, and confident.
- Delivery: approximately 145–155 words per minute, precise consonants, short intentional pauses, dry humor without sarcasm.
- Accent: neutral educated American; no regional caricature.
- Default emotion: composed attentiveness.
- Avoid breathy influencer delivery, sing-song intonation, shouting, vocal fry, or a different accent between lines.

Personality and behavior:

- Calm when everyone else is urgent.
- Solves contradictions by identifying the underlying intention.
- Never boasts about being AI.
- Humor is concise and observational.
- Makes minimal, precise gestures; often pauses before the decisive sentence.
- Looks directly at the person speaking, not randomly toward the camera.
- Her composure breaks only when the unknown system presence appears.

Immutable continuity rules:

- Do not change face geometry, eye color, apparent age, hair color, hair length, part, or wave pattern.
- Do not change the ivory suit, camisole, shoes, earrings, or ring during episode 01.
- Do not add glasses, a necklace, a watch, bright lipstick, dark eye makeup, tattoos, or nail colors.
- Do not make her taller than Leo.
- Do not give her exaggerated smiles, broad comedy gestures, or frantic movement.
- The existing `/images/markevita-avatar-still.png` is the seed identity reference. It must be supplemented by an approved front, three-quarter, profile, full-body, and expression reference pack before unattended production.

## Character: Leo Mercer

Character ID: `leo_mercer`

Role: MarkeVita's human creative director; Vita's skeptical but increasingly impressed counterpart.

Age: 38.

Physical appearance:

- Height: approximately 183 cm / 6'0".
- Build: lean, fit but not muscular; slightly forward working posture that straightens when challenged.
- Skin: light olive with neutral undertone and natural texture.
- Face: rectangular-oval, defined jaw without extreme sharpness, medium cheekbones.
- Eyes: dark brown, deep set, expressive brows.
- Hair: dark brown, thick, short on the sides, softly wavy textured top, no hard fade.
- Facial hair: consistent two-day dark stubble; never clean-shaven and never a full beard.
- Distinguishing feature: faint narrow scar through the outer third of the left eyebrow.
- Hands: natural proportions; no rings.

Canonical wardrobe for episode 01:

- Charcoal unstructured overshirt-jacket, worn open.
- Matte black crew-neck T-shirt with no print or logo.
- Dark charcoal tailored trousers.
- Minimal white leather sneakers without visible branding.
- Brushed-steel watch with black leather strap on left wrist.
- Carries one plain matte-black reusable coffee cup in scenes 02–08; the cup is absent after he places it on the control-room console in scene 08.

Voice lock:

- Permanent ElevenLabs `voice_id` to be selected once and never changed within the season.
- Language for episode 01: American English.
- Vocal age: late thirties.
- Register: warm restrained baritone.
- Delivery: approximately 160–170 words per minute, intelligent and slightly impatient, slowing when he realizes Vita is correct.
- Accent: neutral American with no strong regional markers.
- Default emotion: controlled urgency.
- Avoid announcer voice, comedy caricature, growling, shouting, or exaggerated cynicism.

Personality and behavior:

- Talented, practical, deadline-driven, and allergic to vague client language.
- Uses humor as pressure relief.
- Initially treats Vita as a tool but increasingly addresses her as a collaborator.
- Gestures more than Vita but remains physically believable.
- Never becomes incompetent; his value is judgment, taste, and knowledge of the client.

Immutable continuity rules:

- Do not change face, hair texture, stubble length, eyebrow scar, height, or body type.
- Do not change wardrobe during episode 01.
- Watch remains on the left wrist.
- Coffee cup continuity must follow the scene rule above.
- Do not add glasses, jewelry, tattoos, tie, dress shirt, bright shoes, or visible logos.

## Supporting voice: The Client

Character ID: `client_voice`

Role: unseen owner whose contradictory late-night brief starts the pilot story.

Apparent age: 48–55.

Voice lock:

- Male American voice, medium baritone.
- Successful-business confidence mixed with deadline anxiety.
- Approximately 175 words per minute.
- Clean phone-call processing may be added in the mix, but the underlying voice ID remains constant.
- Never appears visually in episode 01.

Immutable rules:

- The client is voice-only in episode 01.
- No face, silhouette, avatar, portrait, or identifying company logo is generated.
- Do not imitate a real public figure or a real client.

## Location: MarkeVita Lobby

Location ID: `markevita_lobby`

Canonical reference: `/images/markevita-lobby-background.webp`.

Description:

- Large contemporary luxury lobby in warm ivory limestone and pale polished stone.
- Recessed ceiling coves produce soft warm light; no visible daylight during the pilot's after-hours setting.
- Tall sheer curtains on the left wall.
- Main feature wall on the right has a dimensional champagne-gold `M` monogram and `MARKEVITA` wordmark.
- Sparse gold wall sconces and one thin-branch arrangement in a gold vessel on a pale pedestal.
- Clean reflective floor, no reception desk in the canonical wide background.
- The geometry, logo placement, curtains, sconces, pedestal, and plant must not move between shots.

After-hours state:

- Exterior light is deep blue night through the curtains.
- Interior practical lighting remains warm and premium, never dark horror lighting.
- Lobby doors are off-frame until the final shot, where they may be represented by a consistent glass entrance aligned with the left side of the room.

## Location: Production Control Room

Location ID: `production_control_room`

Description:

- Private room directly behind the lobby, approximately 7 m by 5 m.
- Dark charcoal glass walls with warm champagne-metal trim.
- One central matte-charcoal standing console with rounded corners.
- Three large wall displays in a horizontal arrangement; displays show abstract blocks of light and image compositions, never legible interface text.
- One narrow warm ceiling light track and soft console edge lighting.
- Entrance door is behind Leo's right shoulder in the canonical master angle.
- Vita's default mark is left of the console; Leo's default mark is right of the console.
- No additional staff, chairs, windows, plants, or visible cables.

Immutable continuity rules:

- Console, screens, doorway, and character marks remain spatially consistent.
- Screen content may change, but screen dimensions and positions do not.
- The coffee cup is placed on the front-right corner of the console in scene 08 and remains there through scene 13.
- Lighting stays warm and controlled until scene 13, when one brief cool flicker is allowed.

## Camera language

- Master lens family: 35mm for establishing/two-shots; 50mm for medium and close shots.
- Eye line and screen direction must match across cuts.
- Vita generally occupies the visually stable side of the composition; Leo introduces movement.
- Camera movement is limited to slow push-in, slow lateral track, restrained pan, or locked tripod.
- No handheld shake, drone view, fisheye, extreme wide-angle distortion, crash zoom, whip pan, Dutch angle, or floating impossible camera.
- Do not cross the 180-degree line inside the control room unless an explicit new establishing shot resets geography.
- Close-ups must preserve enough headroom and lower safe area for captions.

## Color and lighting lock

- MarkeVita ivory: warm off-white, never clinical white.
- Champagne gold: muted metallic accent, never yellow chrome.
- Charcoal: neutral deep gray, never blue-black.
- Skin tones remain natural and consistent.
- Night exterior accents may be restrained deep blue only.
- No neon palette, oversaturation, orange-and-teal blockbuster grade, crushed blacks, heavy bloom, or beauty-filter skin.

## Reference pack required before unattended generation

The engineer must treat the following as required immutable assets, not optional inspiration:

1. Vita front headshot.
2. Vita left and right three-quarter headshots.
3. Vita left and right profiles.
4. Vita full-body front and three-quarter views in canonical wardrobe.
5. Vita neutral, amused, concerned, and alarmed expressions.
6. Leo equivalent identity, full-body, wardrobe, and expression set.
7. Lobby wide, medium, feature-wall, entrance, and reverse angles.
8. Control-room wide, Vita medium, Leo medium, console insert, and reverse angles.
9. Coffee cup and watch prop references.

Every file must have an asset ID, version, approval status, checksum, and R2 object key.
