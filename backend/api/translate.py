"""Message translation API endpoint."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.ai.translate import translate_message
from backend.llm.base import LLMError

logger = logging.getLogger(__name__)

router = APIRouter()


class TranslateRequest(BaseModel):
    text: str
    target_language: str


class TranslateResponse(BaseModel):
    translation: str


@router.post("")
async def translate(req: TranslateRequest) -> TranslateResponse:
    """Translate a message into the target language using the local LLM."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="No text to translate")

    try:
        result = await translate_message(req.text, req.target_language)
        return TranslateResponse(translation=result)
    except LLMError as e:
        logger.error("Translation failed: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
