"""
PersonaPlex API Gateway
=======================
A FastAPI wrapper that adds API key authentication in front of the 
PersonaPlex/Moshi WebSocket server and provides a REST endpoint for 
offline (file-based) inference.

Requirements:
    pip install fastapi uvicorn websockets python-multipart aiofiles

Usage:
    1. Configure your API keys and Moshi server address below
    2. Start the PersonaPlex/Moshi server on its default port (8998)
    3. Run this gateway:
         uvicorn api_gateway:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
    4. Connect your clients to this gateway (port 8000) instead of Moshi directly

Architecture:
    Client (your app) --> [API Gateway :8000] --> [Moshi Server :8998]
                              (auth layer)          (model inference)
"""

import os
import json
import uuid
import asyncio
import hashlib
import secrets
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    HTTPException, Depends, Header, UploadFile, File, Form, Query
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import websockets

# =============================================================================
# CONFIGURATION
# =============================================================================

# Moshi/PersonaPlex backend server
MOSHI_HOST = os.getenv("MOSHI_HOST", "localhost")
MOSHI_PORT = int(os.getenv("MOSHI_PORT", "8998"))
MOSHI_WS_URL = f"wss://{MOSHI_HOST}:{MOSHI_PORT}/ws"

# Path to the personaplex repo (for offline inference)
PERSONAPLEX_REPO = os.getenv("PERSONAPLEX_REPO", "/opt/personaplex")

# API Keys - stored as SHA-256 hashes for security
# In production, use a database or secrets manager
API_KEYS_FILE = os.getenv("API_KEYS_FILE", "api_keys.json")

# Rate limiting (requests per minute per key)
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))

