import asyncio
import logging
import os
import ssl
import threading
import traceback
import struct
import numpy as np
import sys
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple
from functools import reduce

# TODO(bashi): Remove import check suppressions once aioquic dependency is resolved.
from aioquic.buffer import Buffer  # type: ignore
from aioquic.asyncio import QuicConnectionProtocol, serve  # type: ignore
from aioquic.asyncio.client import connect  # type: ignore
from aioquic.h3.connection import H3_ALPN, FrameType, H3Connection, ProtocolError, Setting  # type: ignore
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived, DatagramReceived, DataReceived  # type: ignore
from aioquic.quic.configuration import QuicConfiguration  # type: ignore
from aioquic.quic.connection import stream_is_unidirectional  # type: ignore
from aioquic.quic.events import QuicEvent, ProtocolNegotiated, ConnectionTerminated, StreamReset  # type: ignore
from aioquic.tls import SessionTicket  # type: ignore

from tools.wptserve.wptserve import stash  # type: ignore
from .capsule import H3Capsule, H3CapsuleDecoder, CapsuleType

"""
A WebTransport over HTTP/3 server for testing.

The server interprets the underlying protocols (WebTransport, HTTP/3 and QUIC)
and passes events to a particular webtransport handler. From the standpoint of
test authors, a webtransport handler is a Python script which contains some
callback functions. See handler.py for available callbacks.
"""

SERVER_NAME = 'webtransport-h3-server'

_logger: logging.Logger = logging.getLogger(__name__)
_doc_root: str = ""

connections = []
streamIds = []
global counter
counter = 0

class DataView:
    def __init__(self, array, bytes_per_element=1):
        """
        bytes_per_element is the size of each element in bytes.
        By default we are assume the array is one byte per element.
        """
        self.array = array
        self.bytes_per_element = 1  # because writeBuffer is uint8 array

    def __get_binary(self, start_index, byte_count, signed=False):
        integers = [self.array[start_index + x] for x in range(byte_count)]
        bytes = [integer.to_bytes(self.bytes_per_element, byteorder='big', signed=False) for integer in integers]
        return reduce(lambda a, b: a + b, bytes)

    def get_uint_32(self, start_index):
        bytes_to_read = 4
        return int.from_bytes(self.__get_binary(start_index, bytes_to_read), byteorder='big')   # big endian!!!!!!!!

def parse(array):
    # _logger.info("data size: %s", sys.getsizeof(array))
    dv = DataView(array)
    result = {
            "streamId": dv.get_uint_32(0),
            "sequenceNumber": dv.get_uint_32(4),
            "ts": dv.get_uint_32(8),
            "eof": dv.get_uint_32(12),
    }
    return result

class H3ConnectionWithDatagram04(H3Connection):
    """
    A H3Connection subclass, to make it work with the latest
    HTTP Datagram protocol.
    """
    H3_DATAGRAM_04 = 0xffd277

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._supports_h3_datagram_04 = False

    def _validate_settings(self, settings: Dict[int, int]) -> None:
        H3_DATAGRAM_04 = H3ConnectionWithDatagram04.H3_DATAGRAM_04
        if H3_DATAGRAM_04 in settings and settings[H3_DATAGRAM_04] == 1:
            settings[Setting.H3_DATAGRAM] = 1
            self._supports_h3_datagram_04 = True
        return super()._validate_settings(settings)

    def _get_local_settings(self) -> Dict[int, int]:
        H3_DATAGRAM_04 = H3ConnectionWithDatagram04.H3_DATAGRAM_04
        settings = super()._get_local_settings()
        settings[H3_DATAGRAM_04] = 1
        return settings

    @property
    def supports_h3_datagram_04(self) -> bool:
        """
        True if the client supports the latest HTTP Datagram protocol.
        """
        return self._supports_h3_datagram_04


