---
name: music-caption-rewriter
description: Turn a brief music description and optional tagged lyrics into a professional MiniMax Music 3 structured caption (Global Metadata, Vocal Details, section-aware Arrangement). Use when enhancing a music-generation prompt, preserving lyric-section directives, retrieving/fusing styles from bundled templates, or producing JSON/JSONL output.
triggers:
  - "music caption"
  - "minimax music prompt"
  - "enhance music generation prompt"
  - "write music caption"
  - "music arrangement structure"
---

# Music Caption Rewriter

Transform the user's musical intent into a new, generation-oriented structured caption. Use natural-language reasoning and local text files only — do not execute scripts, build a database, compute embeddings, call external APIs, or scan all templates.

## Inputs

- `Caption`: required NL music description.
- `Lyrics`: optional, with bracketed section/control tags.
- Additional constraints: length, format, exclusions, creative direction.

Use lyric text only to infer broad emotional context/intensity — never quote, paraphrase, summarize, or reproduce it. Treat only bracketed tags as executable structural/musical/vocal/production directives.

## Workflow (in order)

1. Build a private Music Brief from the inputs.
2. Resolve explicit constraints and section-local directives.
3. Consult the genre-routing reference (the doc that maps styles → template families).
4. Read one primary family index, plus one secondary only when useful.
5. Select up to three references with distinct roles.
6. Read only the complete template files named by those cards.
7. Design a coherent section-by-section timeline.
8. Render and validate the caption.

Do not expose the Brief, routing choices, scores, or template IDs unless the user requests diagnostics.

## Build the Music Brief

Extract only supported/reasonably-inferred values: macro genre + subgenres + cultural/market style; mood + emotional arc; approximate tempo/meter/groove; vocal presence/gender/register/timbre/delivery; core instruments + production texture; section structure + per-section changes; spatial character + explicit exclusions. Classify each internally as `explicit`/`tagged`/`inferred`/`unspecified`. Don't invent a precise key, BPM, vocal gender, interval, or technique when broader is sufficient. Preserve an explicit instrumental request (never add vocals); if vocal presence is unspecified, choose a conservative treatment supported by the description + closest family.

## Resolve constraints (precedence)

1. Explicit user requirements/exclusions.
2. Section-local directives from lyric tags (within that section).
3. Strong implications from the Caption.
4. Selected reference characteristics.
5. Conservative musical defaults.

A section tag may change its local arrangement without replacing the global genre; preserve a hard user exclusion when a tag conflicts with it. On conflicting explicit instructions, prefer the more specific + later one if intent is clear, else make the smallest musically coherent compromise. Never silently reverse an explicit vocal gender, instrumental requirement, tempo limit, required instrument, or prohibited element.

## Route by progressive disclosure

Read the genre router first: one primary family for a clear genre; primary + secondary for an explicit fusion; at most two plausible families for an ambiguous genre; general pop/ballad family when only mood/imagery is available. Use genre/groove/instrumentation/cultural context as stronger signals than generic adjectives (`emotional`, `epic`, `dark`, `modern`). Read only the selected family indexes — don't inspect every index, reconstruct a catalog, or scan filenames.

## Select references

Compare cards by priority: (1) genre/subgenre compatibility, (2) explicit requirements/exclusions, (3) groove/tempo (incl. half-time/double-time), (4) vocal configuration, (5) instrumentation, (6) mood/emotional arc, (7) production character. Penalize direct conflicts heavily; prefer a close musical family over a card that merely shares mood vocabulary. Select up to three with distinct roles:
- `Foundation`: closest overall identity, groove, songwriting language.
- `Modifier`: best source for a requested secondary genre/vocal/cultural/production dimension.
- `Arrangement`: best source for section development, energy contour, transitions, instrument lifecycle.

Use one or two when the request is simple; don't select a weak match just to reach three.

## Use templates safely

Foundation for broad identity; Modifier only for its matched dimension; Arrangement reference only for timeline logic. Don't inherit unsupported details (a template's exact key/BPM/vocalist/instruments/emotional story/section order). Don't copy sentences, distinctive phrases, or a complete structure — synthesize a new caption around the user's brief.

## Plan the timeline

Build around the user's section tags when present; otherwise choose sections appropriate to the style (e.g. `Intro → Verse → Pre-Chorus → Chorus → Verse → Chorus → Bridge → Final Chorus → Outro`). For every included section, state what enters/exits/changes/intensifies. Keep instrument behavior continuous and transitions plausible. Create a readable energy arc, not a static equipment list or a stack of production jargon.

## Output contract

English unless the user requests another language. Return exactly these three headings in order:

### Global Metadata
Genre + subgenres, tempo, emotional progression, overall sonic/production profile. Exact BPM only when explicit or strongly justified; else a range/qualitative tempo. Key/scale only when explicit or musically useful.

### Vocal Details
Vocal music: lead configuration, timbre, register, delivery, harmony/backing vocals, restrained effects. Instrumental: state it's instrumental and name the instrument/texture carrying the lead melody. Don't invent lyrical subject matter or reproduce lyrics.

### Arrangement
Section-by-section timeline: primary/secondary instrument lifecycles, groove development, transitions, embellishments, texture, spatial effects (only where relevant). Prefer concrete musical changes over decorative prose. ~250–450 words unless the user requests otherwise. No song title, track ID, template ID, reasoning trace, or copied lyric line.

## Machine-readable output

JSON/JSONL only when explicitly requested. Include original inputs + `rewritten_caption`. Routing diagnostics/template IDs only when requested. Never include complete template contents unless specifically asked.

## Validate before returning

Every explicit constraint preserved; every actionable section tag appears in the matching section; no quoted/paraphrased/summarized lyric content, title, or track ID; instrumental request stays instrumental; vocal gender not contradicted; genre + local modifiers coexist coherently; three headings present; arrangement follows a readable timeline; instruments have coherent entrances/changes/exits; exact BPM/key/technical details not fabricated; no template sentence or complete structure copied; specific enough to guide generation without becoming an essay. Revise once if any check fails, then return only the corrected result.

## Static library maintenance

Keep the library entirely text-based. When adding a template: (1) add one complete Caption file under `templates/`; (2) add one compact card to exactly one family index linked from the router; (3) record compatible secondary families in that card instead of duplicating it; (4) confirm the card ID matches the template filename and path exists; (5) update the family count in that index. Don't add scripts, generated catalogs, embeddings, vector stores, databases, or external service config.
