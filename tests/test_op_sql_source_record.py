"""SQL-native provenance hydration by physical table and record_id."""


async def test_hydrates_a_current_record_without_projection_aliases(client):
    response = await client.get("/v1/sql/source-record/records_new/4001")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "partner_sql"
    assert body["table"] == "records_new"
    assert body["rowid"] is None
    assert body["record_id"] == "4001"
    assert "property_owner" in body["source_file"]
    assert body["data"]["first_name"] == "JANE"
    assert body["data"]["raw_data"]["ownerName"] == "DOE, JANE ANN"
    assert "ownername" not in body["data"]


async def test_hydrates_a_legacy_record(client):
    body = (await client.get("/v1/sql/source-record/records_legacy/1002")).json()
    assert body["table"] == "records_legacy"
    assert body["data"]["last_name"] == "Doe"
    assert body["data"]["raw_data"]["Record_Date"] == "20240115"


async def test_rejects_unknown_table_missing_row_and_bad_id(client):
    assert (await client.get("/v1/sql/source-record/tax/4001")).status_code == 404
    assert (await client.get("/v1/sql/source-record/records_new/999999")).status_code == 404
    assert (await client.get("/v1/sql/source-record/records_new/nope")).status_code == 400