class WebTransportH3Protocol(QuicConnectionProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._handler: Optional[Any] = None
        self._http: Optional[H3ConnectionWithDatagram04] = None
        self._session_stream_id: Optional[int] = None
        self._close_info: Optional[Tuple[int, bytes]] = None
        self._capsule_decoder_for_session_stream: H3CapsuleDecoder =\
            H3CapsuleDecoder()
        self._allow_calling_session_closed = True
        self._allow_datagrams = False

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated):
            self._http = H3ConnectionWithDatagram04(
                self._quic, enable_webtransport=True)
            if not self._http.supports_h3_datagram_04:
                self._allow_datagrams = True

        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self._h3_event_received(http_event)

        if isinstance(event, ConnectionTerminated):
            self._call_session_closed(close_info=None, abruptly=True)
        if isinstance(event, StreamReset):
            if self._handler:
                self._handler.stream_reset(event.stream_id, event.error_code)

    def _h3_event_received(self, event: H3Event) -> None:
        global counter
        if isinstance(event, HeadersReceived):
            _logger.info("event received")
            # Convert from List[Tuple[bytes, bytes]] to Dict[bytes, bytes].
            # Only the last header will be kept when there are duplicate
            # headers.
            headers = {}
            for header, value in event.headers:
                headers[header] = value

            _logger.info("H3Event: %s", event)
            method = headers.get(b":method")
            protocol = headers.get(b":protocol")
            if method == b"CONNECT" and protocol == b"webtransport":    # client connects
                self._session_stream_id = event.stream_id
                self._handshake_webtransport(event, headers)
            else:
                self._send_error_response(event.stream_id, 400)

        if isinstance(event, DataReceived) and\
           self._session_stream_id == event.stream_id:
            if self._http and not self._http.supports_h3_datagram_04 and\
               len(event.data) > 0:
                raise ProtocolError('Unexpected data on the session stream')
            self._receive_data_on_session_stream(
                event.data, event.stream_ended)
        elif self._handler is not None:
            if isinstance(event, WebTransportStreamDataReceived):
                if (len(streamIds) < 2):
                    streamIds.append(event.stream_id)
                self._handler.stream_data_received(
                    stream_id=event.stream_id,
                    data=event.data,
                    stream_ended=event.stream_ended)
            elif isinstance(event, DatagramReceived):
                if self._allow_datagrams:
                    self._handler.datagram_received(data=event.data)

    def _receive_data_on_session_stream(self, data: bytes, fin: bool) -> None:  # what is this?
        _logger.info("INTERESTING")
        self._capsule_decoder_for_session_stream.append(data)
        if fin:
            self._capsule_decoder_for_session_stream.final()
        for capsule in self._capsule_decoder_for_session_stream:
            if capsule.type in {CapsuleType.DATAGRAM,
                                CapsuleType.REGISTER_DATAGRAM_CONTEXT,
                                CapsuleType.CLOSE_DATAGRAM_CONTEXT}:
                raise ProtocolError(
                    "Unimplemented capsule type: {}".format(capsule.type))
            if capsule.type in {CapsuleType.REGISTER_DATAGRAM_NO_CONTEXT,
                                CapsuleType.CLOSE_WEBTRANSPORT_SESSION}:
                # We'll handle this case below.
                pass
            else:
                # We should ignore unknown capsules.
                continue

            if self._close_info is not None:
                raise ProtocolError((
                    "Receiving a capsule with type = {} after receiving " +
                    "CLOSE_WEBTRANSPORT_SESSION").format(capsule.type))

            if capsule.type == CapsuleType.REGISTER_DATAGRAM_NO_CONTEXT:
                buffer = Buffer(data=capsule.data)
                format_type = buffer.pull_uint_var()
                # https://ietf-wg-webtrans.github.io/draft-ietf-webtrans-http3/draft-ietf-webtrans-http3.html#name-datagram-format-type
                WEBTRANPORT_FORMAT_TYPE = 0xff7c00
                if format_type != WEBTRANPORT_FORMAT_TYPE:
                    raise ProtocolError(
                        "Unexpected datagram format type: {}".format(
                            format_type))
                self._allow_datagrams = True
            elif capsule.type == CapsuleType.CLOSE_WEBTRANSPORT_SESSION:
                buffer = Buffer(data=capsule.data)
                code = buffer.pull_uint32()
                # 4 bytes for the uint32.
                reason = buffer.pull_bytes(len(capsule.data) - 4)
                # TODO(yutakahirano): Make sure `reason` is a UTF-8 text.
                self._close_info = (code, reason)
                if fin:
                    self._call_session_closed(self._close_info, abruptly=False)

    def _send_error_response(self, stream_id: int, status_code: int) -> None:
        assert self._http is not None
        headers = [(b"server", SERVER_NAME.encode()),
                   (b":status", str(status_code).encode())]
        self._http.send_headers(stream_id=stream_id,
                                headers=headers,
                                end_stream=True)

    def _handshake_webtransport(self, event: HeadersReceived,
                                request_headers: Dict[bytes, bytes]) -> None:
        assert self._http is not None
        _logger.info("REQUEST HEADERS: %s", request_headers)
        path = request_headers.get(b":path")
        if path is None:
            # `:path` must be provided.
            self._send_error_response(event.stream_id, 400)
            return

        # Create a handler using `:path`.
        try:
            _logger.info("EVENT: %s", event)
            self._handler = self._create_event_handler(
                session_id=event.stream_id,
                path= b'/webtransport/handlers/custom-response.py?:status=200', # messy workaround, used to be path=path
                # path = path,
                request_headers=event.headers)
        except IOError:
            self._send_error_response(event.stream_id, 404)
            return

        response_headers = [
            (b"server", SERVER_NAME.encode()),
            (b"sec-webtransport-http3-draft", b"draft02"),
        ]
        self._handler.connect_received(response_headers=response_headers)

        status_code = None
        _logger.info("RESPONSE HEADERS: %s", response_headers)
        for name, value in response_headers:
            if name == b":status":
                status_code = value
                break
        if not status_code:
            response_headers.append((b":status", b"200"))
        self._http.send_headers(stream_id=event.stream_id,
                                headers=response_headers)

        if status_code is None or status_code == b"200":
            self._handler.session_established()

    def _create_event_handler(self, session_id: int, path: bytes,
                              request_headers: List[Tuple[bytes, bytes]]) -> Any:
        parsed = urlparse(path.decode())
        file_path = os.path.join(_doc_root, parsed.path.lstrip("/"))
        callbacks = {"__file__": file_path}
        with open(file_path) as f:
            exec(compile(f.read(), path, "exec"), callbacks)
        session = WebTransportSession(self, counter, request_headers)
        connections.append(session)
        _logger.info("-------------------------------------")
        _logger.info("CONNECTIONS: %s", connections)
        return WebTransportEventHandler(session, callbacks)

    def _call_session_closed(
            self, close_info: Optional[Tuple[int, bytes]],
            abruptly: bool) -> None:
        allow_calling_session_closed = self._allow_calling_session_closed
        self._allow_calling_session_closed = False
        if self._handler and allow_calling_session_closed:
            self._handler.session_closed(close_info, abruptly)


