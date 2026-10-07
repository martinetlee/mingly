"""DM history archive controls (backend.telegram.archive)."""

from fastapi import APIRouter

from backend.telegram import archive

router = APIRouter()


@router.get("/status")
async def archive_status():
    return await archive.refresh_status()


@router.post("/start")
async def archive_start():
    archive.start()
    status = await archive.refresh_status()
    await archive.archive._broadcast(force=True)
    return status


@router.post("/pause")
async def archive_pause():
    archive.pause()
    status = await archive.refresh_status()
    await archive.archive._broadcast(force=True)
    return status