# Temp directory for audio file processing
TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/personaplex_gateway")
os.makedirs(TEMP_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("personaplex-gateway")

# =============================================================================
# API KEY MANAGEMENT
# =============================================================================

class APIKeyManager:
    """Manages API keys with hashed storage and basic rate limiting."""

    def __init__(self, keys_file: str):
        self.keys_file = keys_file
        self.keys: dict = {}          # hash -> metadata
        self.rate_tracker: dict = {}   # hash -> list of timestamps
        self._load_keys()

    def _load_keys(self):
        if os.path.exists(self.keys_file):
            with open(self.keys_file, "r") as f:
                self.keys = json.load(f)
            logger.info(f"Loaded {len(self.keys)} API keys")
        else:
            logger.info("No API keys file found, creating empty store")
            self._save_keys()

    def _save_keys(self):
        with open(self.keys_file, "w") as f:
            json.dump(self.keys, f, indent=2)

    @staticmethod
    def _hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    def generate_key(self, name: str, description: str = "") -> str:
        """Generate a new API key. Returns the plaintext key (show once!)."""
        raw_key = f"ppx-{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(raw_key)
        self.keys[key_hash] = {
            "name": name,
            "description": description,
            "created": datetime.now(timezone.utc).isoformat(),
            "active": True,
            "usage_count": 0,
        }
        self._save_keys()
        logger.info(f"Generated API key for: {name}")
        return raw_key

    def validate_key(self, api_key: str) -> Optional[dict]:
        """Validate an API key. Returns metadata if valid, None if not."""
        key_hash = self._hash_key(api_key)
        entry = self.keys.get(key_hash)
        if entry and entry.get("active", False):
            # Rate limiting check
            now = datetime.now(timezone.utc).timestamp()
            window = self.rate_tracker.get(key_hash, [])
            window = [t for t in window if now - t < 60]  # 1-min window
            if len(window) >= RATE_LIMIT_RPM:
                return None  # Rate limited
            window.append(now)
            self.rate_tracker[key_hash] = window

            # Increment usage
            entry["usage_count"] = entry.get("usage_count", 0) + 1
            self._save_keys()
            return entry
        return None

    def revoke_key(self, api_key: str) -> bool:
        key_hash = self._hash_key(api_key)
        if key_hash in self.keys:
            self.keys[key_hash]["active"] = False
            self._save_keys()
            return True
        return False

    def list_keys(self) -> list:
        """List all keys (metadata only, no hashes exposed)."""
        return [
            {"name": v["name"], "created": v["created"],
             "active": v["active"], "usage_count": v.get("usage_count", 0)}
            for v in self.keys.values()
        ]


key_manager = APIKeyManager(API_KEYS_FILE)

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(
    title="PersonaPlex API Gateway",
    description="Authenticated API gateway for NVIDIA PersonaPlex speech-to-speech model",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten this for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# AUTH DEPENDENCY
# =============================================================================

async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> dict:
    """Dependency that validates the API key from the X-API-Key header."""
    meta = key_manager.validate_key(x_api_key)
    if meta is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or rate-limited API key"
        )
    return meta

# =============================================================================
# HEALTH & INFO ENDPOINTS
# =============================================================================

@app.get("/")
async def root():
    return {
        "service": "PersonaPlex API Gateway",
        "version": "1.0.0",
        "endpoints": {
            "websocket_stream": "/ws/stream",
            "offline_inference": "/v1/inference",
            "health": "/health",
            "admin_generate_key": "/admin/keys/generate",
            "admin_list_keys": "/admin/keys",
        }
    }


@app.get("/health")
async def health_check():
    """Check if the Moshi backend is reachable."""
    try:
        async with websockets.connect(
            MOSHI_WS_URL, ssl=True, close_timeout=5,
            additional_headers={}
        ) as ws:
            await ws.close()
        backend_status = "connected"
    except Exception as e:
        backend_status = f"unreachable: {str(e)}"

    return {
        "gateway": "healthy",
        "backend": backend_status,
        "moshi_url": MOSHI_WS_URL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# WEBSOCKET STREAMING ENDPOINT (Real-Time Full-Duplex)
# =============================================================================

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket, api_key: str = Query(...)):
    """
    WebSocket endpoint for real-time full-duplex audio streaming.

    Connect with your API key as a query parameter:
        wss://your-server:8000/ws/stream?api_key=ppx-xxxxx

    Protocol:
        - Send raw audio frames (24kHz, same as Moshi expects)
        - Receive audio frames + text tokens from PersonaPlex
        - All data is proxied bidirectionally to the Moshi backend
    """
    # Validate API key
    meta = key_manager.validate_key(api_key)
    if meta is None:
        await websocket.close(code=4001, reason="Invalid or rate-limited API key")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())[:8]
    logger.info(f"[{session_id}] WebSocket session started for: {meta['name']}")

    backend_ws = None
    try:
        # Connect to Moshi backend
        backend_ws = await websockets.connect(
            MOSHI_WS_URL,
            ssl=True,
            max_size=2**20,  # 1MB max frame
        )
        logger.info(f"[{session_id}] Connected to Moshi backend")

        async def client_to_backend():
            """Forward client audio to Moshi."""
            try:
                while True:
                    data = await websocket.receive_bytes()
                    await backend_ws.send(data)
            except WebSocketDisconnect:
                logger.info(f"[{session_id}] Client disconnected")
            except Exception as e:
                logger.error(f"[{session_id}] Client->Backend error: {e}")

        async def backend_to_client():
            """Forward Moshi responses to client."""
            try:
                async for message in backend_ws:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            except websockets.exceptions.ConnectionClosed:
                logger.info(f"[{session_id}] Backend connection closed")
            except Exception as e:
                logger.error(f"[{session_id}] Backend->Client error: {e}")

        # Run both directions concurrently
        await asyncio.gather(
            client_to_backend(),
            backend_to_client(),
            return_exceptions=True
        )

    except Exception as e:
        logger.error(f"[{session_id}] Session error: {e}")
    finally:
        if backend_ws:
            await backend_ws.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"[{session_id}] Session ended")

# =============================================================================
# REST OFFLINE INFERENCE ENDPOINT (File-Based)
# =============================================================================

