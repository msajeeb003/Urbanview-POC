# Task: the document

From these pages (title pages, running headers, the adoption decision, the introduction, summary tables), return in values the document's identity and the rules it sets for its whole area, one entry per field that is stated.

- name: the document's title, as printed.
- document_type: a code, one of {{ document_type_codes }}: the type whose name matches the document's own designation. text is the designation as printed.
- status: a code, one of adopted, in_progress, superseded. adopted when the pages show the adoption decision or its publication in the official gazette; in_progress when they call the document a draft or a proposal and show no adoption; superseded when they say it is no longer in force. text is the words that justify the code; leave it out when the pages say nothing about it.
- gazette_reference, decision_number, decision_date: the official gazette reference, and the number and the date of the adoption decision, as printed.
- area_ha: the area the document covers, as printed with its unit.
- amends / amended_by: each document this one amends or is amended by, with that document's name as value.
- The rule fields: limits the document sets for its whole area. Figures that describe the plan as a whole (totals and averages of areas, dwellings, residents, average indices in a summary table) are not rules: give them as note entries.
