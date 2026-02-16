"""
MedForce Unified Server — Entry Point
"""
import os
import platform
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    if platform.system() == "Windows":
        uvicorn.run("medforce.app:app", host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    else:
        uvicorn.run("medforce.app:app", host="0.0.0.0", port=port, log_level="info")
