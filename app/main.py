import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure backend root is in sys.path so 'app' can be imported anywhere
_backend_dir = str(Path(__file__).resolve().parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("temptext.main")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from app.routes.files import router
    from app.services.storage import cleanup_expired, cleanup_loop, ensure_storage
except ImportError:
    from routes.files import router
    from services.storage import cleanup_expired, cleanup_loop, ensure_storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_storage()

    # Safely attempt an initial cleanup sweep if Supabase credentials are configured
    try:
        await cleanup_expired()
    except Exception as exc:
        logger.warning("Initial cleanup check skipped or failed: %s", exc)

    # Start long-running background cleanup task ONLY in persistent environments (not on Vercel serverless)
    cleanup_task = None
    if not os.environ.get("VERCEL"):
        cleanup_task = asyncio.create_task(cleanup_loop())

    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="TempText API", lifespan=lifespan)

# Determine allowed origins for CORS
allowed_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://localhost:8001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8001",
]

frontend_url_env = os.environ.get("FRONTEND_URL", "").strip()
if frontend_url_env:
    for url in frontend_url_env.split(","):
        cleaned = url.strip().rstrip("/")
        if cleaned and cleaned not in allowed_origins:
            allowed_origins.append(cleaned)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://.*\.vercel\.app$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"message": "TempText API is running", "status": "ok"}
