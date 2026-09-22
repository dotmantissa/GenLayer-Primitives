# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
SLA Enforcement Oracle
======================

Purpose
-------
This contract enforces Service Level Agreements between a service provider and
a consumer, entirely on-chain. A provider locks a penalty stake at the start of
a billing period. When the consumer believes a breach occurred, they submit a
claim backed by publicly accessible evidence (status pages, uptime monitors,
incident reports). GenLayer validators independently fetch that evidence, read
the SLA terms, and adjudicate: was there a breach, and if so, how large was the
deficit against the stated threshold?

If the consensus verdict is a breach, the contract automatically calculates the
proportional penalty and prepares it for transfer from the provider's locked
stake to the consumer. No customer support ticket. No legal review. No trust
in a single party.

Why GenLayer?
-------------
SLA adjudication is inherently ambiguous. A single region going down may or may
not constitute a breach depending on how the SLA defines "availability". A
timeout may be the client's network, not the provider's service. A scheduled
maintenance window may be excluded. These judgment calls require reading the
natural-language SLA document and weighing it against the evidence supplied by
the claimant.

This is exactly what GenLayer's Optimistic Democracy consensus handles. Multiple
validators independently read the SLA and the evidence, and the contract's
equivalence principle determines whether their conclusions agree. Disagreement
on borderline cases triggers the appeal mechanism with a larger validator panel.

Consensus Model
---------------
The adjudication method uses `gl.vm.run_nondet_unsafe` with a custom
leader/validator pair:

  - Leader function: fetches the SLA document from the stored URL, fetches each
    piece of evidence supplied by the claimant (status pages, uptime reports,
    incident logs), and asks an LLM to determine whether the evidence proves an
    SLA breach. Returns a structured JSON verdict.

  - Validator function: independently fetches the same SLA document and the
    same evidence URLs. It evaluates the leader's verdict against its own
    independent reading of the sources. It accepts the verdict only if the
    breach determination (yes/no) and the integer measured deficit in basis
    points agree exactly. That same validated deficit drives the penalty.

Contract Lifecycle
------------------
1. Provider calls `register_sla` supplying the SLA document URL, the agreed
   availability threshold (e.g., 99.9%), a billing period start time, and a
   penalty cap. They must send GEN as the stake.

2. The contract records the SLA and holds the stake.

3. During or after the billing period, the consumer calls `submit_claim`
   supplying:
     - the SLA ID
     - a list of evidence URLs (status pages, third-party monitors)
     - the consumer's measured availability figure
     - an incident description

4. GenLayer validators independently adjudicate the claim using the LLM and
   web-access APIs. The verdict is committed to chain once consensus is reached.

5. Anyone calls `execute_penalty` on a finalized claim. If the verdict is a
   breach, the proportional penalty is released from the provider's stake to the
   consumer.

6. After the billing period, the provider calls `reclaim_stake` to retrieve any
   un-penalized portion of their stake.

State Variables
---------------
- sla_records: each registered SLA with its terms, stake, and parties
- claims: each submitted claim with its verdict once adjudicated
- SLA IDs are deterministic (derived from provider and period), claims are
  sequential per SLA

