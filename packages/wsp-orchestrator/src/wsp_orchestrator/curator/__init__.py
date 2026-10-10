"""Curator bots and the proposal/registry plumbing they feed (PLAN §5.3-§5.5)."""

from wsp_orchestrator.curator.gaps import GapMiner
from wsp_orchestrator.curator.health import AdapterSmokeBot, RegistryHealthBot
from wsp_orchestrator.curator.kiwix import KiwixWatcher
from wsp_orchestrator.curator.proposals import ProposalFiles, ProposalRecord
from wsp_orchestrator.curator.wikimedia import WikimediaRankingBot

__all__ = [
    "AdapterSmokeBot",
    "GapMiner",
    "KiwixWatcher",
    "ProposalFiles",
    "ProposalRecord",
    "RegistryHealthBot",
    "WikimediaRankingBot",
]
