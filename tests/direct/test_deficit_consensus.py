import json
from unittest.mock import Mock, call

import pytest


SLA_URL = "https://sla.example/terms"
EVIDENCE_URL = "https://sla.example/incident"
STAKE_WEI = 10**18 + 123
THRESHOLD_BPS = 9990
PENALTY_CAP_BPS = 5000
NONCANONICAL_PAYOUT_FIELDS = [
    {},
    {"breach_confirmed": True},
    {"measured_deficit_bps": 0},
    {"breach_confirmed": True, "measured_deficit_bps": None},
    {"breach_confirmed": True, "measured_deficit_bps": "0"},
    {"breach_confirmed": True, "measured_deficit_bps": 0.5},
    {"breach_confirmed": True, "measured_deficit_bps": False},
    {"breach_confirmed": True, "measured_deficit_bps": []},
    {"breach_confirmed": True, "measured_deficit_bps": -1},
    {"breach_confirmed": True, "measured_deficit_bps": 10001},
    {"breach_confirmed": "true", "measured_deficit_bps": 0},
    {"breach_confirmed": 1, "measured_deficit_bps": 0},
    {"breach_confirmed": False, "measured_deficit_bps": 1},
]


def mock_adjudication(direct_vm, deficit_bps, breach_confirmed=True):
    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r"https://sla\.example/(terms|incident)",
        {"status": 200, "body": b"Independent availability evidence."},
    )
    direct_vm.mock_llm(
        r"You are an impartial SLA adjudicator",
        json.dumps({
            "breach_confirmed": breach_confirmed,
            "measured_deficit_bps": deficit_bps,
            "evidence_quality": "strong",
            "reasoning": "The independent evidence establishes this deficit.",
            "sla_availability_definition": "Availability over the billing period.",
        }),
    )


def submit_claim(contract, sla_id):
    return json.loads(contract.submit_claim(
        sla_id,
        json.dumps([EVIDENCE_URL]),
        9850,
        "A documented service outage during the billing period.",
    ))


@pytest.fixture
def registered_sla(direct_vm, direct_deploy, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.warp("2026-01-15T12:00:00Z")
    contract = direct_deploy(
        "contracts/sla_enforcement_oracle.py", sdk_version="v0.2.16"
    )
    direct_vm.value = STAKE_WEI
    sla_id = contract.register_sla(
        SLA_URL, THRESHOLD_BPS, 1767225600, 1769904000,
        "0x" + bytes(direct_bob).hex(), PENALTY_CAP_BPS,
    )
    direct_vm.value = 0
    direct_vm.sender = direct_bob
    return contract, sla_id


@pytest.mark.parametrize("deficit_delta", [
    *range(-101, 0), *range(1, 102),
])
def test_validator_rejects_every_nearby_deficit(
    registered_sla, direct_vm, deficit_delta
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 200)
    verdict = submit_claim(contract, sla_id)
    verdict["measured_deficit_bps"] = 200 + deficit_delta

    assert direct_vm.run_validator(leader_result=verdict) is False


@pytest.mark.parametrize("leader_deficit,validator_deficit", [
    (0, 1), (1, 0), (9999, 10000), (10000, 9999), (0, 10000),
])
def test_validator_rejects_deficit_boundary_disagreement(
    registered_sla, direct_vm, leader_deficit, validator_deficit
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, validator_deficit)
    verdict = submit_claim(contract, sla_id)
    verdict["measured_deficit_bps"] = leader_deficit

    assert direct_vm.run_validator(leader_result=verdict) is False


@pytest.mark.parametrize("deficit_bps", [0, 1, 99, 100, 140, 4995, 9990, 10000])
@pytest.mark.parametrize("serialized", [False, True])
def test_validator_accepts_exact_deficit_with_different_reasoning(
    registered_sla, direct_vm, deficit_bps, serialized
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, deficit_bps)
    verdict = submit_claim(contract, sla_id)
    verdict["reasoning"] = "Different wording for the same adjudication."
    verdict["evidence_quality"] = "moderate"
    proposed = json.dumps(verdict) if serialized else verdict

    assert direct_vm.run_validator(leader_result=proposed) is True


@pytest.mark.parametrize("fields", NONCANONICAL_PAYOUT_FIELDS)
def test_validator_rejects_noncanonical_payout_fields(
    registered_sla, direct_vm, fields
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 0, fields.get("breach_confirmed") is not False)
    submit_claim(contract, sla_id)

    assert direct_vm.run_validator(leader_result=fields) is False


@pytest.mark.parametrize("fields", NONCANONICAL_PAYOUT_FIELDS)
def test_settlement_rejects_noncanonical_fields_before_storing_claim(
    registered_sla, direct_vm, direct_alice, monkeypatch, fields
):
    from genlayer import gl

    contract, sla_id = registered_sla
    monkeypatch.setattr(gl.vm, "run_nondet_unsafe", Mock(return_value=fields))

    with direct_vm.expect_revert("[LLM_ERROR]"):
        submit_claim(contract, sla_id)
    assert contract.get_claim_count(sla_id) == 0
    assert contract.get_claim(f"{sla_id}:0") == ""
    assert int(contract.get_provider_stake("0x" + bytes(direct_alice).hex())) == STAKE_WEI


@pytest.mark.parametrize("leader_result", [None, [], 0, "not JSON"])
def test_validator_rejects_invalid_result(registered_sla, direct_vm, leader_result):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 140)
    submit_claim(contract, sla_id)

    assert direct_vm.run_validator(leader_result=leader_result) is False
    assert direct_vm.run_validator(leader_error=RuntimeError("LLM failed")) is False


