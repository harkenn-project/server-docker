# The assistant's screen

Along with its spoken answer, the assistant can put something on the speaker's round 240x240 touch screen: a
picture, with buttons, text and overlays on top. Tapping a button sends its label back to the assistant as the
person's next turn, so "Which room? [Kitchen] [Bedroom]" works by voice or by touch. Tapping anywhere else closes
the screen.

Switch it on or off on the **Voice** page ("The assistant may show pictures and buttons…"). The **Screen** page
has a box to try screens on a speaker, your custom overlays, and the exact instructions the assistant gets.

## How the assistant asks for it

It adds a marker anywhere in its reply. The marker is never spoken:

```
Which room? [[screen {"overlays": [{"type": "button", "label": "Kitchen"}, {"type": "button", "label": "Bedroom"}]}]]
```

Everything inside is JSON, and every field is optional:

| Field | |
|---|---|
| `image` | A picture URL (`https://…` or `data:`; PNG, JPEG, WebP, or GIF, which stays animated), or an SVG document the assistant writes itself (`<svg viewBox='0 0 240 240'>…</svg>`). SVG animation isn't played: a list of SVGs or URLs (up to 24) is played as frames instead. |
| `frame_ms` | How long each frame of such a list shows: 150 ms by default. |
| `fit` | `cover` (default: fills the screen) or `contain`. |
| `background` | A color behind everything; the speaker's theme background by default. |
| `overlays` | Drawn on top, in order. See below. |
| `dismiss` | `tap` (default): a tap outside the buttons closes the screen. `none`: only a labelled tap or the time limit does. |
| `seconds` | How long it stays up: 60 by default, at most 600. |

### Overlays

Each overlay has a `type` and an optional box: `x`, `y`, `w`, `h` in pixels (0,0 is the top left, the centre is
120,120, and the corners are cut off by the round screen).

| Type | |
|---|---|
| `button` | `label`, `style` (`primary`, `secondary`, `danger`). Without `x`/`y`, up to four buttons are laid out near the bottom. |
| `text` | `text`, `size` (`sm`, `md`, `lg`, `xl`), `color`, `background`, `align` (`left`, `center`, `right`). |
| `area` | `label`: an invisible tappable area over part of the picture (a room on a floor plan, say). |
| `svg` | `svg`: an SVG document drawn into the box. |
| `image` | `url`: a picture drawn into the box. |
| *custom* | One of your own overlays (below), by name, with its parameters as fields. |

Buttons and areas are always tappable; any other overlay is too when it has a `label`. Colors are `#rrggbb` or the
speaker's theme colors: `background`, `surface`, `primary`, `accent`, `text`, `muted`.

A tap on a labelled overlay reaches the assistant as `(tapped "Kitchen" on the screen)`, in the same conversation.
When an agent is waiting for an answer through the MCP server (a ring), the tap is passed to the agent instead.

### Custom overlays

On the **Screen** page, save an SVG template with `{{name}}` placeholders, a default box and a description. The
assistant is told about each one and fills in the placeholders. Values are inserted as text, so they can't add
markup:

```
name: weather_card   box: 40, 40, 160 x 80   description: Today's weather: temp (°C) and summary
<svg viewBox="0 0 160 80" xmlns="http://www.w3.org/2000/svg">
  <rect width="160" height="80" rx="16" fill="#1a2240"/>
  <text x="80" y="38" font-size="28" text-anchor="middle" fill="#ffd166">{{temp}}°</text>
  <text x="80" y="64" font-size="14" text-anchor="middle" fill="#e8ecff">{{summary}}</text>
</svg>
```

The assistant would use it as `{"type": "weather_card", "temp": "18", "summary": "Light rain"}`.

## Limits and safety

* Harkenn fetches picture URLs itself, up to 5 MB. Longer animations keep 48 frames spread over their length, and
  fewer still when the speaker couldn't hold them all; they play for as long as the original.
* A picture link in a reply without a marker is shown anyway.
* SVGs can't reference outside files or addresses: only inline `data:` pictures and their own `#ids`.
* An overlay that can't be drawn is skipped and noted in the speaker's log; the rest of the screen still shows.
* Allowing screens raises the assistant's answer limit to 3000 tokens, so it has room for SVGs.
