from pathlib import Path

import pytest

from app.utils.nvr_inventory import country_for, load_inventory


def test_load_inventory_validates_and_normalises(tmp_path: Path) -> None:
    inventory = tmp_path / "nvrs.csv"
    inventory.write_text(
        "store_name,public_ip,rtsp_port\n"
        " Yaya ,41.90.228.62,8080\n"
        "Kigali Heights,105.178.106.23,800\n",
        encoding="utf-8",
    )

    rows = load_inventory(inventory)

    assert [(row.store_name, row.host, row.port) for row in rows] == [
        ("Yaya", "41.90.228.62", 8080),
        ("Kigali Heights", "105.178.106.23", 800),
    ]
    assert country_for(rows[0].store_name) == "Kenya"
    assert country_for(rows[1].store_name) == "Rwanda"


def test_load_inventory_accepts_separate_http_port_and_brand(tmp_path: Path) -> None:
    inventory = tmp_path / "nvrs.csv"
    inventory.write_text(
        "store_name,public_ip,rtsp_port,http_port,brand\n"
        "Sarit,192.0.2.10,554,443,hikvision\n",
        encoding="utf-8",
    )
    row = load_inventory(inventory)[0]
    assert (row.rtsp_port, row.http_port, row.brand) == (554, 443, "hikvision")


@pytest.mark.parametrize(
    "body",
    [
        "store_name,public_ip,rtsp_port\nA,not-an-ip,80\n",
        "store_name,public_ip,rtsp_port\nA,192.0.2.1,70000\n",
        "store_name,public_ip,rtsp_port\nA,192.0.2.1,80\nA,192.0.2.2,80\n",
        "store_name,public_ip,rtsp_port\nA,192.0.2.1,80\nB,192.0.2.1,81\n",
    ],
)
def test_load_inventory_rejects_unsafe_rows(tmp_path: Path, body: str) -> None:
    inventory = tmp_path / "nvrs.csv"
    inventory.write_text(body, encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        load_inventory(inventory)
