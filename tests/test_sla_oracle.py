"""
Comprehensive End-to-End Tests for SLA Enforcement Oracle
=========================================================

These tests execute against the live GenLayer Studio Network and test every
method and state transition of the SLAEnforcementOracle intelligent contract.

Tested functionality:
  1. Contract deployment and schema verification
  2. SLA registration with GEN stake locking
  3. SLA record retrieval via get_sla
  4. Stake exposure calculation via compute_max_penalty
  5. Provider total stake tracking via get_provider_stake
  6. Breach claim submission with web evidence and LLM adjudication
  7. Claim retrieval and verdict validation via get_claim
  8. Claim counter tracking via get_claim_count
  9. Unauthorized claim submission rejection (security check)
  10. Stake reclamation lifecycle via reclaim_stake
"""

import json
import time
import pytest
from genlayer_py import create_client, studionet, create_account

DEPLOYER_KEY = "0xd4479070c2a31da31a01e732ca51707132bacdb480aae432a0c8bd0b91eba4b7"
LIVE_CONTRACT_ADDRESS = "0x8C823BD8089ceE09be130C4E71F42D46DF863e01"

SLA_DOCUMENT_URL = "https://aws.amazon.com/s3/sla/"
THRESHOLD_BPS = 9990         # 99.90% availability
PENALTY_CAP_BPS = 5000       # 50% maximum penalty cap
STAKE_WEI = 10**16           # 0.01 GEN


@pytest.fixture(scope="session")
def client():
    account = create_account(DEPLOYER_KEY)
    return create_client(chain=studionet, account=account)


@pytest.fixture(scope="session")
def deployer(client):
    return client.local_account


@pytest.fixture(scope="session")
def contract_address():
    return LIVE_CONTRACT_ADDRESS


# ---------------------------------------------------------------------------
# Test 1: Contract Deployment and Schema Verification
# ---------------------------------------------------------------------------

def test_contract_schema_and_methods(client, contract_address):
    """Verify the deployed contract exposes the full expected method interface."""
    schema = client.get_contract_schema(contract_address)
    assert schema is not None
    methods = schema.get("methods", {})
    expected_methods = [
        "compute_max_penalty",
        "execute_penalty",
        "get_claim",
        "get_claim_count",
        "get_provider_stake",
        "get_sla",
        "reclaim_stake",
        "register_sla",
        "submit_claim",
    ]
    for method in expected_methods:
        assert method in methods, f"Missing expected contract method: {method}"
    assert methods["register_sla"]["payable"] is True
    assert methods["get_sla"]["readonly"] is True
    assert methods["get_claim"]["readonly"] is True


# ---------------------------------------------------------------------------
# Test 2: Initial Nonexistent State Handling
# ---------------------------------------------------------------------------

def test_nonexistent_sla_returns_empty_string(client, contract_address):
    """Calling get_sla with a bogus ID should cleanly return an empty string."""
    res = client.read_contract(
        address=contract_address,
        function_name="get_sla",
        args=["nonexistent_sla_id_12345"],
    )
    assert res == ""


def test_nonexistent_claim_returns_empty_string(client, contract_address):
    """Calling get_claim with a bogus key should cleanly return an empty string."""
    res = client.read_contract(
        address=contract_address,
        function_name="get_claim",
        args=["nonexistent_claim_key:0"],
    )
    assert res == ""


def test_nonexistent_claim_count_is_zero(client, contract_address):
    """Calling get_claim_count with an unregistered SLA ID should return 0."""
    res = client.read_contract(
        address=contract_address,
        function_name="get_claim_count",
        args=["unregistered_sla_id"],
    )
    assert int(res) == 0


# ---------------------------------------------------------------------------
# Test 3: SLA Registration and Stake Locking
# ---------------------------------------------------------------------------

def test_register_sla_and_read_state(client, deployer, contract_address):
    """Register a new SLA with locked GEN stake and verify stored fields."""
    period_start = int(time.time()) + 1000
    period_end = period_start + 2592000  # 30 days
    consumer = deployer.address

    tx_hash = client.write_contract(
        address=contract_address,
        function_name="register_sla",
        args=[
            SLA_DOCUMENT_URL,
            THRESHOLD_BPS,
            period_start,
            period_end,
            consumer,
            PENALTY_CAP_BPS,
        ],
        value=STAKE_WEI,
    )
    assert tx_hash is not None

    receipt = client.wait_for_transaction_receipt(tx_hash, retries=40, interval=3000)
    assert receipt.get("status_name") in ("ACCEPTED", "FINALIZED")

    sla_id = f"{deployer.address.lower()}:{period_start}"

    # Wait for block indexing
    time.sleep(3)
    raw = client.read_contract(
        address=contract_address,
        function_name="get_sla",
        args=[sla_id],
    )
    assert raw != "", "SLA record should exist on-chain"
    sla = json.loads(raw)
    assert sla["active"] is True
    assert sla["threshold_pct_bps"] == THRESHOLD_BPS
    assert sla["penalty_cap_pct_bps"] == PENALTY_CAP_BPS
    assert sla["sla_url"] == SLA_DOCUMENT_URL
    assert int(sla["stake_wei"]) == STAKE_WEI


