# Vulgarities Bot — image card system v2

## Direction

The Discord image cards use an editorial anime-noir direction: full-bleed art, a quiet graphite content area, restrained dusty-rose accents and DM Sans typography. The visual language is inspired by the clarity and type rhythm of Nekotina's public site, while all artwork and layouts remain original.

## Visual audit resolved

- Removed the outer purple frame and the nested table frame.
- Replaced five competing leaderboard columns with three semantic zones: participant, statistics and total.
- Removed per-row outlines and progress bars; top-three emphasis is now a narrow medal-colored marker.
- Replaced bright gold/silver/bronze row fills with one consistent translucent surface system.
- Reduced rank-card avatar and progress dominance and aligned all values to one grid.
- Replaced Noto Sans as the primary UI face with DM Sans. Noto remains only as a Unicode and emoji fallback.
- Regenerated the levels background with a reserved left content area and the character isolated on the right.

## Constraints

- Leaderboards remain 1600×1200; profile and level-up cards remain 1200×675.
- Successful `/rank`, `/top` and level-up responses contain one PNG attachment and no visible message text.
- Artwork contains no text, logo or copied Nekotina characters.
- Contrast is maintained through the canvas shade, not through enclosing borders.
