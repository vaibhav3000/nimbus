"""MS2 Task 7: equipment that sits in a place (spare pumps in a ward) can be moved into a store."""

import pytest

from cloud_helpers import make_orch


def test_place_seed_and_move_conserve_the_total():
    f = make_orch().fleet
    assert f.place_stock("2C") == {"infusion_pump": 2}
    before = f.clusters["west-wing"]["stock"]["infusion_pump"]
    assert f.move_stock("2C", "west-wing", "infusion_pump", 1)
    assert f.place_stock("2C") == {"infusion_pump": 1}
    assert f.clusters["west-wing"]["stock"]["infusion_pump"] == before + 1


def test_cannot_move_more_than_the_place_holds():
    f = make_orch().fleet
    assert not f.move_stock("2C", "west-wing", "infusion_pump", 3)
    assert f.place_stock("2C") == {"infusion_pump": 2}


def test_unknown_place_holds_nothing():
    f = make_orch().fleet
    assert f.place_stock("nowhere") == {} and not f.move_stock("nowhere", "west-wing", "infusion_pump")


def test_places_are_listed_on_get_fleet(api):
    from conftest import http_json
    _, body = http_json("GET", api + "/fleet")
    assert body["places"]["2C"] == {"infusion_pump": 2}


def test_places_survive_restart_on_dynamo(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    from cloud.aws_dynamo import DynamoStockStore, ensure_tables, tables_from_env
    from cloud.clock import FixedClock
    from cloud.fleet import FleetRegistry
    from cloud.pack import Pack
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name="ap-south-1")
        ensure_tables(client, tables_from_env())
        pack = Pack.load("hospital")
        f1 = FleetRegistry.load(FixedClock(), pack, stock_store=DynamoStockStore(client))
        assert f1.move_stock("2C", "west-wing", "infusion_pump", 1)
        f2 = FleetRegistry.load(FixedClock(), pack, stock_store=DynamoStockStore(client))   # a restart
        assert f2.place_stock("2C") == {"infusion_pump": 1}