@app.post("/v1/inference", dependencies=[Depends(verify_api_key)])
async def offline_inference(
    audio: UploadFile = File(..., description="Input WAV file (24kHz recommended)"),
    voice: str = Form(default="NATF2", description="Voice profile name"),
    persona: str = Form(
        default="You are a helpful assistant.",
        description="Text prompt defining the persona/role"
    ),
):
    """
    REST endpoint for offline (non-realtime) inference.

    Send a WAV file and get a WAV response back.
    Useful for batch processing or simpler integrations 
    that don't need real-time streaming.

    curl example:
        curl -X POST https://your-server:8000/v1/inference \\
            -H "X-API-Key: ppx-xxxxx" \\
            -F "audio=@input.wav" \\
            -F "voice=NATF2" \\
            -F "persona=You are a helpful assistant." \\
            --output response.wav
    """
    request_id = str(uuid.uuid4())[:8]
    input_path = os.path.join(TEMP_DIR, f"{request_id}_input.wav")
    output_path = os.path.join(TEMP_DIR, f"{request_id}_output.wav")
    
    # Optional: If you want to capture stdout/stderr from the process
    # transcript_path = os.path.join(TEMP_DIR, f"{request_id}_transcript.txt")

    try:
        # Save uploaded audio
        content = await audio.read()
        with open(input_path, "wb") as f:
            f.write(content)
        logger.info(f"[{request_id}] Received {len(content)} bytes of audio")

        # Run offline inference via the Moshi CLI
        # NOTE: Ensure PERSONAPLEX_REPO is set correctly for your environment
        cmd = [
            "python", "-m", "moshi.offline",
            "--input", input_path,
            "--output", output_path,
            "--voice", voice,
            "--prompt", persona,
        ]

        logger.info(f"[{request_id}] Running offline inference...")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PERSONAPLEX_REPO,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=300  # 5 min timeout for safety
        )

        if process.returncode != 0:
            logger.error(f"[{request_id}] Inference failed: {stderr.decode()}")
            raise HTTPException(
                status_code=500,
                detail=f"Inference failed: {stderr.decode()[:500]}"
            )

        if not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail="No output generated")

        logger.info(f"[{request_id}] Inference complete, returning audio")

        # Return the output WAV file
        return FileResponse(
            output_path,
            media_type="audio/wav",
            filename=f"personaplex_response_{request_id}.wav",
            headers={
                "X-Request-ID": request_id,
                "X-Transcript": stdout.decode()[:500] if stdout else "",
            }
        )

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Inference timed out")
    finally:
        # Cleanup input file
        if os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception as e:
                logger.warning(f"Could not remove input file: {e}")
        # Cleanup output file - handled by background task usually, but let's leave it for now
        # or rely on temp directory cleaniup strategies. 
        # Ideally, we stream the response and then delete, but FileResponse handles open files. 
        # For simplicity in this script, we rely on OS or periodic cleanup for output files, 
        # or implement BackgroundTasks in FastAPI to delete after response.

# =============================================================================
# ADMIN ENDPOINTS (Key Management)
# =============================================================================
# In production, protect these with a separate admin auth mechanism

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-me-in-production")


def verify_admin(x_admin_secret: str = Header(..., alias="X-Admin-Secret")):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


@app.post("/admin/keys/generate", dependencies=[Depends(verify_admin)])
async def generate_api_key(
    name: str = Form(..., description="Name/label for this key"),
    description: str = Form(default="", description="Optional description"),
):
    """
    Generate a new API key.

    IMPORTANT: The plaintext key is returned ONCE. Store it securely.

    curl example:
        curl -X POST https://your-server:8000/admin/keys/generate \\
            -H "X-Admin-Secret: your-admin-secret" \\
            -F "name=mikes-pbx-app" \\
            -F "description=API key for Project Astro PBX integration"
    """
    raw_key = key_manager.generate_key(name, description)
    return {
        "api_key": raw_key,
        "name": name,
        "message": "Store this key securely — it cannot be retrieved again."
    }


@app.get("/admin/keys", dependencies=[Depends(verify_admin)])
async def list_api_keys():
    """List all API keys (metadata only, keys are not exposed)."""
    return {"keys": key_manager.list_keys()}


@app.post("/admin/keys/revoke", dependencies=[Depends(verify_admin)])
async def revoke_api_key(api_key: str = Form(...)):
    """Revoke an API key."""
    success = key_manager.revoke_key(api_key)
    if success:
        return {"message": "Key revoked"}
    raise HTTPException(status_code=404, detail="Key not found")


# =============================================================================
# STARTUP
# =============================================================================

@app.on_event("startup")
async def startup():
    logger.info("=" * 60)
    logger.info("PersonaPlex API Gateway starting")
    logger.info(f"Backend: {MOSHI_WS_URL}")
    logger.info(f"API Keys loaded: {len(key_manager.keys)}")
    logger.info(f"Rate limit: {RATE_LIMIT_RPM} req/min per key")
    logger.info("=" * 60)

    # Generate a default key if none exist
    if len(key_manager.keys) == 0:
        default_key = key_manager.generate_key(
            "default-admin",
            "Auto-generated default key"
        )
        logger.info(f"No keys found. Generated default key: {default_key}")
        logger.info("Store this key! It won't be shown again.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
