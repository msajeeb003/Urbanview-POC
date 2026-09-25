"""Source viewer: one click from a planning value to the page of the document it cites.

Both routes return a short-lived signed URL into the private bucket (``api.services.source``):
the page image when rendered, else the PDF with a ``#page=N`` anchor. The bucket stays private,
object keys never leave the API and the responses are ``Cache-Control: no-store``. 404
``not_found`` only for a document, page or value that truly does not exist; the client emits the
``source_reference_opened`` analytics event itself (``document_id`` and ``page`` are in the body).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Response

from api.deps import SourceServiceDep
from api.schemas.source import SourcePage

router = APIRouter(prefix="/source", tags=["source"])

RESPONSES = {
    404: {"description": "No such document, value, or page (`not_found`; `details.reason`)"},
    503: {"description": "Object storage or the planning database unavailable"},
}


@router.get(
    "/{document_id}/page/{page}",
    response_model=SourcePage,
    summary="Signed, short-lived URL to one page of a stored planning document",
    responses=RESPONSES,
)
async def source_page(
    document_id: Annotated[int, Path(gt=0, description="planning_documents.id")],
    page: Annotated[int, Path(ge=1, description="1-based page of the PDF")],
    service: SourceServiceDep,
    response: Response,
) -> SourcePage:
    response.headers["Cache-Control"] = "no-store"
    return await service.page(document_id, page)


@router.get(
    "/value/{value_id}",
    response_model=SourcePage,
    summary="Signed, short-lived URL to the page a published planning value cites, with its box",
    responses=RESPONSES,
)
async def source_value(
    value_id: Annotated[
        int, Path(gt=0, description="planning_parameter_values.id (Source.value_id)")
    ],
    service: SourceServiceDep,
    response: Response,
) -> SourcePage:
    response.headers["Cache-Control"] = "no-store"
    return await service.value(value_id)
