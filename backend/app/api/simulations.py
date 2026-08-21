"""Read/trigger the isolated scenario catalogue."""
from fastapi import APIRouter, Depends

from app.deps import get_current_user, require_role
from app.simulation.catalog import SCENARIOS
from app.simulation.runner import run_catalog

router = APIRouter(prefix="/simulations", tags=["simulations"])


@router.get("/catalog")
def catalog(_user=Depends(get_current_user)):
    return {"execution_mode": "isolated_simulation", "scenarios": SCENARIOS}


@router.post("/run")
def run(_user=Depends(require_role("admin"))):
    result = run_catalog()
    return {**result, "warning": "Simulation pass rate is not production alert precision or recall."}