def test_validator_rejects_breach_disagreement(registered_sla, direct_vm):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 0, False)
    verdict = submit_claim(contract, sla_id)
    verdict["breach_confirmed"] = True

    assert direct_vm.run_validator(leader_result=verdict) is False


def test_validator_independently_reads_evidence(
    registered_sla, direct_vm, monkeypatch
):
    from genlayer import gl

    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 140)
    submit_claim(contract, sla_id)
    fetch = Mock(wraps=gl.nondet.web.get)
    assess = Mock(wraps=gl.nondet.exec_prompt)
    monkeypatch.setattr(gl.nondet.web, "get", fetch)
    monkeypatch.setattr(gl.nondet, "exec_prompt", assess)

    assert direct_vm.run_validator() is True
    assert fetch.call_args_list == [call(SLA_URL), call(EVIDENCE_URL)]
    assess.assert_called_once()

    mock_adjudication(direct_vm, 141)
    assert direct_vm.run_validator() is False


def test_validator_rejects_independent_assessment_failure(
    registered_sla, direct_vm
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 140)
    submit_claim(contract, sla_id)
    direct_vm.clear_mocks()

    assert direct_vm.run_validator() is False


@pytest.mark.parametrize("deficit_bps", [1, 99, 100, 140, 4995, 4996, 9990, 10000])
def test_exact_consensus_deficit_drives_stored_and_transferred_penalty(
    registered_sla, direct_vm, direct_alice, direct_bob, monkeypatch, deficit_bps
):
    from genlayer import Address, gl

    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, deficit_bps)
    verdict = submit_claim(contract, sla_id)
    assert direct_vm.run_validator() is True
    expected_penalty = min(
        STAKE_WEI * deficit_bps // THRESHOLD_BPS,
        STAKE_WEI * PENALTY_CAP_BPS // 10000,
    )
    claim = json.loads(contract.get_claim(verdict["claim_key"]))
    assert claim["measured_deficit_bps"] == deficit_bps
    assert int(claim["penalty_wei"]) == expected_penalty
    assert int(verdict["penalty_wei"]) == expected_penalty

    consumer = Mock()
    get_consumer = Mock(return_value=consumer)
    monkeypatch.setattr(gl, "get_contract_at", get_consumer)
    transfer = json.loads(contract.execute_penalty(verdict["claim_key"]))

    get_consumer.assert_called_once_with(Address(direct_bob))
    consumer.emit_transfer.assert_called_once_with(
        value=expected_penalty, on="finalized"
    )
    assert int(transfer["penalty_wei"]) == expected_penalty
    assert int(contract.get_provider_stake("0x" + bytes(direct_alice).hex())) == (
        STAKE_WEI - expected_penalty
    )
    assert int(json.loads(contract.get_sla(sla_id))["stake_wei"]) == (
        STAKE_WEI - expected_penalty
    )
    assert json.loads(contract.get_claim(verdict["claim_key"]))["penalty_executed"]
    with direct_vm.expect_revert("Penalty has already been executed"):
        contract.execute_penalty(verdict["claim_key"])
    assert consumer.emit_transfer.call_count == 1


@pytest.mark.parametrize("breach_confirmed", [False, True])
def test_zero_deficit_cannot_transfer_stake(
    registered_sla, direct_vm, direct_alice, breach_confirmed
):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 0, breach_confirmed)
    verdict = submit_claim(contract, sla_id)

    assert direct_vm.run_validator() is True
    assert verdict["measured_deficit_bps"] == 0
    assert int(verdict["penalty_wei"]) == 0
    with direct_vm.expect_revert():
        contract.execute_penalty(verdict["claim_key"])
    assert int(contract.get_provider_stake("0x" + bytes(direct_alice).hex())) == STAKE_WEI


def test_no_breach_normalizes_to_consensus_bound_zero(registered_sla, direct_vm):
    contract, sla_id = registered_sla
    mock_adjudication(direct_vm, 140, False)
    verdict = submit_claim(contract, sla_id)

    assert direct_vm.run_validator() is True
    assert verdict["breach_confirmed"] is False
    assert verdict["measured_deficit_bps"] == 0
    assert int(verdict["penalty_wei"]) == 0