Integration for Builders
------------------------
Deploy this contract and use the returned address in your application. The
provider calls `register_sla` once per billing period. Your consumer-facing
code calls `submit_claim`. The contract handles all adjudication through the
GenLayer consensus network. Your application reads the claim verdict from
`get_claim` once the transaction finalizes.
"""

import json
import re
import typing
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# ---------------------------------------------------------------------------
# Storage dataclasses
# ---------------------------------------------------------------------------

@allow_storage
@dataclass
class SLARecord:
    """
    Represents a single registered SLA between a provider and a consumer.
    """
    provider: Address
    consumer: Address
    sla_url: str          # publicly accessible URL of the SLA document
    threshold_pct: u32    # availability threshold in basis points (e.g. 9990 = 99.90%)
    period_start: u64     # Unix timestamp of billing period start
    period_end: u64       # Unix timestamp of billing period end
    stake_wei: u256       # GEN locked by provider, in wei
    penalty_cap_pct: u32  # maximum penalty as % of stake, in basis points (10000 = 100%)
    active: bool          # False once stake is reclaimed or fully penalized


@allow_storage
@dataclass
class Claim:
    """
    Represents a consumer's breach claim against a registered SLA.
    """
    sla_id: str
    claimant: Address
    submitted_at: u64
    measured_availability_pct: u32   # consumer's measured availability in basis points
    incident_description: str
    # stored as JSON array of URL strings, max 5
    evidence_urls_json: str
    # verdict fields, populated after adjudication
    adjudicated: bool
    breach_confirmed: bool
    measured_deficit_bps: u32        # shortfall below threshold in basis points
    penalty_wei: u256                # penalty amount calculated by the contract
    penalty_executed: bool
    verdict_reasoning: str


# ---------------------------------------------------------------------------
# Helper functions for defensive parsing
# ---------------------------------------------------------------------------

def _clean_json(raw: typing.Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        first = txt.find("{")
        last = txt.rfind("}")
        if first >= 0 and last > first:
            txt = txt[first : last + 1]
        txt = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", txt)
        loaded = json.loads(txt)
        if isinstance(loaded, dict):
            return loaded
    raise gl.vm.UserError("[LLM_ERROR] LLM reply was not a JSON object")


def _coerce_bool(raw: typing.Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    txt = str(raw).strip().lower()
    if txt in ("true", "yes", "1", "breach", "confirmed"):
        return True
    if txt in ("false", "no", "0", "no_breach", "rejected", "dismissed"):
        return False
    return False


def _payout_fields(verdict: dict) -> tuple[bool, int]:
    breach_confirmed = verdict.get("breach_confirmed")
    deficit_bps = verdict.get("measured_deficit_bps")
    if not isinstance(breach_confirmed, bool):
        raise gl.vm.UserError("[LLM_ERROR] Verdict breach_confirmed must be a boolean")
    if not isinstance(deficit_bps, int) or isinstance(deficit_bps, bool):
        raise gl.vm.UserError("[LLM_ERROR] Verdict measured_deficit_bps must be an integer")
    if not 0 <= deficit_bps <= 10000:
        raise gl.vm.UserError("[LLM_ERROR] Verdict deficit must be between 0 and 10000 bps")
    if not breach_confirmed and deficit_bps != 0:
        raise gl.vm.UserError("[LLM_ERROR] A no-breach verdict must have zero deficit")
    return breach_confirmed, deficit_bps


# ---------------------------------------------------------------------------
# Main contract
# ---------------------------------------------------------------------------

class SLAEnforcementOracle(gl.Contract):
    """
    SLA Enforcement Oracle - GenLayer primitive for on-chain SLA adjudication.

    See module docstring for full design documentation.
    """

    # SLA ID (str) -> SLARecord
    sla_records: TreeMap[str, SLARecord]

    # SLA ID -> number of claims filed
    claim_count: TreeMap[str, u32]

    # composite key "<sla_id>:<claim_index>" -> Claim
    claims: TreeMap[str, Claim]

    # total stake held per provider address (in wei)
    provider_stake: TreeMap[Address, u256]

    # contract deployer
    owner: Address

    def __init__(self) -> None:
        self.owner = gl.message.sender_address

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise gl.vm.UserError(message)

    def _now(self) -> u64:
        try:
            if hasattr(gl, "message_raw") and isinstance(gl.message_raw, dict) and "datetime" in gl.message_raw:
                txt = str(gl.message_raw["datetime"]).strip()
                if txt:
                    if txt.endswith("Z"):
                        txt = txt[:-1] + "+00:00"
                    dt = datetime.fromisoformat(txt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return u64(int(dt.timestamp()))
            return u64(int(datetime.now(timezone.utc).timestamp()))
        except Exception:
            return u64(0)

    def _sla_key(self, provider: str, period_start: int) -> str:
        """
        Deterministic SLA identifier derived from provider address and billing
        period start time. Ensures each (provider, period) pair maps to a
        unique record.
        """
        return f"{provider.lower()}:{period_start}"

    def _claim_key(self, sla_id: str, claim_index: int) -> str:
        return f"{sla_id}:{claim_index}"

    def _compute_penalty(self, stake_wei: int, penalty_cap_bps: int, threshold_bps: int, measured_bps: int) -> int:
        """
        Proportional penalty formula:

            deficit_bps = threshold_bps - measured_bps
            penalty_fraction = deficit_bps / threshold_bps
            penalty = stake * penalty_fraction
            penalty = min(penalty, stake * penalty_cap_bps / 10000)

        All arithmetic in integer basis points to avoid floating point in storage.
        """
        deficit_bps = threshold_bps - measured_bps
        if deficit_bps <= 0:
            return 0
        penalty = (stake_wei * deficit_bps) // threshold_bps
        cap = (stake_wei * penalty_cap_bps) // 10000
        return min(penalty, cap)

    # ------------------------------------------------------------------
    # Provider: register an SLA and lock stake
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def register_sla(
        self,
        sla_url: str,
        threshold_pct_bps: int,
        period_start: int,
        period_end: int,
        consumer_address: str,
        penalty_cap_pct_bps: int,
    ) -> str:
        """
        Register a Service Level Agreement and lock the penalty stake.

        Parameters
        ----------
        sla_url : str
            Publicly accessible URL of the SLA document. Validators will fetch
            this during adjudication. It must remain accessible for the lifetime
            of the billing period.
        threshold_pct_bps : int
            Availability threshold in basis points. 9990 = 99.90%. Must be
            between 1 and 10000.
        period_start : int
            Unix timestamp of the billing period start.
        period_end : int
            Unix timestamp of the billing period end. Must be > period_start.
        consumer_address : str
            Ethereum-style address of the service consumer who can file claims.
        penalty_cap_pct_bps : int
            Maximum penalty the consumer can claim as a percentage of the stake,
            in basis points. 5000 = 50%. Must be between 1 and 10000.

        Returns
        -------
        str
            The SLA ID, which must be supplied in subsequent claim calls.
        """
        provider = gl.message.sender_address
        stake = gl.message.value

        self._require(stake > u256(0), "Stake must be non-zero; send GEN with this call")
        self._require(1 <= threshold_pct_bps <= 10000, "Threshold must be between 1 and 10000 bps")
        self._require(1 <= penalty_cap_pct_bps <= 10000, "Penalty cap must be between 1 and 10000 bps")
        self._require(period_end > period_start, "period_end must be after period_start")
        self._require(len(sla_url) >= 8, "SLA URL must be a valid HTTP URL")

        consumer = Address(consumer_address)
        sla_id = self._sla_key(str(provider), period_start)

        self._require(sla_id not in self.sla_records, "An SLA for this provider and period already exists")

        record = SLARecord(
            provider=provider,
            consumer=consumer,
            sla_url=sla_url,
            threshold_pct=u32(threshold_pct_bps),
            period_start=u64(period_start),
            period_end=u64(period_end),
            stake_wei=stake,
            penalty_cap_pct=u32(penalty_cap_pct_bps),
            active=True,
        )
        self.sla_records[sla_id] = record
        self.claim_count[sla_id] = u32(0)

        existing = int(self.provider_stake.get(provider, u256(0)))
        self.provider_stake[provider] = u256(existing + int(stake))

        return sla_id

    # ------------------------------------------------------------------
    # Consumer: submit a breach claim
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_claim(
        self,
        sla_id: str,
        evidence_urls: str,
        measured_availability_bps: int,
        incident_description: str,
    ) -> str:
        """
        Submit a breach claim against a registered SLA.

        The claim triggers GenLayer consensus: validators independently fetch
        the SLA document and all evidence URLs, read the SLA terms, and
        adjudicate whether a breach occurred.

        Parameters
        ----------
        sla_id : str
            The SLA ID returned by `register_sla`.
        evidence_urls : str
            JSON array (as string) of up to five publicly accessible URLs that
            support the breach claim.
        measured_availability_bps : int
            The consumer's measured availability in basis points for the billing
            period. For example, 9850 = 98.50%.
        incident_description : str
            Human-readable description of the incident.

        Returns
        -------
        str
            JSON with the claim key and adjudication verdict.
        """
        sender = gl.message.sender_address

        self._require(sla_id in self.sla_records, "SLA ID not found")
        record = self.sla_records[sla_id]

        self._require(record.active, "This SLA is no longer active")
        self._require(sender == record.consumer, "Only the designated consumer may file claims")
        self._require(
            0 <= measured_availability_bps <= 10000,
            "measured_availability_bps must be between 0 and 10000"
        )
        self._require(len(incident_description) >= 10, "Incident description is too short")

        try:
            urls_list = json.loads(evidence_urls)
        except Exception:
            raise gl.vm.UserError("evidence_urls must be a valid JSON array of URL strings")

        self._require(isinstance(urls_list, list), "evidence_urls must be a JSON array")
        self._require(1 <= len(urls_list) <= 5, "Provide between 1 and 5 evidence URLs")
        for u in urls_list:
            self._require(isinstance(u, str) and len(u) >= 8, "Each evidence URL must be a string")

        claim_index = int(self.claim_count.get(sla_id, u32(0)))
        claim_key = self._claim_key(sla_id, claim_index)

        sla_url_mem = str(record.sla_url)
        threshold_bps_mem = int(record.threshold_pct)
        stake_mem = int(record.stake_wei)
        penalty_cap_bps_mem = int(record.penalty_cap_pct)
        period_start_mem = int(record.period_start)
        period_end_mem = int(record.period_end)

        # -------------------------------------------------------------------
        # Non-deterministic adjudication block
        # -------------------------------------------------------------------

        def leader_fn() -> dict:
            try:
                sla_response = gl.nondet.web.get(sla_url_mem)
                raw_sla_body = getattr(sla_response, "body", b"")
                if isinstance(raw_sla_body, bytes):
                    sla_text = raw_sla_body.decode("utf-8", errors="replace")[:8000]
                else:
                    sla_text = str(raw_sla_body)[:8000]
            except Exception as e:
                raise gl.vm.UserError(f"[EXTERNAL] SLA document unavailable: {e}")

            evidence_texts = []
            for ev_url in urls_list:
                try:
                    ev_resp = gl.nondet.web.get(str(ev_url))
                    raw_body = getattr(ev_resp, "body", b"")
                    if isinstance(raw_body, bytes):
                        ev_text = raw_body.decode("utf-8", errors="replace")[:3000]
                    else:
                        ev_text = str(raw_body)[:3000]
                    status_code = getattr(ev_resp, "status", 200)
                    evidence_texts.append({"url": ev_url, "content": ev_text, "status": status_code})
                except Exception as ex:
                    evidence_texts.append({"url": ev_url, "content": "", "error": str(ex), "status": 0})

            evidence_summary = json.dumps(evidence_texts, ensure_ascii=False)[:8000]

            prompt = f"""You are an impartial SLA adjudicator. Your task is to determine whether the evidence supports a breach of the Service Level Agreement.

