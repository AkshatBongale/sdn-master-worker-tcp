import os
import json
import socket
import logging

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('SDNController')

SOCKET_PATH      = '/tmp/sdn_events.sock'
EXPECTED_SWITCHES = 4
QUARANTINE_SECONDS = 10

# Set SDN_MODE=static to run the baseline (no reactions to events)
SDN_MODE = os.environ.get('SDN_MODE', 'sdn').lower()

MASTER_IP = '10.0.0.1'

# host IP -> (attached switch dpid, port on that switch)
# Port numbers follow the ORDER of addLink() calls in topology.py
HOSTS = {
    '10.0.0.1': (1, 1),
    '10.0.0.2': (4, 3),
    '10.0.0.3': (4, 4),
    '10.0.0.4': (4, 5),
}

# (switch, neighbour switch) -> port on that switch
PORT_TO = {
    (1, 2): 2, (1, 3): 3,
    (2, 1): 1, (2, 4): 2,
    (3, 1): 1, (3, 4): 2,
    (4, 2): 1, (4, 3): 2,
}

# link delay in ms (must match topology.py)
LINK_DELAY_MS = {
    frozenset((1, 2)): 10, frozenset((2, 4)): 10,
    frozenset((1, 3)): 2,  frozenset((3, 4)): 2,
}

NEIGHBOR_AT  = {(sw, port): nbr for (sw, nbr), port in PORT_TO.items()}
GRAPH        = {}
for (_a, _b) in sorted(PORT_TO):
    GRAPH.setdefault(_a, []).append(_b)
MASTER_SWITCH, MASTER_PORT = HOSTS[MASTER_IP]


class DiamondSDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(DiamondSDNController, self).__init__(*args, **kwargs)
        self.datapaths    = {}
        self.down_links   = set()
        self.current_paths = {}
        self.overloaded   = set()
        LOG.info("Controller mode: %s", SDN_MODE.upper())
        hub.spawn(self._serve_events)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser   = datapath.ofproto_parser
        self.datapaths[datapath.id] = datapath
        LOG.info("Switch connected: DPID=%s (%d/%d)", datapath.id, len(self.datapaths), EXPECTED_SWITCHES)
        # table-miss = drop (never flood in a loop topology)
        self._add_flow(datapath, 0, parser.OFPMatch(), [])
        if len(self.datapaths) == EXPECTED_SWITCHES:
            self.install_all_flows("initial")

    def _add_flow(self, datapath, priority, match, actions, hard_timeout=0):
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match,
                                  instructions=inst, hard_timeout=hard_timeout)
        datapath.send_msg(mod)

    def compute_path(self, src, dst, metric='hops'):
        if metric == 'delay':
            return self._lowest_delay_path(src, dst)
        queue, visited = [[src]], {src}
        while queue:
            path = queue.pop(0)
            node = path[-1]
            if node == dst:
                return path
            for nbr in GRAPH.get(node, []):
                if nbr in visited or frozenset((node, nbr)) in self.down_links:
                    continue
                visited.add(nbr)
                queue.append(path + [nbr])
        return None

    def _lowest_delay_path(self, src, dst):
        dist, prev, todo, done = {src: 0}, {}, {src}, set()
        while todo:
            node = min(todo, key=lambda n: dist[n])
            todo.discard(node)
            if node == dst:
                break
            done.add(node)
            for nbr in GRAPH.get(node, []):
                link = frozenset((node, nbr))
                if nbr in done or link in self.down_links:
                    continue
                nd = dist[node] + LINK_DELAY_MS[link]
                if nbr not in dist or nd < dist[nbr]:
                    dist[nbr], prev[nbr] = nd, node
                    todo.add(nbr)
        if dst not in dist:
            return None
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return path[::-1]

    def install_all_flows(self, reason):
        if len(self.datapaths) < EXPECTED_SWITCHES:
            return
        for ip, (dst_sw, dst_port) in HOSTS.items():
            if ip == MASTER_IP:
                continue
            metric = 'delay' if ip in self.overloaded else 'hops'
            path   = self.compute_path(MASTER_SWITCH, dst_sw, metric)
            if path is None:
                LOG.warning("[REROUTE] No path to %s - all routes are down", ip)
                continue
            old = self.current_paths.get(ip)
            self._program(path, ip, dst_port)
            self._program(list(reversed(path)), MASTER_IP, MASTER_PORT, src_ip=ip)
            if old is None:
                LOG.info("[PATH] master <-> %s via %s", ip, path)
            elif old != path:
                LOG.info("[REROUTE] master <-> %s: %s -> %s (%s)", ip, old, path, reason)
            self.current_paths[ip] = path

    def _program(self, path, dst_ip, final_port, src_ip=None):
        for i, sw in enumerate(path):
            out_port = PORT_TO[(sw, path[i + 1])] if i < len(path) - 1 else final_port
            dp = self.datapaths.get(sw)
            if dp is None:
                continue
            parser = dp.ofproto_parser
            fields = {'eth_type': 0x0800, 'ipv4_dst': dst_ip}
            if src_ip is not None:
                fields['ipv4_src'] = src_ip
            self._add_flow(dp, 10, parser.OFPMatch(**fields), [parser.OFPActionOutput(out_port)])

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        if SDN_MODE == 'static':
            return
        msg      = ev.msg
        dp       = msg.datapath
        ofproto  = dp.ofproto
        port_no  = msg.desc.port_no
        nbr      = NEIGHBOR_AT.get((dp.id, port_no))
        if nbr is None:
            return  # host-facing port, ignore
        is_down = (msg.reason == ofproto.OFPPR_DELETE
                   or bool(msg.desc.state  & ofproto.OFPPS_LINK_DOWN)
                   or bool(msg.desc.config & ofproto.OFPPC_PORT_DOWN))
        link = frozenset((dp.id, nbr))
        if is_down and link not in self.down_links:
            self.down_links.add(link)
            LOG.info("[LINK DOWN] s%s <-> s%s. Recomputing paths...", dp.id, nbr)
            self.install_all_flows("link down")
        elif not is_down and link in self.down_links:
            self.down_links.discard(link)
            LOG.info("[LINK UP] s%s <-> s%s. Recomputing paths...", dp.id, nbr)
            self.install_all_flows("link up")

    def _serve_events(self):
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o777)
        server.listen(5)
        LOG.info("SDN UNIX socket listening on %s", SOCKET_PATH)
        while True:
            conn, _ = server.accept()
            hub.spawn(self._handle_client, conn)

    def _handle_client(self, conn):
        try:
            data = b''
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            for line in data.decode('utf-8').splitlines():
                if line.strip():
                    self._handle_event(json.loads(line))
        except Exception as e:
            LOG.error("Socket error: %s", e)
        finally:
            conn.close()

    def _handle_event(self, ev):
        name, ip = ev.get('event'), ev.get('ip')
        if SDN_MODE == 'static':
            LOG.info("[STATIC MODE] ignoring application event %s for %s", name, ip)
            return
        if ip not in HOSTS or ip == MASTER_IP:
            return
        if name == 'WORKER_DEAD':
            self.overloaded.discard(ip)
            self._quarantine(ip)
            self.install_all_flows("worker dead")
        elif name == 'WORKER_OVERLOAD':
            LOG.info("[SOCKET EVENT] Worker OVERLOADED (%s): steering to low-delay path", ip)
            self.overloaded.add(ip)
            self.install_all_flows("worker overload")
        elif name == 'WORKER_NORMAL':
            LOG.info("[SOCKET EVENT] Worker back to NORMAL (%s): default path", ip)
            self.overloaded.discard(ip)
            self.install_all_flows("worker normal")

    def _quarantine(self, ip):
        LOG.info("[SOCKET EVENT] Worker DEAD (%s): dropping traffic for %ss", ip, QUARANTINE_SECONDS)
        for dp in self.datapaths.values():
            match = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_dst=ip)
            self._add_flow(dp, 20, match, [], hard_timeout=QUARANTINE_SECONDS)
