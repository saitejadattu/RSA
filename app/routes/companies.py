from fastapi import APIRouter, Query

from app.services.company_service import (
    get_company,
    get_import_summary,
    list_applications,
    list_companies,
    list_repeated_companies,
    list_shortlists,
)


router = APIRouter(prefix="/companies", tags=["Companies"])


@router.get("/")
async def get_companies(
    search: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await list_companies(search=search, company_name=company_name, role=role, limit=limit)


@router.get("/summary")
async def summary(company_name: str | None = None, role: str | None = None) -> dict:
    return await get_import_summary(company_name=company_name, role=role)


@router.get("/repeated")
async def repeated_companies(limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return await list_repeated_companies(limit=limit)


@router.get("/applications")
async def get_applications(
    company_id: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    status: str | None = None,
    is_interested: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await list_applications(
        company_id=company_id,
        company_name=company_name,
        role=role,
        status_value=status,
        is_interested=is_interested,
        limit=limit,
    )


@router.get("/shortlists")
async def get_shortlists(
    company_id: str | None = None,
    company_name: str | None = None,
    role: str | None = None,
    matched_application: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await list_shortlists(
        company_id=company_id,
        company_name=company_name,
        role=role,
        matched_application=matched_application,
        limit=limit,
    )


@router.get("/{company_id}")
async def get_company_by_id(company_id: str) -> dict:
    return await get_company(company_id)