SLA DOCUMENT:
{sla_text}

CONSUMER'S CLAIM:
- Billing period: Unix {period_start_mem} to Unix {period_end_mem}
- Consumer's measured availability: {measured_availability_bps / 100:.2f}%
- Agreed SLA threshold: {threshold_bps_mem / 100:.2f}%
- Incident description: {incident_description}

EVIDENCE PROVIDED:
{evidence_summary}

ADJUDICATION TASK:
1. Read the SLA document carefully to understand:
   - What constitutes the service availability metric
   - Any maintenance window exclusions
   - The definition of availability (which regions count, what type of failure counts)
   - Any notice requirements or claim procedures
2. Evaluate each piece of evidence:
   - Does it confirm the outage or degradation?
   - Does it cover the correct time period?
   - Is it from a credible, independent source?
3. Consider whether:
   - The consumer's measured availability figure is supported by the evidence
   - Any exclusions in the SLA apply (scheduled maintenance, force majeure, etc.)
   - The deficit (if any) is genuinely attributable to the provider's service

Respond with a JSON object with exactly these fields:
{{
  "breach_confirmed": true or false,
  "measured_deficit_bps": integer (basis points below threshold; 0 if no breach),
  "evidence_quality": "strong" or "moderate" or "weak",
  "exclusions_apply": true or false,
  "reasoning": "concise factual explanation, max 300 words",
  "sla_availability_definition": "brief quote or paraphrase of how SLA defines availability"
}}

