# MARKEVITA — Static Site (Netlify Drag & Drop)

## Deploy in 60 seconds

1. Unzip → drag the `markevita-static/` folder into Netlify
2. Done. No build step. No CLI.

---

## Layer architecture — Full-bleed hero

```
hero-right (position: relative, overflow: hidden)
│
├── z:0   .hero-placeholder        ← Dev reference. Hidden once lobby loads.
│
├── z:10  .hero-lobby-bg           ← /images/markevita-lobby-background.jpg
│         Full lobby scene.         JPG, no person, desk visible at bottom.
│
├── z:20  .avatar-layer            ← /videos/markevita-avatar-{lang}.webm
│         └─ <video>                Transparent VP9 WebM. Receptionist only.
│         └─ <img fallback>         /images/markevita-avatar-still.png
│
├── z:30  .desk-mask               ← /images/markevita-desk-foreground.png
│         Transparent PNG cutout.   Only desk pixels are opaque.
│         Covers avatar lower body → "standing behind desk" illusion.
│
├── z:35  .hero-overlay-left       ← Left-edge ivory fade (no pointer events)
│         .hero-overlay-bottom     ← Bottom vignette    (no pointer events)
│
└── z:40  .lang-ui                 ← EN · RU · ES language switcher (interactive)
```

---

## Required assets

| Layer | Path | Spec |
|-------|------|------|
| Lobby BG | `/images/markevita-lobby-background.jpg` | 2400×1600px landscape JPG 90q, FULL-BLEED. Room, walls, MARKEVITA logo, lighting, desk. Keep LEFT side calmer for headline legibility; desk+avatar on RIGHT. **NO person.** |
| Avatar still | `/images/markevita-avatar-still.png` | Transparent PNG. Receptionist portrait, waist-up or full body. Used as fallback before video plays. |
| Desk mask | `/images/markevita-desk-foreground.png` | Transparent PNG. Only desk surface + front panel pixels are opaque. Perspective must match lobby BG exactly. Height covers bottom ~32% of panel. |
| Avatar EN | `/videos/markevita-avatar-en.webm` | VP9 WebM with alpha. 1080×1920px, 15–30s. |
| Avatar RU | `/videos/markevita-avatar-ru.webm` | Same spec. |
| Avatar ES | `/videos/markevita-avatar-es.webm` | Same spec. |
| Work img 1 | `/images/work-aurevelle.jpg` | 1200×900px JPG |
| Work img 2 | `/images/work-novara.jpg` | 1200×900px JPG |
| Work img 3 | `/images/work-castellan.jpg` | 1200×900px JPG |

---

## Creating the desk foreground mask

**Option A — Photoshop (most precise):**
1. Open `/images/markevita-lobby-background.jpg`
2. Use pen tool or Select Subject to isolate only the desk surface + front panel
3. Add layer mask → invert to hide everything except the desk
4. Export → PNG-24 with transparency

**Option B — AI image:**
Prompt: `luxury hotel reception desk, dark charcoal stone, front panel and surface only, isolated on transparent background, PNG, matching perspective of a lobby shot`

**Option C — 3D render:**
Render just the desk mesh on a transparent layer from the same camera angle.

**Critical:** The desk mask must match the lobby background **exactly** in perspective, scale, and position. Even a 10px misalignment will break the composite.

---

## Avatar positioning tuning

In `index.html` → `.avatar-layer`:

```css
.avatar-layer {
  bottom: 0;       /* flush to floor; desk mask covers lower body */
  left: 50%;
  transform: translateX(-50%);
  width: 52%;      /* avatar width relative to right panel */
  max-width: 380px;
}
```

And `.desk-mask`:

```css
.desk-mask {
  height: 32%;     /* adjust to match desk's actual height in your lobby image */
}
```

Test by temporarily giving `.desk-mask` a red tint to see its coverage area:
```css
.desk-mask { filter: hue-rotate(180deg) saturate(5); }
```
Remove the filter once positioned correctly.

---

## Avatar video spec (HeyGen / Runway / D-ID)

- Format: **VP9 WebM with alpha channel** — transparent background required
- Resolution: 1080 × 1920px portrait
- Duration: 15–30 seconds per language
- Framing: receptionist from mid-thigh or waist upward (lower body will be hidden by desk mask anyway)
- Export setting: **"Transparent background"** = ON

---

## Behavior

| State | Video | Sound |
|-------|-------|-------|
| Page load | Muted autoplay, loops | Silent |
| Language switch | Swaps src, restarts muted | Silent |
| Concierge button click | Restarts from 0, unmuted | Full greeting audio |
| Greeting ends | Returns to muted loop | Silent |

---

## Adding a 4th language

In `index.html` — JavaScript section:

```js
const AVATAR_VIDEOS = {
  ...existing,
  zh: '/videos/markevita-avatar-zh.webm',
};
const LANG_LABELS = {
  ...existing,
  zh: '中文',
};
```

Add button to `.lang-switcher`:
```html
<span class="lang-sep">·</span>
<button class="lang-btn" data-lang="zh" onclick="switchLang('zh')">ZH</button>
```
