import json
from fastapi import WebSocket


class WebSocketManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        for ws in list(self.connections):
            try:
                await ws.send_text(msg)
            except Exception:
                self.disconnect(ws)


ws_manager = WebSocketManager()