class WebTransportSession:
    """
    A WebTransport session.
    """

    def __init__(self, protocol: WebTransportH3Protocol, session_id: int,
                 request_headers: List[Tuple[bytes, bytes]]) -> None:
        self.session_id = session_id
        self.request_headers = request_headers

        self._protocol: WebTransportH3Protocol = protocol
        self._http: H3Connection = protocol._http

        # Use the a shared default path for all handlers so that different
        # WebTransport sessions can access the same store easily.
        self._stash_path = '/webtransport/handlers'
        self._stash: Optional[stash.Stash] = None
        self._dict_for_handlers: Dict[str, Any] = {}

    @property
    def stash(self) -> stash.Stash:
        """A Stash object for storing cross-session state."""
        if self._stash is None:
            address, authkey = stash.load_env_config()
            self._stash = stash.Stash(self._stash_path, address, authkey)
        return self._stash

    @property
    def dict_for_handlers(self) -> Dict[str, Any]:
        """A dictionary that handlers can attach arbitrary data."""
        return self._dict_for_handlers

    def stream_is_unidirectional(self, stream_id: int) -> bool:
        """Return True if the stream is unidirectional."""
        return stream_is_unidirectional(stream_id)

    def close(self, close_info: Optional[Tuple[int, bytes]]) -> None:
        """
        Close the session.

        :param close_info The close information to send.
        """
        self._protocol._allow_calling_session_closed = False
        assert self._protocol._session_stream_id is not None
        session_stream_id = self._protocol._session_stream_id
        if close_info is not None:
            code = close_info[0]
            reason = close_info[1]
            buffer = Buffer(capacity=len(reason) + 4)
            buffer.push_uint32(code)
            buffer.push_bytes(reason)
            capsule =\
                H3Capsule(CapsuleType.CLOSE_WEBTRANSPORT_SESSION, buffer.data)
            self._http.send_data(session_stream_id, capsule.encode(), end_stream=False)

        self._http.send_data(session_stream_id, b'', end_stream=True)
        # TODO(yutakahirano): Reset all other streams.
        # TODO(yutakahirano): Reject future stream open requests
        # We need to wait for the stream data to arrive at the client, and then
        # we need to close the connection. At this moment we're relying on the
        # client's behavior.
        # TODO(yutakahirano): Implement the above.

    def create_unidirectional_stream(self) -> int:
        _logger.info("created unidirectional stream")
        """
        Create a unidirectional WebTransport stream and return the stream ID.
        """
        return self._http.create_webtransport_stream(
            session_id=self.session_id, is_unidirectional=True)

    def create_bidirectional_stream(self) -> int:
        _logger.info("created bidirectional stream")
        """
        Create a bidirectional WebTransport stream and return the stream ID.
        """
        stream_id = self._http.create_webtransport_stream(
            session_id=self.session_id, is_unidirectional=False)
        _logger.info("STREAM ID: %d", stream_id)
        # TODO(bashi): Remove this workaround when aioquic supports receiving
        # data on server-initiated bidirectional streams.
        stream = self._http._get_or_create_stream(stream_id)
        assert stream.frame_type is None
        assert stream.session_id is None
        stream.frame_type = FrameType.WEBTRANSPORT_STREAM
        stream.session_id = self.session_id
        return stream_id

    def send_stream_data(self,
                         stream_id: int,
                         data: bytes,
                         end_stream: bool = False) -> None:
        """
        Send data on the specific stream.

        :param stream_id: The stream ID on which to send the data.
        :param data: The data to send.
        :param end_stream: If set to True, the stream will be closed.
        """
        # _logger.info("stream id: %s", stream_id)
        # _logger.info("END_STREAM: %s", end_stream)
        #_logger.info("conneciton stream_id vs standard one: %d %d", self._protocol._session_stream_id, stream_id)
        self._http._quic.send_stream_data(stream_id=stream_id,
                                          data=data,
                                          end_stream=end_stream)
        # _logger.info("stream data sent on")

    def send_datagram(self, data: bytes) -> None:
        """
        Send data using a datagram frame.

        :param data: The data to send.
        """
        if not self._protocol._allow_datagrams:
            _logger.warn(
                "Sending a datagram while that's now allowed - discarding it")
            return
        flow_id = self.session_id
        if self._http.supports_h3_datagram_04:
            # The REGISTER_DATAGRAM_NO_CONTEXT capsule was on the session
            # stream, so we must have the ID of the stream.
            assert self._protocol._session_stream_id is not None
            # TODO(yutakahirano): Make sure if this is the correct logic.
            # Chrome always use 0 for the initial stream and the initial flow
            # ID, we cannot check the correctness with it.
            flow_id = self._protocol._session_stream_id // 4
        # _logger.info("connection stream_id vs standard one: %d", flow_id)
        # _logger.info("flow id: %s", flow_id)
        self._http.send_datagram(flow_id=flow_id, data=data)

    def stop_stream(self, stream_id: int, code: int) -> None:
        """
        Send a STOP_SENDING frame to the given stream.
        :param code: the reason of the error.
        """
        self._http._quic.stop_stream(stream_id, code)

    def reset_stream(self, stream_id: int, code: int) -> None:
        """
        Send a RESET_STREAM frame to the given stream.
        :param code: the reason of the error.
        """
        self._http._quic.reset_stream(stream_id, code)


