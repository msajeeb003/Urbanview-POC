# Task: planning-parameter tables

The pages hold tables of planning parameters with one row per urban parcel. Return one entry in urban_parcels for every row that carries an urban parcel number: number exactly as printed, block from the row, and in values the parameters that row states.

- A row without an urban parcel number is not a parcel: continuation rows (for example rows marked ***), rows describing single buildings (existing, planned, extension ...) and annex rows. Create no entry for them and take none of their values into a parcel.
- A totals row of a block or plan zone goes into blocks with is_total_row true, label as printed in the row, and the values that row prints. Leave out the totals row of the whole document.
- Match columns to fields by their headers, using the planning terms. A column that is none of the fields (gross floor area, ground-floor area, dwellings, residents, employees ...) is not returned and is never used to work out a field.
- A field without a column in the table is not listed for any parcel. Do not derive it from other columns.
- A cell that says the parcel's values will be set later (it may span several columns) makes each field it covers deferred, with the cell text as text.
- For a table value, text is the cell text, column the header as printed; when a grid is shown, table is the table id and cell the cell id. When a cell is wrapped over several lines, text joins its lines. number_cell and block_cell are the cell ids of the parcel's number and block.
- In columns, list every column header of every table as printed, with the field it maps to or "none".
