"""
Development entry point. Runs the FastAPI backend only.
Start the frontend separately with: cd frontend && npm run dev
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )
