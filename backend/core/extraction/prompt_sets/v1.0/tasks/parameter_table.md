# Task: planning-parameter tables

The pages hold tables of planning parameters with one row per urban parcel. Return one entry in urban_parcels for every row that carries an urban parcel number, keyed by that number exactly as printed, with block_ref from the row.

- A row without an urban parcel number is not a parcel: continuation rows (for example rows marked ***), rows describing single buildings (existing, planned, extension ...) and annex rows. Create no entry for them; when such a row states a rule of its parcel, list it under that parcel's other_conditions.
- A totals row of a block or plan zone goes into blocks with is_total_row true, block_ref as printed in the row, area_m2 and the rule fields that row prints. Leave out the totals row of the whole document.
- Match columns to fields by their headers, using the planning terms. A column that is none of the fields (gross floor area, ground-floor area, dwellings, residents, employees ...) is not returned and is never used to work out a field.
- A field without a column in the table is not_found for every parcel. Do not derive it from other columns.
- A cell that says the parcel's values will be set later (it may span several columns) makes each field it covers deferred, with the cell text as raw_text.
- For a table value, raw_text is the cell text, and table_ref gives the row label (the parcel number as printed) and the column header as printed; when a grid is shown, also the table id and the cell id. When a cell is wrapped over several lines, raw_text joins its lines.
- In columns, list every column header of every table as printed, with the field it maps to or null.
