"""
WhatsApp AI Agent Platform

Multi-tenant WhatsApp AI agent service supporting multiple industry clients.
Each client gets its own conversation flow template (BNI, generic, etc.),
isolated database tables, and per-account AI configuration.

Routing via slug-based webhooks: /webhook/{slug}
Configuration stored in lad_dev.social_whatsapp_accounts.
"""
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db.connection import init_pools, close_pools
from db.crm_tables import ensure_crm_tables
from api.health import router as health_router
from api.webhook import router as webhook_router
from api.conversations import router as conversations_router
from api.followup_settings import router as followup_settings_router
from api.labels import router as labels_router
from api.quick_replies import router as quick_replies_router
from api.notes import router as notes_router
from api.chat_groups import router as chat_groups_router
from api.prompts import router as prompts_router
from api.followups import router as followups_router
from api.ownership import router as ownership_router
from api.admin import router as admin_router
from api.personal_webhook import router as personal_webhook_router
from api.leads import router as leads_router
from services.account_registry import load_accounts, get_accounts_by_flow
from modules.bni import register_bni_flow
from modules.bni.tasks import (
    send_meeting_reminders,
    send_post_meeting_followups,
    send_icp_followups,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting WhatsApp AI Agent Platform")
    try:
        await init_pools()
        logger.info("Database pools initialized")
        await ensure_crm_tables()

        # Register flow templates before loading accounts
        register_bni_flow()

        await load_accounts()
        logger.info("WhatsApp account configs loaded")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        logger.warning("Service starting without DB — requests will fail")

    # Start background scheduler
    scheduler = AsyncIOScheduler()

    # BNI-specific tasks only if BNI accounts exist
    bni_accounts = get_accounts_by_flow("bni")
    if bni_accounts:
        scheduler.add_job(send_meeting_reminders, "interval", minutes=5, id="reminders")
        scheduler.add_job(send_post_meeting_followups, "interval", minutes=15, id="followups")
        scheduler.add_job(send_icp_followups, "interval", minutes=60, id="icp_followups")
        logger.info(f"BNI tasks registered for {len(bni_accounts)} account(s)")

    scheduler.start()
    logger.info("Background scheduler started")

    yield

    scheduler.shutdown()
    await close_pools()
    logger.info("WhatsApp AI Agent Platform stopped")


app = FastAPI(
    title="WhatsApp AI Agent Platform",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3004",
        "https://web.mrlads.com",
        "https://lad-frontend-develop.run.app",
        "https://lad-frontend-stage.run.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(conversations_router)
app.include_router(followup_settings_router)
app.include_router(labels_router)
app.include_router(quick_replies_router)
app.include_router(notes_router)
app.include_router(chat_groups_router)
app.include_router(prompts_router)
app.include_router(followups_router)
app.include_router(ownership_router)
app.include_router(admin_router)
app.include_router(personal_webhook_router)
app.include_router(leads_router)
