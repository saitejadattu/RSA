from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo
from app.routes import auth, companies, students


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_to_mongo()
    await create_indexes()
    yield
    await close_mongo_connection()


settings = get_settings()

app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(students.router)
app.include_router(companies.router)


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok"}
