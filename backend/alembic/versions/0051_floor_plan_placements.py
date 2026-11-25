"""Store floor plans + camera floor placements (Heatmap Insights page)

Revision ID: 0051
Revises: 0050

stores.floor_plan_path       — uploaded plan image, served by the API.
cameras.floor_placement_json — where the camera's heatmap sits on the
                               plan ({"x","y","w","h"} normalised 0..1
                               plus "rot" in 0/90/180/270).

Guarded so a re-run is harmless.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set:
    from sqlalchemy import inspect as _inspect
    return {c["name"] for c in _inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "floor_plan_path" not in _cols(bind, "stores"):
        op.add_column("stores", sa.Column("floor_plan_path",
                                          sa.String(255), nullable=True))
    if "floor_placement_json" not in _cols(bind, "cameras"):
        op.add_column("cameras", sa.Column("floor_placement_json",
                                           sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "floor_placement_json" in _cols(bind, "cameras"):
        op.drop_column("cameras", "floor_placement_json")
    if "floor_plan_path" in _cols(bind, "stores"):
        op.drop_column("stores", "floor_plan_path")
