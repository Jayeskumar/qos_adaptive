"""Queueing disciplines modelled after their Linux counterparts.

FIFO      - drop-tail (pfifo/bfifo): what most ISP CPE and modems run ("standard")
FQCoDel   - RFC 8290 flow-queue CoDel (Linux/OpenWrt default for SQM)
CakeLite  - CAKE-style: FQ-CoDel with per-host fairness (triple-isolate quantum scaling)
HTB       - hierarchical token bucket (Linux sch_htb semantics: rate/ceil/prio/borrowing)
"""
from .base import Qdisc
from .fifo import FIFO
from .fq_codel import FQCoDel, CakeLite
from .htb import HTB, HTBClass

__all__ = ["Qdisc", "FIFO", "FQCoDel", "CakeLite", "HTB", "HTBClass"]
