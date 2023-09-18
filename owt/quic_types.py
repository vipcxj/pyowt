import asyncio
from dataclasses import dataclass
import sys
if sys.version_info < (3, 11):
    from typing_extensions import Any, NotRequired, TypedDict, List, TextIO, Tuple, cast
else:
    from typing import Any, NotRequired, TypedDict, List, TextIO, Tuple, cast
from datetime import datetime
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.logger import QuicLogger
from aioquic.tls import CipherSuite, SessionTicket

@dataclass
class QuicBidirectionalStream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    lock: asyncio.Lock
    
CipherSuiteType = CipherSuite | str | int

def toCipherSuite(value: CipherSuiteType) -> CipherSuite:
    if isinstance(value, CipherSuite):
        return value
    if isinstance(value, int):
        return CipherSuite(value)
    return CipherSuite[value]

class SessionTicketDict(TypedDict):
    """
    A TLS session ticket for session resumption.
    """

    age_add: int
    cipher_suite: CipherSuiteType
    not_valid_after: str | float | datetime
    not_valid_before: str | float | datetime
    resumption_secret: bytes
    server_name: str
    ticket: bytes

    max_early_data_size: NotRequired[int | None]
    other_extensions: NotRequired[List[Tuple[int, bytes]]]
    
def toDatetime(value: str | float | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, float):
        return datetime.fromtimestamp(value)
    return datetime.fromisoformat(value)
    
def toSessionTicket(value: SessionTicket | SessionTicketDict) -> SessionTicket:
    if isinstance(value, SessionTicket):
        return value
    value = value.copy()
    value['cipher_suite'] = toCipherSuite(value['cipher_suite'])
    value['not_valid_after'] = toDatetime(value['not_valid_after'])
    value['not_valid_before'] = toDatetime(value['not_valid_before'])
    return SessionTicket(**cast(dict, value))
    
    
class QuicConfigurationDict(TypedDict):
    """
    A QUIC configuration.
    """

    alpn_protocols: NotRequired[List[str] | None]
    """
    A list of supported ALPN protocols.
    """

    connection_id_length: NotRequired[int]
    """
    The length in bytes of local connection IDs.
    """

    idle_timeout: NotRequired[float]
    """
    The idle timeout in seconds.

    The connection is terminated if nothing is received for the given duration.
    """

    is_client: NotRequired[bool]
    """
    Whether this is the client side of the QUIC connection.
    """

    max_data: NotRequired[int]
    """
    Connection-wide flow control limit.
    """

    max_stream_data: NotRequired[int]
    """
    Per-stream flow control limit.
    """

    quic_logger: NotRequired[QuicLogger | None]
    """
    The :class:`~aioquic.quic.logger.QuicLogger` instance to log events to.
    """

    secrets_log_file: NotRequired[TextIO | None]
    """
    A file-like object in which to log traffic secrets.

    This is useful to analyze traffic captures with Wireshark.
    """

    server_name: NotRequired[str | None]
    """
    The server name to send during the TLS handshake the Server Name Indication.

    .. note:: This is only used by clients.
    """

    session_ticket: NotRequired[SessionTicket | SessionTicketDict | None]
    """
    The TLS session ticket which should be used for session resumption.
    """

    cadata: NotRequired[bytes | None]
    cafile: NotRequired[str | None]
    capath: NotRequired[str | None]
    certificate: NotRequired[Any | None]
    certificate_chain: NotRequired[List[Any]]
    cipher_suites: NotRequired[List[CipherSuiteType] | None]
    initial_rtt: NotRequired[float]
    max_datagram_frame_size: NotRequired[int | None]
    private_key: NotRequired[Any | None]
    quantum_readiness_test: NotRequired[bool]
    supported_versions: NotRequired[List[int]]
    verify_mode: NotRequired[int | None]
    
def toQuicConfiguration(value: QuicConfiguration | QuicConfigurationDict) -> QuicConfiguration:
    if isinstance(value, QuicConfiguration):
        return value
    value = value.copy()
    session_ticket = value.get('session_ticket')
    if session_ticket is not None:
        value['session_ticket'] = toSessionTicket(session_ticket)
    cipher_suites = value.get('cipher_suites')
    if cipher_suites is not None:
        value['cipher_suites'] = [toCipherSuite(s) for s in cipher_suites]
    return QuicConfiguration(**cast(dict, value))