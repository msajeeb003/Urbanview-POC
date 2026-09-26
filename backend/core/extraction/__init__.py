"""AI document extraction: the canonical contract (docs/specs/extraction-contract.md).

AI has one job: read the planning documents so people do not transcribe them. It structures
planning parameters against the document / block / urban parcel model and attaches a source
reference to every value. It never invents a planning value and never does arithmetic; nothing
it produces reaches the map except through expert review and the publish job.

- ``schema``: the canonical models (``SCHEMA_VERSION``), stored payload readers;
- ``fields`` / ``normalise``: field specs, units, numbers, dates, floor notation, land use;
- ``response``: what the model returns (transcription only) and the structured-output schemas;
- ``prompts`` + ``prompt_sets/v<version>/``: versioned prompts (``PROMPT_VERSION``);
- ``pages`` / ``textmatch``: the pages read and the check that every citation is on its page;
- ``validate``: transcription -> contract, with flags and issues; ``staging``: contract ->
  review-queue rows; ``llm``: the model seam (Claude); ``run``: one request end to end;
- ``cases`` / ``sample`` / ``evaluate``: the never-guessed cases, the hand-labelled sample and
  the evaluation (``python -m core.extraction``).
"""

from core.extraction.prompts import PROMPT_VERSION
from core.extraction.schema import SCHEMA_VERSION, ExtractionResult, read_payload

__all__ = ["PROMPT_VERSION", "SCHEMA_VERSION", "ExtractionResult", "read_payload"]
