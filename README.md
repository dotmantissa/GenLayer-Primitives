# SLA Enforcement Oracle

A decentralized, autonomous Service Level Agreement (SLA) adjudication and enforcement primitive built on GenLayer.

The SLA Enforcement Oracle eliminates the friction, legal overhead, and trust requirements of commercial SLAs. Service providers lock a performance stake into the contract at the start of a billing cycle. Consumers submit breach claims accompanied by public web evidence (such as status pages, synthetic monitoring dashboards, or incident postmortems). GenLayer validator committees independently fetch the natural language SLA terms, inspect the evidence, adjudicate the claim through AI consensus, and execute financial penalties directly on-chain.

## Live Deployment

- Network: GenLayer Studio Network (studionet)
- Chain ID: 61999
- RPC Endpoint: https://studio.genlayer.com/api
- Contract Address: `0x04bfadd3273Bd7F50B2B3b316ACcfBe8a0a9B59F`
- Explorer: [View the fixed contract](https://explorer-studio.genlayer.com/address/0x04bfadd3273Bd7F50B2B3b316ACcfBe8a0a9B59F)
- Deployment Transaction: `0x42d8f44cc16eb1c58ef247ffcc1037ef58179681d5e3197ce1af3b6363d0e4ac`
- Deployer Address: `0xBC1399c55538eC034d4Da550C03c34Ae0C357f53`
- GenVM Runner: `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- Source SHA-256: `d797df82d686028f863bf1057d6d64956f42a84b95c2574e110c6f1f8afc43e3`

This finalized deployment contains the exact-deficit consensus fix. The previous instance, `0x8C823BD8089ceE09be130C4E71F42D46DF863e01`, retains the unsafe tolerance and is superseded: use the new address for new SLAs. Existing stakes and claims are not migrated or modified.

## The Problem

Every enterprise software contract, API subscription, and cloud infrastructure product includes an SLA guaranteeing availability (for example, 99.9% uptime). Despite these written guarantees, enforcing SLAs today is broken:

1. High Friction: Consumers must manually collect evidence, open customer support tickets, and wait weeks for vendor billing departments to review claims.
2. Conflict of Interest: The service provider acts as the sole judge and jury of whether their own service failed.
3. Abandoned Value: Small and medium enterprises frequently absorb outage losses because claiming SLA credits takes more time and legal effort than the credits are worth.
4. Ambiguity in Contract Terms: Outages involve nuanced exclusions: scheduled maintenance windows, regional degradations, upstream transit issues, or client network faults. Traditional deterministic smart contracts cannot read natural language documents or assess subjective evidence.

## The GenLayer Solution

The SLA Enforcement Oracle leverages GenLayer's Optimistic Democracy consensus:

- Natural Language Understanding: The contract reads human-written SLA agreements directly from public URLs, parsing commitments, exclusion clauses, and remedy tiers.
- Independent Web Evidence Retrieval: When a claim is filed, each GenLayer validator independently fetches the provider's public status pages, incident reports, and third-party monitoring URLs.
- Decentralized Adjudication: Validators prompt large language models inside isolated execution sandboxes, checking whether the evidence demonstrates a bona fide breach under the stated terms.
- Automated Settlement: When consensus is achieved, the contract calculates proportional damages and transfers penalty funds from the provider's locked stake to the consumer.

## Architecture and Consensus Flow

```
+------------------+         +-------------------------+
| Service Provider |         |    Service Consumer     |
+--------+---------+         +------------+------------+
         |                                |
         | 1. register_sla(stake)         |
         v                                |
+---------------------------------------+ |
|        SLAEnforcementOracle           | |
|     (Intelligent Contract)            | |
+---------------------------------------+ |
         ^                                |
         | 2. submit_claim(evidence_urls) |
         +--------------------------------+
         |
         v
+-------------------------------------------------------------+
|               GenLayer Validator Committee                  |
|                                                             |
|  [Leader Node]                       [Validator Nodes]      |
|  - Fetches SLA document              - Independently fetch  |
|  - Fetches evidence URLs             - Independently verify |
|  - Runs LLM prompt                   - Compare verdicts     |
|  - Proposes verdict JSON             - Bind exact deficit   |
|                                                             |
|                   Equivalence Principle                     |
|           (Majority / Unanimous Agreement)                  |
+-------------------------------------------------------------+
         |
         v
+---------------------------------------+
|          Deterministic State          |
|  - Records Claim Record               |
|  - Computes Proportional Penalty      |
|  - Deducts from Provider Stake        |
|  - Emits Transfer to Consumer         |
+---------------------------------------+
```

### Equivalence Principle

Consensus for non-deterministic web retrieval and LLM evaluation is governed by `gl.vm.run_nondet_unsafe`:

1. Leader Function:
   - Fetches the published SLA contract from its canonical URL.
   - Fetches up to five supporting evidence URLs provided by the claimant.
   - Prompts the LLM with structured criteria: evaluate availability definition, scheduled maintenance exclusions, and evidence credibility.
   - Returns a normalized JSON payload containing `breach_confirmed`, `measured_deficit_bps`, and `verdict_reasoning`.

2. Validator Function:
   - Validates that the leader proposal conforms to the strict schema.
   - Independently performs web retrieval of the SLA and evidence.
   - Evaluates the claim against its own LLM assessment.
   - Accepts the proposal only when both nodes agree exactly on the boolean `breach_confirmed` and integer `measured_deficit_bps`. Even a one-basis-point disagreement is rejected; reasoning text may differ.
   - Requires an explicit integer deficit between 0 and 10000, with zero deficit for a no-breach verdict. Missing, malformed, or coerced proposal fields are rejected.
   - Uses the same payout-field validation after consensus, then stores and calculates the penalty from that exact agreed deficit. There is no tolerance band or separate unbound payout value.

Exact agreement prioritizes settlement safety: ambiguous evidence may cause validator disagreement rather than accepting materially different payouts.

## Contract Specification

### Data Structures

#### SLARecord
- `provider` (Address): The service provider locking the stake.
- `consumer` (Address): The designated counterparty authorized to submit claims.
- `sla_url` (str): Public HTTP URL of the SLA terms.
- `threshold_pct` (u32): Target availability in basis points (for example, 9990 represents 99.90%).
- `period_start` (u64): Unix timestamp marking start of the billing period.
- `period_end` (u64): Unix timestamp marking end of the billing period.
- `stake_wei` (u256): Total GEN locked by the provider for this SLA.
- `penalty_cap_pct` (u32): Maximum percentage of the stake claimable across all breaches in basis points.
- `active` (bool): Operational status of the SLA.

#### Claim
- `sla_id` (str): Identifier of the SLA being claimed against.
- `claimant` (Address): Address of the claimant (must match the consumer).
- `submitted_at` (u64): Timestamp of claim submission.
- `measured_availability_pct` (u32): Claimed availability in basis points.
- `incident_description` (str): Plain-text summary of the incident.
- `evidence_urls_json` (str): Serialized JSON array of evidence URLs.
- `adjudicated` (bool): Whether the claim has completed validator consensus.
- `breach_confirmed` (bool): True if consensus determined a breach occurred.
- `measured_deficit_bps` (u32): Confirmed shortfall below the agreed threshold.
- `penalty_wei` (u256): Proportional penalty amount owed to the consumer.
- `penalty_executed` (bool): Whether funds have been disbursed.
- `verdict_reasoning` (str): Natural language rationale provided by the validator committee.

### Public Methods

#### `register_sla(sla_url, threshold_pct_bps, period_start, period_end, consumer_address, penalty_cap_pct_bps) -> str`
- Type: Write, Payable
- Registers a new SLA and locks the attached GEN value as security stake.
- Derives a deterministic `sla_id` using the provider address and period start timestamp.

#### `submit_claim(sla_id, evidence_urls, measured_availability_bps, incident_description) -> str`
- Type: Write
- Callable only by the designated consumer.
- Triggers non-deterministic committee consensus to fetch evidence, analyze SLA clauses, and determine whether a breach occurred.
- Computes proportional penalties and stores the immutable claim record.

#### `execute_penalty(claim_key) -> str`
- Type: Write
- Executes payout of an adjudicated breach claim.
- Transfers the calculated penalty from the provider's locked stake to the consumer address.

#### `reclaim_stake(sla_id) -> str`
- Type: Write
- Callable only by the provider after the billing period concludes.
- Verifies that all filed claims are resolved and returns any remaining unpenalized stake to the provider.

#### `get_sla(sla_id) -> str`
- Type: View
- Returns the complete JSON representation of an SLA record.

#### `get_claim(claim_key) -> str`
- Type: View
- Returns the full JSON claim object including the consensus verdict and validator reasoning.

#### `compute_max_penalty(sla_id) -> str`
- Type: View
- Calculates the maximum possible financial exposure for an SLA given its penalty cap.

#### `get_provider_stake(provider_address) -> str`
- Type: View
- Returns the total active stake currently deposited by a provider.

#### `get_claim_count(sla_id) -> int`
- Type: View
- Returns the number of claims registered against a specific SLA.

## Penalty Calculation Formula

Penalties are proportional to the severity of the deficit and strictly capped by the configured maximum penalty parameter:

1. Consensus-Bound Deficit:
   `deficit_bps = verdict["measured_deficit_bps"]`
   Validators must independently agree on this exact integer shortfall below the SLA threshold. The consumer's submitted availability is evidence input, not an authoritative payout amount. If there is no confirmed breach or the agreed deficit is zero, the penalty is zero.

2. Proportional Scaling:
   `raw_penalty = (stake_wei * deficit_bps) // threshold_pct_bps`

3. Cap Enforcement:
   `max_penalty = (stake_wei * penalty_cap_pct_bps) // 10000`
   `final_penalty = min(raw_penalty, max_penalty)`

All calculations use integer basis points to prevent floating point discrepancies across different validator environments.

## Integration Guide for Builders

### Python Integration

```python
import json
import time
from genlayer_py import create_client, studionet, create_account

# Initialize client
PRIVATE_KEY = "0x..."
account = create_account(PRIVATE_KEY)
client = create_client(chain=studionet, account=account)

ORACLE_ADDRESS = "0x04bfadd3273Bd7F50B2B3b316ACcfBe8a0a9B59F"

# 1. Provider: Register an SLA
period_start = int(time.time())
period_end = period_start + 30 * 86400  # 30-day billing cycle
stake_amount = 10**18  # 1 GEN

reg_tx = client.write_contract(
    address=ORACLE_ADDRESS,
    function_name="register_sla",
    args=[
        "https://api.mycloud.com/terms/sla",  # SLA document URL
        9990,                                 # 99.90% availability
        period_start,
        period_end,
        "0xConsumerAddressHere...",           # Consumer wallet
        5000,                                 # 50% max penalty cap
    ],
    value=stake_amount,
)
client.wait_for_transaction_receipt(reg_tx)
sla_id = f"{account.address.lower()}:{period_start}"

# 2. Consumer: Submit Breach Claim
evidence = json.dumps([
    "https://status.mycloud.com/incidents/2024-03-12",
    "https://uptime.thirdparty.org/reports/mycloud",
])

claim_tx = client.write_contract(
    address=ORACLE_ADDRESS,
    function_name="submit_claim",
    args=[
        sla_id,
        evidence,
        9820,  # Measured 98.20%
        "Core database outage in us-east region spanning 8 hours.",
    ],
)
client.wait_for_transaction_receipt(claim_tx)

# 3. Read the Adjudicated Verdict
claim_key = f"{sla_id}:0"
claim_raw = client.read_contract(
    address=ORACLE_ADDRESS,
    function_name="get_claim",
    args=[claim_key],
)
verdict = json.loads(claim_raw)
print("Breach Confirmed:", verdict["breach_confirmed"])
print("Penalty Owed (wei):", verdict["penalty_wei"])
print("Validator Reasoning:", verdict["verdict_reasoning"])
```

### TypeScript / JavaScript Integration

```typescript
import { createAccount, createClient } from "genlayer-js";
import { studionet } from "genlayer-js/chains";

const client = createClient({
  chain: studionet,
  account: createAccount("0x...privateKey"),
});

const ORACLE_ADDRESS = "0x04bfadd3273Bd7F50B2B3b316ACcfBe8a0a9B59F";

// Read SLA status
async function checkSLA(slaId: string) {
  const rawSla = await client.readContract({
    address: ORACLE_ADDRESS,
    functionName: "get_sla",
    args: [slaId],
  });
  return JSON.parse(rawSla as string);
}

// Read Claim Details
async function checkClaim(claimKey: string) {
  const rawClaim = await client.readContract({
    address: ORACLE_ADDRESS,
    functionName: "get_claim",
    args: [claimKey],
  });
  return JSON.parse(rawClaim as string);
}
```

## Running the Test Suite

Use Python 3.12 or newer for the pinned GenVM SDK:

```bash
# Install verification dependencies
python3.12 -m pip install -r requirements-dev.txt

# Lint before testing
genvm-lint check contracts/sla_enforcement_oracle.py

# Run offline consensus and payout regressions
pytest tests/direct/ -v
```

The direct suite explicitly invokes the contract's captured validator with `direct_vm.run_validator`; ordinary leader-only direct execution does not test consensus. It checks every mismatch from 1 through 100 bps in both directions, adjacent boundary values, malformed proposals, independent evidence retrieval, and exact agreement despite different reasoning. It also checks that stored penalties and finalized transfer requests use the same agreed deficit, including integer rounding, caps, and zero payouts.

The generic `gltest` pytest plugin is disabled in `pyproject.toml` because its startup clears the configured artifacts directory. The direct-test plugin remains enabled, and the SDK-based live tests do not require that startup hook.

The end-to-end suite additionally verifies the deployed source bytes, successful execution receipts (not just accepted/finalized status), and the consensus-deficit-to-penalty formula against the live GenLayer network. It is skipped unless `GENLAYER_PRIVATE_KEY` is explicitly set, and reads the current contract address from `artifacts/deployment.json`. Never commit private keys. To run it after securely setting that environment variable:

```bash
pytest tests/test_sla_oracle.py -v -s
```

Set `SLA_ORACLE_ADDRESS` to override the recorded deployment when testing another instance.

Verification results and transaction hashes are recorded in [artifacts/verification.json](artifacts/verification.json): 267 offline regressions and 10 live tests passed, and all six live write transactions finalized successfully with five initial validators. The live claim was rejected with zero deficit and zero penalty; positive payout and transfer-amount checks are covered by the direct suite.

## Project Structure

```
sla-enforcement-oracle/
├── contracts/
│   └── sla_enforcement_oracle.py   # Core Intelligent Contract
├── tests/
│   ├── direct/
│   │   └── test_deficit_consensus.py # Offline validator and payout regressions
│   └── test_sla_oracle.py          # End-to-end integration test suite
├── artifacts/
│   ├── deployment.json             # Deployment metadata and network details
│   └── verification.json           # Test results and finalized transaction evidence
├── pyproject.toml                  # Python package and dependency configuration
├── requirements-dev.txt            # Pinned verification dependencies
├── gltest.config.yaml              # GenLayer test network definitions
└── README.md                       # Architecture, specification, and integration guide
```
