from fastapi import APIRouter, Depends, Query

from app.services.admin_company_service import get_admin_company_detail, get_admin_opportunity_detail
from app.services.admin_dashboard_service import get_admin_dashboard, list_admin_students, list_recent_applications
from app.utils.dependencies import require_admin_access


router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin_access)])


@router.get("/dashboard")
async def dashboard() -> dict:
    return await get_admin_dashboard()


@router.get("/applications")
async def applications(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return await list_recent_applications(limit=limit, status_value=status)


@router.get("/students")
async def students(limit: int = Query(default=500, ge=1, le=1000)) -> list[dict]:
    return await list_admin_students(limit=limit)


@router.get("/companies/{company_id}")
async def company_detail(company_id: str) -> dict:
    return await get_admin_company_detail(company_id)


@router.get("/opportunities/{opportunity_id}")
async def opportunity_detail(opportunity_id: str) -> dict:
    return await get_admin_opportunity_detail(opportunity_id)
