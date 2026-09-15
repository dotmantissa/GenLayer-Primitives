# GenLayer Primitives

A repository of production-grade Intelligent Contract primitives designed for the GenLayer ecosystem.

## Overview

GenLayer introduces Intelligent Contracts: blockchain contracts that execute Python code, connect natively to web APIs, and leverage Large Language Models (LLMs) through decentralized validator consensus (Optimistic Democracy).

This repository houses reusable, production-ready primitives that leverage GenLayer's non-deterministic execution and consensus model, including:
- Natural language contract and policy adjudication
- Decentralized web evidence retrieval and verification
- Multi-validator equivalence principles
- Autonomous financial settlement

## Available Primitives

Each primitive is developed and maintained on its own dedicated branch containing the complete contract, test suite, and integration documentation:

- **SLA Enforcement Oracle** (Branch: `sla-enforcement-oracle`):
  Autonomous Service Level Agreement adjudication and penalty enforcement on-chain. Service providers lock performance stakes, consumers submit claims with public web evidence, and GenLayer validator committees independently adjudicate breaches and transfer penalties.

- **Agent Task Escrow** (Branch: `agent-task-escrow`):
  Autonomous task escrow and deliverable adjudication primitive for the agentic economy. Clients deposit escrow funds with natural-language task specifications and explicit acceptance criteria rubrics. GenLayer validator committees independently retrieve deliverables, score each acceptance criterion on substantive compliance, calculate proportional partial payouts, and execute on-chain settlement.
