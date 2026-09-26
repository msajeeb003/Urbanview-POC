# Task: the document

From these pages (title pages, the adoption decision, the introduction, summary tables), return the document's identity and the rules it sets for its whole area.

- name: the document's title, as printed.
- document_type: a code, one of {{ document_type_codes }}: the type whose name matches the document's own designation. raw_text is the designation as printed.
- status: a code, one of adopted, in_progress, superseded. adopted when the pages show the adoption decision or its publication in the official gazette; in_progress when they call the document a draft or a proposal and show no adoption; superseded when they say it is no longer in force. raw_text is the words that justify the code; not_found when the pages say nothing about it.
- gazette_reference, decision_number, decision_date: the official gazette reference, and the number and the date of the adoption decision, as printed.
- area_ha: the area the document covers, as printed with its unit.
- amendments: each document this one amends (relation amends) or is amended by (relation amended_by), with that document's name as printed.
- rules: limits the document sets for its whole area. Figures that describe the plan as a whole (totals and averages of areas, dwellings, residents, average indices in a summary table) are not rules: list them under notes.
