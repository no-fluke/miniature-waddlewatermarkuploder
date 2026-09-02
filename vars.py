# ─── MRBERLIN CONFIG ─────────────────────────────────────────────────────────
import os
from os import environ

API_ID    = int(environ.get("API_ID", "21866171"))
API_HASH  = environ.get("API_HASH", "5788dba8f23fade5edda55948e985f06")
BOT_TOKEN = environ.get("BOT_TOKEN", "")

OWNER  = int(environ.get("OWNER", "1289248746"))
CREDIT = environ.get("CREDIT", "MRBERLIN")

# ─── MONGODB ──────────────────────────────────────────────────────────────────
# Set MONGO_URL as an environment variable, e.g.:
#   mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
MONGO_URL = environ.get("MONGO_URL", "")

# ─── VIMEO WORKER ─────────────────────────────────────────────────────────────
# Cloudflare Worker URL for Vimeo signed URL extraction.
# Deploy worker.js to Cloudflare and set this to your worker's base URL.
# e.g. https://vimeo-extractor.yourname.workers.dev
VIMEO_WORKER_URL = environ.get("VIMEO_WORKER_URL", "")

# ─── LEGACY IN-MEMORY USERS (kept for backward compat, DB is primary) ─────────
TOTAL_USER  = os.environ.get("TOTAL_USERS", str(OWNER)).split(",")
TOTAL_USERS = [int(u) for u in TOTAL_USER]

AUTH_USER  = os.environ.get("AUTH_USERS", str(OWNER)).split(",")
AUTH_USERS = [int(u) for u in AUTH_USER]
if OWNER not in AUTH_USERS:
    AUTH_USERS.append(OWNER)
