"""Announces the server on the local network so speakers find it without any setup.

mDNS / DNS-SD: a `_harkenn._tcp` service whose TXT record carries the server's public URL, and the host name
`harkenn.local`. Speakers try, in order: a URL saved in their setup page, this service, `harkenn.local`, and the
plain host name `harkenn` (a DNS record you can add in your router when mDNS doesn't reach the speakers, e.g. across
VLANs or from a Kubernetes cluster).

mDNS only works when the container shares the host's network (docker-compose.yml uses network_mode: host).
"""
from __future__ import annotations

import logging
import socket

from .settings import Settings, env

log = logging.getLogger("harkenn.discovery")

SERVICE = "_harkenn._tcp.local."
HOSTNAME = "harkenn.local."


def primary_ipv4() -> str:
    """The address of the interface that routes outward (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, only selects the interface
        return s.getsockname()[0]


class Announcer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._zc = None

    async def start(self) -> None:
        try:
            from zeroconf import IPVersion, ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf

            ip = primary_ipv4()
            port = int(env("PORT", "8000"))
            info = ServiceInfo(SERVICE, f"Harkenn on {socket.gethostname()}.{SERVICE}", port=port,
                               properties={"url": self.settings.public_url, "path": "/"}, server=HOSTNAME,
                               parsed_addresses=[ip])
            self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self._zc.async_register_service(info, allow_name_change=True)
            log.info("announced %s on %s:%d (%s)", SERVICE, ip, port, self.settings.public_url)
        except Exception as e:  # no multicast (e.g. a container without host networking): speakers use DNS instead
            log.warning("mDNS announcement unavailable: %s", e)
            self._zc = None

    async def stop(self) -> None:
        if self._zc:
            await self._zc.async_unregister_all_services()
            await self._zc.async_close()
