"""SOAP transport that never debug-logs credential-bearing request or response XML."""

import logging

from zeep.transports import AsyncTransport


class _PrivateTransportLog(logging.Filter):
    """Protect SDK-created discovery/time-adjustment transports as well as ours."""

    def filter(self, record):
        record.msg = "ONVIF SOAP transport details redacted"
        record.args = ()
        return True


_logger = logging.getLogger("zeep.transports")
if not any(isinstance(item, _PrivateTransportLog) for item in _logger.filters):
    _logger.addFilter(_PrivateTransportLog())


class PrivateSoapTransport(AsyncTransport):
    """Retain Zeep's response conversion without its raw SOAP body debug logging."""

    async def post(self, address, message, headers):
        """Send once through the owned HTTP client without logging XML or headers."""
        return await self.client.post(address, content=message, headers=headers)
