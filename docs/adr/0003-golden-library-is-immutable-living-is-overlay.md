# ADR-0003: The golden library is immutable; Living is an overlay

- Status: Proposed
- Date: 2026-10-09

## Context
The library must stay a known-good backup. The secondary project lets models change content continuously.

## Decision
Models never write to golden storage. Whisipedia Living stores patches in an append-only log in its own storage area and materializes documents as `base ⊕ accepted patches`. Living gets golden data only through the public read API and change feed, or through a read-only mount under separate OS credentials.

## Consequences
Every model contribution can be reverted and audited. Living needs rebase logic when the base changes.