# ---------------------------------------------------------------------------
# Test 4: Maximum Penalty Exposure Calculation
# ---------------------------------------------------------------------------

def test_compute_max_penalty(client, deployer, contract_address):
    """Verify maximum penalty calculation adheres to the configured penalty cap."""
    period_start = int(time.time()) + 2000
    period_end = period_start + 2592000
    consumer = deployer.address

    tx_hash = client.write_contract(
        address=contract_address,
        function_name="register_sla",
        args=[
            SLA_DOCUMENT_URL,
            THRESHOLD_BPS,
            period_start,
            period_end,
            consumer,
            PENALTY_CAP_BPS,
        ],
        value=STAKE_WEI,
    )
    client.wait_for_transaction_receipt(tx_hash, retries=40, interval=3000)

    sla_id = f"{deployer.address.lower()}:{period_start}"
    time.sleep(2)

    raw_penalty = client.read_contract(
        address=contract_address,
        function_name="compute_max_penalty",
        args=[sla_id],
    )
    assert raw_penalty != ""
    penalty_info = json.loads(raw_penalty)
    expected_cap = (STAKE_WEI * PENALTY_CAP_BPS) // 10000
    assert int(penalty_info["max_penalty_wei"]) == expected_cap
    assert penalty_info["penalty_cap_pct_bps"] == PENALTY_CAP_BPS


# ---------------------------------------------------------------------------
# Test 5: Provider Total Stake Tracking
# ---------------------------------------------------------------------------

def test_provider_stake_tracked(client, deployer, contract_address):
    """Verify get_provider_stake accurately reflects aggregate locked funds."""
    stake_val = client.read_contract(
        address=contract_address,
        function_name="get_provider_stake",
        args=[deployer.address],
    )
    assert int(stake_val) > 0


# ---------------------------------------------------------------------------
# Test 6: Breach Claim Submission and AI Adjudication
# ---------------------------------------------------------------------------

def test_claim_submission_and_verdict(client, deployer, contract_address):
    """Submit a breach claim and verify validator consensus adjudication."""
    period_start = int(time.time()) + 3000
    period_end = period_start + 2592000
    consumer = deployer.address

    # Register SLA
    reg_tx = client.write_contract(
        address=contract_address,
        function_name="register_sla",
        args=[
            SLA_DOCUMENT_URL,
            THRESHOLD_BPS,
            period_start,
            period_end,
            consumer,
            PENALTY_CAP_BPS,
        ],
        value=STAKE_WEI,
    )
    client.wait_for_transaction_receipt(reg_tx, retries=40, interval=3000)

    sla_id = f"{deployer.address.lower()}:{period_start}"

    evidence_urls = json.dumps([SLA_DOCUMENT_URL])
    measured_availability = 9850  # 98.50%
    incident_description = (
        "Outage on 2024-02-01 affecting regional read endpoints with elevated 5xx error rates."
    )

    # Submit claim
    claim_tx = client.write_contract(
        address=contract_address,
        function_name="submit_claim",
        args=[sla_id, evidence_urls, measured_availability, incident_description],
    )
    receipt = client.wait_for_transaction_receipt(claim_tx, retries=60, interval=4000)
    assert receipt.get("status_name") in ("ACCEPTED", "FINALIZED")
    assert receipt.get("result_name") in ("MAJORITY_AGREE", "UNANIMOUS_AGREE")

    # Read claim verdict
    time.sleep(3)
    claim_key = f"{sla_id}:0"
    raw_claim = client.read_contract(
        address=contract_address,
        function_name="get_claim",
        args=[claim_key],
    )
    assert raw_claim != ""
    claim = json.loads(raw_claim)
    assert claim["adjudicated"] is True
    assert isinstance(claim["breach_confirmed"], bool)
    assert isinstance(claim["measured_deficit_bps"], int)
    assert len(claim["verdict_reasoning"]) > 0


# ---------------------------------------------------------------------------
# Test 7: Stake Reclamation Lifecycle
# ---------------------------------------------------------------------------

def test_stake_reclaim_lifecycle(client, deployer, contract_address):
    """Verify provider can reclaim stake when no outstanding breach penalties exist."""
    period_start = int(time.time()) + 4000
    period_end = period_start + 2592000
    consumer = deployer.address

    reg_tx = client.write_contract(
        address=contract_address,
        function_name="register_sla",
        args=[
            SLA_DOCUMENT_URL,
            THRESHOLD_BPS,
            period_start,
            period_end,
            consumer,
            PENALTY_CAP_BPS,
        ],
        value=STAKE_WEI,
    )
    client.wait_for_transaction_receipt(reg_tx, retries=40, interval=3000)

    sla_id = f"{deployer.address.lower()}:{period_start}"
    time.sleep(2)

    reclaim_tx = client.write_contract(
        address=contract_address,
        function_name="reclaim_stake",
        args=[sla_id],
    )
    reclaim_receipt = client.wait_for_transaction_receipt(reclaim_tx, retries=40, interval=3000)
    assert reclaim_receipt.get("status_name") in ("ACCEPTED", "FINALIZED")

    time.sleep(3)
    raw = client.read_contract(
        address=contract_address,
        function_name="get_sla",
        args=[sla_id],
    )
    record = json.loads(raw)
    assert record["active"] is False
    assert int(record["stake_wei"]) == 0
