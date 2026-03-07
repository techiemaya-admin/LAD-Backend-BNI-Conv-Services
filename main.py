"""
BNI Conversation Service

WhatsApp AI agent for BNI Rising Phoenix chapter.
Handles member onboarding, 1-to-1 matching, meeting coordination,
reminders, and post-meeting follow-ups.
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
from tasks.reminder_task import send_meeting_reminders
from tasks.followup_task import send_post_meeting_followups
from tasks.icp_followup_task import send_icp_followups

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting BNI Conversation Service")
    try:
        await init_pools()
        logger.info("Database pools initialized")
        await ensure_crm_tables()
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        logger.warning("Service starting without DB — requests will fail")

    # Start background scheduler for reminders and followups
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_meeting_reminders, "interval", minutes=5, id="reminders")
    scheduler.add_job(send_post_meeting_followups, "interval", minutes=15, id="followups")
    scheduler.add_job(send_icp_followups, "interval", minutes=60, id="icp_followups")
    scheduler.start()
    logger.info("Background scheduler started (reminders=5min, followups=15min, icp_followups=60min)")

    yield

    scheduler.shutdown()
    await close_pools()
    logger.info("BNI Conversation Service stopped")


app = FastAPI(
    title="BNI Conversation Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3004",
        "https://*.run.app",
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