class WebTransportEventHandler:
    def __init__(self, session: WebTransportSession,
                 callbacks: Dict[str, Any]) -> None:
        self._session = session
        self._callbacks = callbacks

    def _run_callback(self, callback_name: str,
                      *args: Any, **kwargs: Any) -> None:
        if callback_name not in self._callbacks:
            return
        try:
            self._callbacks[callback_name](*args, **kwargs)
        except Exception as e:
            _logger.warn(str(e))
            traceback.print_exc()

    def connect_received(self, response_headers: List[Tuple[bytes,
                                                            bytes]]) -> None:
        self._session.request_headers.append((b'swag', b'brap'))
        self._run_callback("connect_received", self._session.request_headers,
                           response_headers)

    def session_established(self) -> None:
        self._run_callback("session_established", self._session)

    def stream_data_received(self, stream_id: int, data: bytes,
                             stream_ended: bool) -> None:
        result = parse(data)
        # _logger.info(result)

        # self._session_stream_id = result['streamId']

        if (result['streamId'] == 1 or result['streamId'] == 2):
            #_logger.info("stream id: %s", result['streamId'])        

            for idx, connection in enumerate(connections):
                if connection != self._session:
                    if (self._session.stream_is_unidirectional(stream_id)):
                        pass
                        # _logger.info("Stream is unidirectional cannot send data")
                    else:
                        #_logger.info("bruh moment: %s %s %s", idx, streamIds[len(streamIds)-1-idx], stream_id)
                        #self._session.send_stream_data(stream_id, data, stream_ended)  
                        connection.send_stream_data(stream_id, data, stream_ended)                    
                        self._run_callback("stream_data_received", stream_id, data, stream_ended)

    def datagram_received(self, data: bytes) -> None:
        # array = bytearray(data)
        result = parse(data)
        if (result['streamId'] != 1 and result['streamId'] != 2):
            _logger.info("stream id: %s", result['streamId'])  
        # _logger.info("DATAGRAM RECEIVED")
        # _logger.info("CODE, %s", result)
        for connection in connections:
            if connection != self._session:
                WebTransportSession.send_datagram(connection, data)
        self._run_callback("datagram_received", self._session, data)

    def session_closed(
            self,
            close_info: Optional[Tuple[int, bytes]],
            abruptly: bool) -> None:
        self._run_callback(
            "session_closed", self._session, close_info, abruptly=abruptly)

    def stream_reset(self, stream_id: int, error_code: int) -> None:
        self._run_callback(
            "stream_reset", self._session, stream_id, error_code)


