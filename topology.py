#!/usr/bin/env python3
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


def build_network():
    net = Mininet(controller=RemoteController, switch=OVSKernelSwitch, link=TCLink)

    info('*** Adding Remote SDN Controller on port 6653\n')
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    info('*** Adding OpenFlow 1.3 Switches\n')
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')

    info('*** Adding Hosts\n')
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')

    # Diamond layout:
    #              s2 (10ms + 10ms = 20ms — Path A, default)
    #   h1 -- s1 -+                                          +- s4 -- h2, h3, h4
    #              s3 ( 2ms +  2ms =  4ms — Path B, fast)
    #
    # Port numbering follows addLink call order.
    # If you reorder these links, update PORT_TO and HOSTS in sdn_controller.py.
    info('*** Creating Diamond Links\n')
    net.addLink(h1, s1, cls=TCLink, bw=100)                         # s1 port 1
    net.addLink(s1, s2, cls=TCLink, bw=50, delay='10ms')            # s1 port 2, s2 port 1
    net.addLink(s2, s4, cls=TCLink, bw=50, delay='10ms')            # s2 port 2, s4 port 1
    net.addLink(s1, s3, cls=TCLink, bw=50, delay='2ms')             # s1 port 3, s3 port 1
    net.addLink(s3, s4, cls=TCLink, bw=50, delay='2ms')             # s3 port 2, s4 port 2
    net.addLink(h2, s4, cls=TCLink, bw=100)                         # s4 port 3
    net.addLink(h3, s4, cls=TCLink, bw=100)                         # s4 port 4
    net.addLink(h4, s4, cls=TCLink, bw=100)                         # s4 port 5

    info('*** Starting Network\n')
    net.start()

    try:
        net.waitConnected(timeout=10)
    except Exception:
        pass

    info('*** Applying Static ARP and Disabling IPv6\n')
    net.staticArp()
    for host in net.hosts:
        host.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        host.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1')

    return net


def create_diamond_topology():
    net = build_network()
    info('*** Topology ready. Entering Mininet CLI.\n')
    CLI(net)
    info('*** Stopping Network\n')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    create_diamond_topology()
