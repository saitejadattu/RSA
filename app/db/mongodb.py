from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config.settings import get_settings


class MongoDB:
    client: AsyncIOMotorClient | None = None
    database: AsyncIOMotorDatabase | None = None


mongodb = MongoDB()


async def connect_to_mongo() -> None:
    settings = get_settings()
    mongodb.client = AsyncIOMotorClient(settings.mongo_uri)
    mongodb.database = mongodb.client[settings.mongo_db_name]


async def close_mongo_connection() -> None:
    if mongodb.client is not None:
        mongodb.client.close()


def get_database() -> AsyncIOMotorDatabase:
    
    if mongodb.database is None:
        raise RuntimeError("MongoDB is not connected")
    return mongodb.database