class SessionTicketStore:
    """
    Simple in-memory store for session tickets.
    """
    def __init__(self) -> None:
        self.tickets: Dict[bytes, SessionTicket] = {}

    def add(self, ticket: SessionTicket) -> None:
        self.tickets[ticket.ticket] = ticket

    def pop(self, label: bytes) -> Optional[SessionTicket]:
        return self.tickets.pop(label, None)


class WebTransportH3Server:
    """
    A WebTransport over HTTP/3 for testing.

    :param host: Host from which to serve.
    :param port: Port from which to serve.
    :param doc_root: Document root for serving handlers.
    :param cert_path: Path to certificate file to use.
    :param key_path: Path to key file to use.
    :param logger: a Logger object for this server.
    """
    def __init__(self, host: str, port: int, doc_root: str, cert_path: str,
                 key_path: str, logger: Optional[logging.Logger]) -> None:
        self.host = host
        self.port = port
        self.doc_root = doc_root
        self.cert_path = cert_path
        self.key_path = key_path
        self.started = False
        global _doc_root
        _doc_root = self.doc_root
        global _logger
        if logger is not None:
            _logger = logger

        _logger.info("cert path %s", self.cert_path)

    def start(self) -> None:
        """Start the server."""
        self.server_thread = threading.Thread(
            target=self._start_on_server_thread, daemon=True)
        self.server_thread.start()
        self.started = True

    def _start_on_server_thread(self) -> None:
        configuration = QuicConfiguration(
            alpn_protocols=H3_ALPN,
            is_client=False,
            max_datagram_frame_size=65536,
        )

        _logger.info("Starting WebTransport over HTTP/3 server on %s:%s",
                     self.host, self.port)

        configuration.load_cert_chain(self.cert_path, self.key_path)

        ticket_store = SessionTicketStore()

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(
            serve(
                self.host,
                self.port,
                configuration=configuration,
                create_protocol=WebTransportH3Protocol,
                session_ticket_fetcher=ticket_store.pop,
                session_ticket_handler=ticket_store.add,
            ))
        self.loop.run_forever()

    def stop(self) -> None:
        """Stop the server."""
        if self.started:
            asyncio.run_coroutine_threadsafe(self._stop_on_server_thread(),
                                             self.loop)
            self.server_thread.join()
            _logger.info("Stopped WebTransport over HTTP/3 server on %s:%s",
                         self.host, self.port)
        self.started = False

    async def _stop_on_server_thread(self) -> None:
        self.loop.stop()


def server_is_running(host: str, port: int, timeout: float) -> bool:
    """
    Check the WebTransport over HTTP/3 server is running at the given `host` and
    `port`.
    """
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_connect_server_with_timeout(host, port, timeout))


async def _connect_server_with_timeout(host: str, port: int, timeout: float) -> bool:
    try:
        await asyncio.wait_for(_connect_to_server(host, port), timeout=timeout)
    except asyncio.TimeoutError:
        _logger.warning("Failed to connect WebTransport over HTTP/3 server")
        return False
    return True


async def _connect_to_server(host: str, port: int) -> None:
    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=True,
        verify_mode=ssl.CERT_NONE,
    )

    async with connect(host, port, configuration=configuration) as protocol:
        await protocol.ping()
