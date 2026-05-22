-- Lyre migration 0006: persona display_name + kind.
--
-- Two columns added to the personas table:
--
--   display_name TEXT — owner-visible label for the persona. Defaults to
--                       NULL on existing rows; runtime falls back to
--                       `name` whenever this is NULL. Edited by the
--                       owner in identity.md frontmatter; on re-seed,
--                       the wizard writes there too. This is THE single
--                       source of truth for "what to show this persona
--                       as in UI / what to call its bootstrap-seeded
--                       agent at seed time".
--
--   kind         TEXT — three-state classifier: 'singleton' / 'seeded' /
--                       'spawn_only'. Drives both the seed step
--                       (singleton + seeded → seed one agent at onboard,
--                       spawn_only → don't) and the create_agent gate
--                       (singleton refuses spawn, the other two allow).
--                       NULL on existing rows is treated as
--                       'spawn_only' so legacy worker personas keep
--                       working without a re-onboard.
--
-- Why these are in personas (the file-backed table) and not in a
-- separate config section: a persona's display name and singleton-ness
-- are facts about the persona's identity, not deployment knobs. Putting
-- them in identity.md frontmatter (mirrored into this table on seed)
-- gives single source of truth.

ALTER TABLE personas ADD COLUMN display_name TEXT;
ALTER TABLE personas ADD COLUMN kind        TEXT;
