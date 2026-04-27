from fastapi import APIRouter, Response

from ..observability.metrics import render

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    body, content_type = render()
    return Response(content=body, media_type=content_type)