Be conservative: confirm a breach only when the evidence clearly and unambiguously supports it.
If evidence is ambiguous, incomplete, or could reflect client-side issues rather than provider failure, set breach_confirmed to false.
"""

            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            parsed = _clean_json(raw)

            breach_confirmed = _coerce_bool(parsed.get("breach_confirmed", False))
            raw_deficit = parsed.get("measured_deficit_bps", 0)
            try:
                measured_deficit_bps = int(raw_deficit)
            except Exception:
                measured_deficit_bps = 0

            if not breach_confirmed:
                measured_deficit_bps = 0

            measured_deficit_bps = max(0, min(10000, measured_deficit_bps))

            evidence_quality = str(parsed.get("evidence_quality", "moderate")).strip().lower()
            if evidence_quality not in ("strong", "moderate", "weak"):
                evidence_quality = "moderate"

            reasoning = str(parsed.get("reasoning", "")).strip()[:500]
            sla_def = str(parsed.get("sla_availability_definition", "")).strip()[:300]

            return {
                "breach_confirmed": breach_confirmed,
                "measured_deficit_bps": measured_deficit_bps,
                "evidence_quality": evidence_quality,
                "reasoning": reasoning,
                "sla_availability_definition": sla_def,
            }

        def validator_fn(leaders_res: typing.Any) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False

            leaders_raw = getattr(leaders_res, "calldata", None)
            if not isinstance(leaders_raw, dict):
                try:
                    leaders_dict = _clean_json(leaders_raw)
                except Exception:
                    return False
            else:
                leaders_dict = leaders_raw

            try:
                leader_breach, leader_deficit = _payout_fields(leaders_dict)
                mine = leader_fn()
                my_breach, my_deficit = _payout_fields(mine)
            except Exception:
                return False

            return leader_breach == my_breach and leader_deficit == my_deficit

        jury_result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        if hasattr(jury_result, "get") and not isinstance(jury_result, dict):
            try:
                verdict = jury_result.get()
            except TypeError:
                verdict = jury_result
        else:
            verdict = jury_result

        if not isinstance(verdict, dict):
            verdict = _clean_json(verdict)

        # -------------------------------------------------------------------
        # Deterministic: compute penalty and persist verdict
        # -------------------------------------------------------------------

        breach_confirmed, deficit_bps = _payout_fields(verdict)

        reasoning = str(verdict.get("reasoning", ""))[:500]
        evidence_quality = str(verdict.get("evidence_quality", "unknown"))

        penalty_wei = 0
        if breach_confirmed and deficit_bps > 0:
            penalty_wei = self._compute_penalty(
                stake_mem,
                penalty_cap_bps_mem,
                threshold_bps_mem,
                threshold_bps_mem - deficit_bps,
            )

        now_ts = self._now()

        claim = Claim(
            sla_id=sla_id,
            claimant=sender,
            submitted_at=now_ts,
            measured_availability_pct=u32(measured_availability_bps),
            incident_description=incident_description[:500],
            evidence_urls_json=evidence_urls,
            adjudicated=True,
            breach_confirmed=breach_confirmed,
            measured_deficit_bps=u32(deficit_bps),
            penalty_wei=u256(penalty_wei),
            penalty_executed=False,
            verdict_reasoning=reasoning,
        )

        self.claims[claim_key] = claim
        self.claim_count[sla_id] = u32(claim_index + 1)

        result = {
            "claim_key": claim_key,
            "claim_index": claim_index,
            "sla_id": sla_id,
            "breach_confirmed": breach_confirmed,
            "measured_deficit_bps": deficit_bps,
            "penalty_wei": str(penalty_wei),
            "evidence_quality": evidence_quality,
            "verdict_reasoning": reasoning,
        }
        return json.dumps(result, sort_keys=True)

    # ------------------------------------------------------------------
    # Execute penalty transfer (after finalized adjudication)
    # ------------------------------------------------------------------

    @gl.public.write
    def execute_penalty(self, claim_key: str) -> str:
        """
        Execute the penalty transfer for a confirmed SLA breach.

        Parameters
        ----------
        claim_key : str
            The claim key returned by `submit_claim` (format: "<sla_id>:<index>").

        Returns
        -------
        str
            JSON confirmation with transferred amount.
        """
        self._require(claim_key in self.claims, "Claim not found")
        claim = self.claims[claim_key]

        self._require(claim.adjudicated, "Claim has not been adjudicated yet")
        self._require(claim.breach_confirmed, "No breach was confirmed for this claim")
        self._require(not claim.penalty_executed, "Penalty has already been executed")

        sla_id = claim.sla_id
        self._require(sla_id in self.sla_records, "Associated SLA not found")
        record = self.sla_records[sla_id]

        penalty = int(claim.penalty_wei)
        self._require(penalty > 0, "Penalty amount is zero")

        provider_current = int(self.provider_stake.get(record.provider, u256(0)))
        self._require(provider_current >= penalty, "Insufficient provider stake for this penalty")
        self.provider_stake[record.provider] = u256(provider_current - penalty)

        current_stake = int(record.stake_wei)
        self._require(current_stake >= penalty, "SLA stake insufficient for penalty")
        record.stake_wei = u256(current_stake - penalty)
        self.sla_records[sla_id] = record

        claim.penalty_executed = True
        self.claims[claim_key] = claim

        consumer_iface = gl.get_contract_at(record.consumer)
        consumer_iface.emit_transfer(value=u256(penalty), on="finalized")

        return json.dumps({
            "claim_key": claim_key,
            "penalty_wei": str(penalty),
            "consumer": str(record.consumer),
            "provider_remaining_stake_wei": str(int(record.stake_wei)),
        }, sort_keys=True)

    # ------------------------------------------------------------------
    # Provider: reclaim remaining stake after billing period
    # ------------------------------------------------------------------

    @gl.public.write
    def reclaim_stake(self, sla_id: str) -> str:
        """
        Reclaim the remaining stake after the billing period has ended.

        Parameters
        ----------
        sla_id : str
            The SLA ID.

        Returns
        -------
        str
            JSON confirmation with reclaimed amount.
        """
        sender = gl.message.sender_address

        self._require(sla_id in self.sla_records, "SLA not found")
        record = self.sla_records[sla_id]

        self._require(sender == record.provider, "Only the provider may reclaim the stake")
        self._require(record.active, "Stake already reclaimed")

        num_claims = int(self.claim_count.get(sla_id, u32(0)))
        for i in range(num_claims):
            ckey = self._claim_key(sla_id, i)
            if ckey in self.claims:
                cl = self.claims[ckey]
                if cl.breach_confirmed and not cl.penalty_executed:
                    raise gl.vm.UserError(f"Claim {ckey} has a confirmed breach with unexecuted penalty")

        remaining = int(record.stake_wei)

        if remaining > 0:
            provider_total = int(self.provider_stake.get(sender, u256(0)))
            new_total = max(0, provider_total - remaining)
            self.provider_stake[sender] = u256(new_total)

            provider_iface = gl.get_contract_at(sender)
            provider_iface.emit_transfer(value=u256(remaining), on="finalized")

        record.active = False
        record.stake_wei = u256(0)
        self.sla_records[sla_id] = record

        return json.dumps({
            "sla_id": sla_id,
            "reclaimed_wei": str(remaining),
            "provider": str(sender),
        }, sort_keys=True)

    # ------------------------------------------------------------------
    # View methods
    # ------------------------------------------------------------------

    @gl.public.view
    def get_sla(self, sla_id: str) -> str:
        """
        Retrieve a registered SLA record.

        Parameters
        ----------
        sla_id : str
            The SLA ID.

        Returns
        -------
        str
            JSON with SLA details, or empty string if not found.
        """
        if sla_id not in self.sla_records:
            return ""
        record = self.sla_records[sla_id]
        return json.dumps({
            "sla_id": sla_id,
            "provider": str(record.provider),
            "consumer": str(record.consumer),
            "sla_url": record.sla_url,
            "threshold_pct_bps": int(record.threshold_pct),
            "period_start": int(record.period_start),
            "period_end": int(record.period_end),
            "stake_wei": str(int(record.stake_wei)),
            "penalty_cap_pct_bps": int(record.penalty_cap_pct),
            "active": record.active,
        }, sort_keys=True)

    @gl.public.view
    def get_claim(self, claim_key: str) -> str:
        """
        Retrieve a claim record with its adjudication verdict.

        Parameters
        ----------
        claim_key : str
            The claim key returned by `submit_claim`.

        Returns
        -------
        str
            JSON with full claim details including verdict, or empty string if
            the claim is not found.
        """
        if claim_key not in self.claims:
            return ""
        cl = self.claims[claim_key]
        return json.dumps({
            "claim_key": claim_key,
            "sla_id": cl.sla_id,
            "claimant": str(cl.claimant),
            "submitted_at": int(cl.submitted_at),
            "measured_availability_pct_bps": int(cl.measured_availability_pct),
            "incident_description": cl.incident_description,
            "evidence_urls": cl.evidence_urls_json,
            "adjudicated": cl.adjudicated,
            "breach_confirmed": cl.breach_confirmed,
            "measured_deficit_bps": int(cl.measured_deficit_bps),
            "penalty_wei": str(int(cl.penalty_wei)),
            "penalty_executed": cl.penalty_executed,
            "verdict_reasoning": cl.verdict_reasoning,
        }, sort_keys=True)

    @gl.public.view
    def get_claim_count(self, sla_id: str) -> int:
        """
        Return the number of claims filed against an SLA.

        Parameters
        ----------
        sla_id : str
            The SLA ID.

        Returns
        -------
        int
            Number of claims.
        """
        return int(self.claim_count.get(sla_id, u32(0)))

    @gl.public.view
    def get_provider_stake(self, provider_address: str) -> str:
        """
        Return the total GEN (in wei) currently locked by a provider address.

        Parameters
        ----------
        provider_address : str
            The provider's Ethereum-style address.

        Returns
        -------
        str
            Stake amount in wei as a string.
        """
        addr = Address(provider_address)
        return str(int(self.provider_stake.get(addr, u256(0))))

    @gl.public.view
    def compute_max_penalty(self, sla_id: str) -> str:
        """
        Compute the maximum possible penalty for an SLA given a total breach.

        Parameters
        ----------
        sla_id : str
            The SLA ID.

        Returns
        -------
        str
            JSON with the cap amount in wei and as a percentage of stake.
        """
        if sla_id not in self.sla_records:
            return ""
        record = self.sla_records[sla_id]
        stake = int(record.stake_wei)
        cap_bps = int(record.penalty_cap_pct)
        max_penalty = (stake * cap_bps) // 10000
        return json.dumps({
            "sla_id": sla_id,
            "stake_wei": str(stake),
            "penalty_cap_pct_bps": cap_bps,
            "max_penalty_wei": str(max_penalty),
        }, sort_keys=True)
