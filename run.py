"""
Windows-compatible launcher for ZoomScribe.
Run this instead of `uvicorn main:app --reload`
"""
import sys
import asyncio

# Must be set BEFORE uvicorn imports anything
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,   # reload doesn't work with ProactorEventLoop on Windows
        loop="asyncio",
    )