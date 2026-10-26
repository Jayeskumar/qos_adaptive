"""FlowPilot - an adaptive, learning QoS engine for mixed home-broadband traffic.

Package layout
--------------
common/      shared vocabulary: app types, traffic classes, flow features, QoE metrics
sim/         packet-level discrete-event simulator (digital twin) with qdisc models
testbed/     real Linux emulation: network namespaces, tc/HTB, nftables, live traffic
controller/  the FlowPilot brain: classify -> estimate demand -> track capacity -> allocate
ml/          dataset generation, classifier training, reinforcement-learning autorate agent
bench/       reproducible benchmark harness and report figures
db/          SQLAlchemy models (PostgreSQL in production, SQLite fallback)
api/         FastAPI service (REST + WebSocket) that drives sessions for the dashboard
"""

__version__ = "1.0.0"
