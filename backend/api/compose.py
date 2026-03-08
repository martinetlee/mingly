from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.telegram.client import get_client

router = APIRouter()


class SendRequest(BaseModel):
    chat_id: int
    text: str
    reply_to_id: int | None = None


@router.post("/send")
async def send_message(req: SendRequest):
    client = get_client()
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        msg = await client.send_message(
            req.chat_id,
            req.text,
            reply_to=req.reply_to_id,
        )
        return {"success": True, "message_id": msg.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
