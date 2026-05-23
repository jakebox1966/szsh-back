from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import NetPayReference, EmployeeGrowth
from app.schemas import NetPayOut, EmployeeGrowthOut

router = APIRouter(prefix="/api/home", tags=["home"])


@router.get("/net-pay", response_model=list[NetPayOut])
def get_net_pay(db: Session = Depends(get_db)):
    return db.query(NetPayReference).all()


@router.get("/employee-growth", response_model=list[EmployeeGrowthOut])
def get_employee_growth(db: Session = Depends(get_db)):
    return db.query(EmployeeGrowth).all()
