from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db import create_collections_and_indexes
from app.db.mongodb_config import health_check as mongodb_health_check
from app.core import auth as auth_core
from app.entlastung_balance import seed_entlastung_2026_balances
from app.core.logging import setup_logging, get_logger

from app.routers import auth, imports, invoicing, patients, analytics, billing, misc

logger = get_logger(__name__)
setup_logging()

# Paths that must remain reachable without a session token.
PUBLIC_PATHS = {"/health", "/auth/login"}

app = FastAPI()


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing bearer token"})

    token = auth_header[len("Bearer "):]
    try:
        auth_core.decode_access_token(token)
    except auth_core.InvalidTokenError:
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})

    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://localhost:5173",      # Vite dev server
        "http://127.0.0.1:5173",      # Vite dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(imports.router)
app.include_router(invoicing.router)
app.include_router(patients.router)
app.include_router(analytics.router)
app.include_router(billing.router)
app.include_router(misc.router)


# ------------------------------
# Startup
# ------------------------------
@app.on_event("startup")
async def startup_event():
    try:
        # Initialize MongoDB collections and indexes
        logger.info("Initializing MongoDB...")
        create_collections_and_indexes()
        logger.info("✓ MongoDB collections initialized")

        # Seed the 2026 Entlastungsleistung bucket for any patient that doesn't
        # have one yet (idempotent — never touches an existing row).
        seed_summary = seed_entlastung_2026_balances()
        logger.info(f"✓ Entlastungsleistung 2026 balances: {seed_summary}")

    except Exception as e:
        logger.error(f"Failed to init database: {e}")


@app.get("/health")
def health():
    mongo_ok = mongodb_health_check()
    return JSONResponse(
        status_code=200 if mongo_ok else 503,
        content={"status": "ok" if mongo_ok else "degraded", "mongodb": mongo_ok},
    )
