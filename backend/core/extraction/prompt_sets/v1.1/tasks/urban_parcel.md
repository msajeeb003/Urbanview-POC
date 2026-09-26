# Task: conditions of urban parcels

The pages state planning conditions for urban parcels in text (for example the urban-technical conditions of each parcel). Return one entry in urban_parcels for every urban parcel the pages state conditions for, keyed by its number exactly as printed.

- Take each field from the sentence that states it for that parcel; text is that phrase.
- Rules stated for a whole block, a group of parcels or the whole plan are not a parcel's own values: do not copy them into each parcel. Rules of a block go into blocks; leave plan-wide rules out.
- Conditions that are none of the fields are other_condition entries, one per condition, as printed.
- Leave columns empty.
