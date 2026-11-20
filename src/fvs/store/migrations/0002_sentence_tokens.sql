-- WP2: each sentence keeps its parsed markup as token groups, so spoken words can be
-- mapped back to caption words ({display|spoken} pairs, *emphasis*) after the voice is made.
-- [{"caption": "1,000", "spoken": "one thousand", "emph": false}, ...]
ALTER TABLE sentences ADD COLUMN tokens TEXT NOT NULL DEFAULT '[]';
