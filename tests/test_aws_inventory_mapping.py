"""Tests for the AWS inventory field mappings, against canned boto3 responses.

No real AWS call is made — _boto3_client is replaced with a fake. A silent
dict-key typo here would ship wrong data to every CLI/MCP consumer.
"""
import pytest

from agent.integrations import aws


class _FakeClient:
    def __init__(self, **responses):
        self._responses = responses

    def __getattr__(self, name):
        if name not in self._responses:
            raise AttributeError(name)
        response = self._responses[name]

        def call(*args, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response
        return call


@pytest.fixture
def fake_boto(monkeypatch):
    def install(**responses):
        monkeypatch.setattr(aws, "_boto3_client", lambda service, region: _FakeClient(**responses))
    return install


# --- get_all_rds ---------------------------------------------------------

def _db(identifier, status):
    return {
        "DBInstanceIdentifier": identifier, "DBInstanceStatus": status,
        "Engine": "postgres", "EngineVersion": "16.3", "DBInstanceClass": "db.t3.micro",
        "MultiAZ": True, "AllocatedStorage": 20,
        "Endpoint": {"Address": f"{identifier}.abc.rds.amazonaws.com"},
        "AvailabilityZone": "ap-south-1a",
    }


def test_get_all_rds_returns_every_instance_regardless_of_status(fake_boto):
    fake_boto(describe_db_instances={"DBInstances": [
        _db("orders", "available"), _db("legacy", "stopped"),
    ]})
    dbs = aws.get_all_rds("ap-south-1")
    assert [d["id"] for d in dbs] == ["orders", "legacy"]


def test_get_all_rds_maps_fields(fake_boto):
    fake_boto(describe_db_instances={"DBInstances": [_db("orders", "available")]})
    (db,) = aws.get_all_rds("ap-south-1")
    assert db == {
        "id": "orders", "engine": "postgres", "engine_version": "16.3",
        "status": "available", "instance_class": "db.t3.micro", "multi_az": True,
        "allocated_storage": 20, "endpoint": "orders.abc.rds.amazonaws.com",
        "availability_zone": "ap-south-1a", "region": "ap-south-1", "is_healthy": True,
    }


@pytest.mark.parametrize("status, healthy", [
    ("available", True), ("backing-up", True),
    ("stopped", False), ("failed", False), ("storage-full", False),
])
def test_get_all_rds_health_flag(fake_boto, status, healthy):
    fake_boto(describe_db_instances={"DBInstances": [_db("x", status)]})
    assert aws.get_all_rds("ap-south-1")[0]["is_healthy"] is healthy


def test_get_all_rds_tolerates_missing_optional_fields(fake_boto):
    fake_boto(describe_db_instances={"DBInstances": [
        {"DBInstanceIdentifier": "bare", "DBInstanceStatus": "creating"},
    ]})
    (db,) = aws.get_all_rds("ap-south-1")
    assert db["endpoint"] == "?" and db["engine"] == "?" and db["multi_az"] is False


def test_get_all_rds_returns_empty_on_api_failure(fake_boto):
    fake_boto(describe_db_instances=RuntimeError("AccessDenied: rds:DescribeDBInstances"))
    assert aws.get_all_rds("ap-south-1") == []


def test_get_all_rds_empty_account(fake_boto):
    fake_boto(describe_db_instances={"DBInstances": []})
    assert aws.get_all_rds("ap-south-1") == []


# --- get_all_ec2 ---------------------------------------------------------

def _instance(instance_id, state, name=None):
    tags = [{"Key": "Name", "Value": name}] if name else []
    return {
        "InstanceId": instance_id, "State": {"Name": state}, "InstanceType": "t3.medium",
        "PrivateIpAddress": "10.0.10.11", "Placement": {"AvailabilityZone": "ap-south-1a"},
        "VpcId": "vpc-123", "LaunchTime": "2026-09-12", "Tags": tags,
    }


def test_get_all_ec2_flattens_reservations(fake_boto):
    fake_boto(describe_instances={"Reservations": [
        {"Instances": [_instance("i-1", "running"), _instance("i-2", "running")]},
        {"Instances": [_instance("i-3", "stopped")]},
    ]})
    assert [i["id"] for i in aws.get_all_ec2("ap-south-1")] == ["i-1", "i-2", "i-3"]


def test_get_all_ec2_uses_name_tag_or_falls_back_to_id(fake_boto):
    fake_boto(describe_instances={"Reservations": [{"Instances": [
        _instance("i-1", "running", name="infragpt_nodes"), _instance("i-2", "running"),
    ]}]})
    named, unnamed = aws.get_all_ec2("ap-south-1")
    assert named["name"] == "infragpt_nodes"
    assert unnamed["name"] == "i-2"


def test_get_all_ec2_only_running_is_healthy(fake_boto):
    fake_boto(describe_instances={"Reservations": [{"Instances": [
        _instance("i-1", "running"), _instance("i-2", "stopped"),
    ]}]})
    running, stopped = aws.get_all_ec2("ap-south-1")
    assert running["is_healthy"] is True and stopped["is_healthy"] is False


def test_get_all_ec2_returns_empty_on_api_failure(fake_boto):
    fake_boto(describe_instances=RuntimeError("InvalidClientTokenId"))
    assert aws.get_all_ec2("ap-south-1") == []
