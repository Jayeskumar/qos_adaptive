#!/usr/bin/env bash
# One-time host preparation for the real-kernel testbed (Ubuntu/Debian, run as root).
set -euo pipefail
apt-get update -q
apt-get install -y -q iproute2 nftables conntrack ethtool iputils-ping iperf3
# allow namespaces to select CUBIC (most CDNs/servers) besides the host default
cur=$(cat /proc/sys/net/ipv4/tcp_allowed_congestion_control)
case " $cur " in *" cubic "*) ;; *) echo "$cur cubic" > /proc/sys/net/ipv4/tcp_allowed_congestion_control ;; esac
cd "$(dirname "$0")/../backend"
python3 - <<'PY'
from flowpilot.testbed.netns import capabilities
from flowpilot.testbed.topology import Topology
from flowpilot.testbed.netns import nsx
print("kernel capabilities:", capabilities())
t = Topology(); t.build(); t.start_delayline()
print(nsx("fp-dev-console", "ping -c 3 -i 0.2 203.0.113.10").splitlines()[-1])
t.stop_delayline(); Topology.teardown()
print("testbed OK")
PY
