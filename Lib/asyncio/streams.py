__all__ = (
    'Stream', 'StreamMode',
    'open_connection', 'start_server',
    'connect', 'connect_read_pipe', 'connect_write_pipe',
    'StreamServer')

import enum
import socket
import sys
import warnings
import weakref

if hasattr(socket, 'AF_UNIX'):
    __all__ += ('open_unix_connection', 'start_unix_server',
                'connect_unix',
                'UnixStreamServer')

from . import abc_streams as abc
from . import constants
from . import coroutines
from . import events
from . import exceptions
from . import format_helpers
from . import protocols
from .log import logger
from . import tasks


_DEFAULT_LIMIT = 2 ** 16  # 64 KiB


class StreamMode(enum.Flag):
    READ = enum.auto()
    WRITE = enum.auto()
    READWRITE = READ | WRITE


def _ensure_can_read(mode):
    if not mode & StreamMode.READ:
        raise RuntimeError("The stream is write-only")


def _ensure_can_write(mode):
    if not mode & StreamMode.WRITE:
        raise RuntimeError("The stream is read-only")


class _ContextManagerHelper:
    __slots__ = ('_awaitable', '_result')

    def __init__(self, awaitable):
        self._awaitable = awaitable
        self._result = None

    def __await__(self):
        return self._awaitable.__await__()

    async def __aenter__(self):
        ret = await self._awaitable
        result = await ret.__aenter__()
        self._result = result
        return result

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._result.__aexit__(exc_type, exc_val, exc_tb)


def connect(host=None, port=None, *,
            limit=_DEFAULT_LIMIT,
            ssl=None, family=0, proto=0,
            flags=0, sock=None, local_addr=None,
            server_hostname=None,
            ssl_handshake_timeout=None,
            happy_eyeballs_delay=None, interleave=None):
    """Connect to TCP socket on *host* : *port* address to send and receive data.

    *limit* determines the buffer size limit used by the returned `Stream`
    instance. By default the *limit* is set to 64 KiB.

    The rest of the arguments are passed directly to `loop.create_connection()`.
    """
    # Design note:
    # Don't use decorator approach but explicit non-async
    # function to fail fast and explicitly
    # if passed arguments don't match the function signature
    return _ContextManagerHelper(_connect(host, port, limit,
                                          ssl, family, proto,
                                          flags, sock, local_addr,
                                          server_hostname,
                                          ssl_handshake_timeout,
                                          happy_eyeballs_delay,
                                          interleave))


async def _connect(host, port,
                  limit,
                  ssl, family, proto,
                  flags, sock, local_addr,
                  server_hostname,
                  ssl_handshake_timeout,
                  happy_eyeballs_delay, interleave):
    loop = events.get_running_loop()
    stream = LegacyStream(mode=StreamMode.READWRITE,
                    limit=limit,
                    loop=loop,
                    _asyncio_internal=True)
    await loop.create_connection(
        lambda: _StreamProtocol(stream, loop=loop,
                                _asyncio_internal=True),
        host, port,
        ssl=ssl, family=family, proto=proto,
        flags=flags, sock=sock, local_addr=local_addr,
        server_hostname=server_hostname,
        ssl_handshake_timeout=ssl_handshake_timeout,
        happy_eyeballs_delay=happy_eyeballs_delay, interleave=interleave)
    return stream


def connect_read_pipe(pipe, *, limit=_DEFAULT_LIMIT):
    """Establish a connection to a file-like object *pipe* to receive data.

    Takes a file-like object *pipe* to return a Stream object of the mode
    StreamMode.READ that has similar API of StreamReader. It can also be used
    as an async context manager.
    """

    # Design note:
    # Don't use decorator approach but explicit non-async
    # function to fail fast and explicitly
    # if passed arguments don't match the function signature
    return _ContextManagerHelper(_connect_read_pipe(pipe, limit))


async def _connect_read_pipe(pipe, limit):
    loop = events.get_running_loop()
    stream = LegacyStream(mode=StreamMode.READ,
                    limit=limit,
                    loop=loop,
                    _asyncio_internal=True)
    await loop.connect_read_pipe(
        lambda: _StreamProtocol(stream, loop=loop,
                                _asyncio_internal=True),
        pipe)
    return stream


def connect_write_pipe(pipe, *, limit=_DEFAULT_LIMIT):
    """Establish a connection to a file-like object *pipe* to send data.

    Takes a file-like object *pipe* to return a Stream object of the mode
    StreamMode.WRITE that has similar API of StreamWriter. It can also be used
    as an async context manager.
    """

    # Design note:
    # Don't use decorator approach but explicit non-async
    # function to fail fast and explicitly
    # if passed arguments don't match the function signature
    return _ContextManagerHelper(_connect_write_pipe(pipe, limit))


async def _connect_write_pipe(pipe, limit):
    loop = events.get_running_loop()
    stream = LegacyStream(mode=StreamMode.WRITE,
                    limit=limit,
                    loop=loop,
                    _asyncio_internal=True)
    await loop.connect_write_pipe(
        lambda: _StreamProtocol(stream, loop=loop,
                                _asyncio_internal=True),
        pipe)
    return stream


async def open_connection(host=None, port=None, *,
                          loop=None, limit=_DEFAULT_LIMIT, **kwds):
    """A wrapper for create_connection() returning a (reader, writer) pair.

    The reader returned is a StreamReader instance; the writer is a
    StreamWriter instance.

    The arguments are all the usual arguments to create_connection()
    except protocol_factory; most common are positional host and port,
    with various optional keyword arguments following.

    Additional optional keyword arguments are loop (to set the event loop
    instance to use) and limit (to set the buffer limit passed to the
    StreamReader).

    (If you want to customize the StreamReader and/or
    StreamReaderProtocol classes, just copy the code -- there's
    really nothing special here except some convenience.)
    """
    warnings.warn("open_connection() is deprecated since Python 3.8 "
                  "in favor of connect(), and scheduled for removal "
                  "in Python 3.10",
                  DeprecationWarning,
                  stacklevel=2)
    if loop is None:
        loop = events.get_event_loop()
    else:
        warnings.warn("The loop argument is deprecated since Python 3.8, "
                      "and scheduled for removal in Python 3.10.",
                      DeprecationWarning, stacklevel=2)
    reader = StreamReader(limit=limit, loop=loop)
    protocol = StreamReaderProtocol(reader, loop=loop, _asyncio_internal=True)
    transport, _ = await loop.create_connection(
        lambda: protocol, host, port, **kwds)
    writer = StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def start_server(client_connected_cb, host=None, port=None, *,
                       loop=None, limit=_DEFAULT_LIMIT, **kwds):
    """Start a socket server, call back for each client connected.

    The first parameter, `client_connected_cb`, takes two parameters:
    client_reader, client_writer.  client_reader is a StreamReader
    object, while client_writer is a StreamWriter object.  This
    parameter can either be a plain callback function or a coroutine;
    if it is a coroutine, it will be automatically converted into a
    Task.

    The rest of the arguments are all the usual arguments to
    loop.create_server() except protocol_factory; most common are
    positional host and port, with various optional keyword arguments
    following.  The return value is the same as loop.create_server().

    Additional optional keyword arguments are loop (to set the event loop
    instance to use) and limit (to set the buffer limit passed to the
    StreamReader).

    The return value is the same as loop.create_server(), i.e. a
    Server object which can be used to stop the service.
    """
    warnings.warn("start_server() is deprecated since Python 3.8 "
                  "in favor of StreamServer(), and scheduled for removal "
                  "in Python 3.10",
                  DeprecationWarning,
                  stacklevel=2)
    if loop is None:
        loop = events.get_event_loop()
    else:
        warnings.warn("The loop argument is deprecated since Python 3.8, "
                      "and scheduled for removal in Python 3.10.",
                      DeprecationWarning, stacklevel=2)

    def factory():
        reader = StreamReader(limit=limit, loop=loop)
        protocol = StreamReaderProtocol(reader, client_connected_cb,
                                        loop=loop,
                                        _asyncio_internal=True)
        return protocol

    return await loop.create_server(factory, host, port, **kwds)


class _BaseStreamServer:
    # Design notes.
    # StreamServer and UnixStreamServer are exposed as FINAL classes,
    # not function factories.
    # async with serve(host, port) as server:
    #      server.start_serving()
    # looks ugly.
    # The class doesn't provide API for enumerating connected streams
    # It can be a subject for improvements in Python 3.9

    _server_impl = None

    def __init__(self, client_connected_cb,
                 /,
                 limit=_DEFAULT_LIMIT,
                 shutdown_timeout=60,
                 _asyncio_internal=False):
        if not _asyncio_internal:
            raise RuntimeError("_ServerStream is a private asyncio class")
        self._client_connected_cb = client_connected_cb
        self._limit = limit
        self._loop = events.get_running_loop()
        self._streams = {}
        self._shutdown_timeout = shutdown_timeout

    def __init_subclass__(cls):
        if not cls.__module__.startswith('asyncio.'):
            raise TypeError(f"asyncio.{cls.__name__} "
                            "class cannot be inherited from")

    async def bind(self):
        if self._server_impl is not None:
            return
        self._server_impl = await self._bind()

    def is_bound(self):
        return self._server_impl is not None

    @property
    def sockets(self):
        # multiple value for socket bound to both IPv4 and IPv6 families
        if self._server_impl is None:
            return ()
        return self._server_impl.sockets

    def is_serving(self):
        if self._server_impl is None:
            return False
        return self._server_impl.is_serving()

    async def start_serving(self):
        await self.bind()
        await self._server_impl.start_serving()

    async def serve_forever(self):
        await self.start_serving()
        await self._server_impl.serve_forever()

    async def close(self):
        if self._server_impl is None:
            return
        self._server_impl.close()
        streams = list(self._streams.keys())
        active_tasks = list(self._streams.values())
        if streams:
            await tasks.wait([stream.close() for stream in streams])
        await self._server_impl.wait_closed()
        self._server_impl = None
        await self._shutdown_active_tasks(active_tasks)

    async def abort(self):
        if self._server_impl is None:
            return
        self._server_impl.close()
        streams = list(self._streams.keys())
        active_tasks = list(self._streams.values())
        if streams:
            await tasks.wait([stream.abort() for stream in streams])
        await self._server_impl.wait_closed()
        self._server_impl = None
        await self._shutdown_active_tasks(active_tasks)

    async def __aenter__(self):
        await self.bind()
        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        await self.close()

    def _attach(self, stream, task):
        self._streams[stream] = task

    def _detach(self, stream, task):
        del self._streams[stream]

    async def _shutdown_active_tasks(self, active_tasks):
        if not active_tasks:
            return
        # NOTE: tasks finished with exception are reported
        # by the Task.__del__() method.
        done, pending = await tasks.wait(active_tasks,
                                         timeout=self._shutdown_timeout)
        if not pending:
            return
        for task in pending:
            task.cancel()
        done, pending = await tasks.wait(pending,
                                         timeout=self._shutdown_timeout)
        for task in pending:
            self._loop.call_exception_handler({
                "message": (f'{task!r} ignored cancellation request '
                            f'from a closing {self!r}'),
                "stream_server": self
            })

    def __repr__(self):
        ret = [f'{self.__class__.__name__}']
        if self.is_serving():
            ret.append('serving')
        if self.sockets:
            ret.append(f'sockets={self.sockets!r}')
        return '<' + ' '.join(ret) + '>'

    def __del__(self, _warn=warnings.warn):
        if self._server_impl is not None:
            _warn(f"unclosed stream server {self!r}",
                  ResourceWarning, source=self)
            self._server_impl.close()


class StreamServer(_BaseStreamServer):

    def __init__(self, client_connected_cb, /, host=None, port=None, *,
                 limit=_DEFAULT_LIMIT,
                 family=socket.AF_UNSPEC,
                 flags=socket.AI_PASSIVE, sock=None, backlog=100,
                 ssl=None, reuse_address=None, reuse_port=None,
                 ssl_handshake_timeout=None,
                 shutdown_timeout=60):
        super().__init__(client_connected_cb,
                         limit=limit,
                         shutdown_timeout=shutdown_timeout,
                         _asyncio_internal=True)
        self._host = host
        self._port = port
        self._family = family
        self._flags = flags
        self._sock = sock
        self._backlog = backlog
        self._ssl = ssl
        self._reuse_address = reuse_address
        self._reuse_port = reuse_port
        self._ssl_handshake_timeout = ssl_handshake_timeout

    async def _bind(self):
        def factory():
            protocol = _ServerStreamProtocol(self,
                                             self._limit,
                                             self._client_connected_cb,
                                             loop=self._loop,
                                             _asyncio_internal=True)
            return protocol
        return await self._loop.create_server(
            factory,
            self._host,
            self._port,
            start_serving=False,
            family=self._family,
            flags=self._flags,
            sock=self._sock,
            backlog=self._backlog,
            ssl=self._ssl,
            reuse_address=self._reuse_address,
            reuse_port=self._reuse_port,
            ssl_handshake_timeout=self._ssl_handshake_timeout)


if hasattr(socket, 'AF_UNIX'):
    # UNIX Domain Sockets are supported on this platform

    async def open_unix_connection(path=None, *,
                                   loop=None, limit=_DEFAULT_LIMIT, **kwds):
        """Similar to `open_connection` but works with UNIX Domain Sockets."""
        warnings.warn("open_unix_connection() is deprecated since Python 3.8 "
                      "in favor of connect_unix(), and scheduled for removal "
                      "in Python 3.10",
                      DeprecationWarning,
                      stacklevel=2)
        if loop is None:
            loop = events.get_event_loop()
        else:
            warnings.warn("The loop argument is deprecated since Python 3.8, "
                          "and scheduled for removal in Python 3.10.",
                          DeprecationWarning, stacklevel=2)
        reader = StreamReader(limit=limit, loop=loop)
        protocol = StreamReaderProtocol(reader, loop=loop,
                                        _asyncio_internal=True)
        transport, _ = await loop.create_unix_connection(
            lambda: protocol, path, **kwds)
        writer = StreamWriter(transport, protocol, reader, loop)
        return reader, writer


    def connect_unix(path=None, *,
                     limit=_DEFAULT_LIMIT,
                     ssl=None, sock=None,
                     server_hostname=None,
                     ssl_handshake_timeout=None):
        """Similar to `connect()` but works with UNIX Domain Sockets."""
        # Design note:
        # Don't use decorator approach but explicit non-async
        # function to fail fast and explicitly
        # if passed arguments don't match the function signature
        return _ContextManagerHelper(_connect_unix(path,
                                                   limit,
                                                   ssl, sock,
                                                   server_hostname,
                                                   ssl_handshake_timeout))


    async def _connect_unix(path,
                           limit,
                           ssl, sock,
                           server_hostname,
                           ssl_handshake_timeout):
        """Similar to `connect()` but works with UNIX Domain Sockets."""
        loop = events.get_running_loop()
        stream = LegacyStream(mode=StreamMode.READWRITE,
                        limit=limit,
                        loop=loop,
                        _asyncio_internal=True)
        await loop.create_unix_connection(
            lambda: _StreamProtocol(stream,
                                    loop=loop,
                                    _asyncio_internal=True),
            path,
            ssl=ssl,
            sock=sock,
            server_hostname=server_hostname,
            ssl_handshake_timeout=ssl_handshake_timeout)
        return stream


    async def start_unix_server(client_connected_cb, path=None, *,
                                loop=None, limit=_DEFAULT_LIMIT, **kwds):
        """Similar to `start_server` but works with UNIX Domain Sockets."""
        warnings.warn("start_unix_server() is deprecated since Python 3.8 "
                      "in favor of UnixStreamServer(), and scheduled "
                      "for removal in Python 3.10",
                      DeprecationWarning,
                      stacklevel=2)
        if loop is None:
            loop = events.get_event_loop()
        else:
            warnings.warn("The loop argument is deprecated since Python 3.8, "
                          "and scheduled for removal in Python 3.10.",
                          DeprecationWarning, stacklevel=2)

        def factory():
            reader = StreamReader(limit=limit, loop=loop)
            protocol = StreamReaderProtocol(reader, client_connected_cb,
                                            loop=loop,
                                            _asyncio_internal=True)
            return protocol

        return await loop.create_unix_server(factory, path, **kwds)

    class UnixStreamServer(_BaseStreamServer):

        def __init__(self, client_connected_cb, /, path=None, *,
                     limit=_DEFAULT_LIMIT,
                     sock=None,
                     backlog=100,
                     ssl=None,
                     ssl_handshake_timeout=None,
                     shutdown_timeout=60):
            super().__init__(client_connected_cb,
                             limit=limit,
                             shutdown_timeout=shutdown_timeout,
                             _asyncio_internal=True)
            self._path = path
            self._sock = sock
            self._backlog = backlog
            self._ssl = ssl
            self._ssl_handshake_timeout = ssl_handshake_timeout

        async def _bind(self):
            def factory():
                protocol = _ServerStreamProtocol(self,
                                                 self._limit,
                                                 self._client_connected_cb,
                                                 loop=self._loop,
                                                 _asyncio_internal=True)
                return protocol
            return await self._loop.create_unix_server(
                factory,
                self._path,
                start_serving=False,
                sock=self._sock,
                backlog=self._backlog,
                ssl=self._ssl,
                ssl_handshake_timeout=self._ssl_handshake_timeout)


class FlowControlMixin(protocols.Protocol):
    """Reusable flow control logic for StreamWriter.drain().

    This implements the protocol methods pause_writing(),
    resume_writing() and connection_lost().  If the subclass overrides
    these it must call the super methods.

    StreamWriter.drain() must wait for _drain_helper() coroutine.
    """

    def __init__(self, loop=None, *, _asyncio_internal=False):
        if loop is None:
            self._loop = events.get_event_loop()
        else:
            self._loop = loop
        if not _asyncio_internal:
            # NOTE:
            # Avoid inheritance from FlowControlMixin
            # Copy-paste the code to your project
            # if you need flow control helpers
            warnings.warn(f"{self.__class__} should be instaniated "
                          "by asyncio internals only, "
                          "please avoid its creation from user code",
                          DeprecationWarning)
        self._paused = False
        self._drain_waiter = None
        self._connection_lost = False

    def pause_writing(self):
        assert not self._paused
        self._paused = True
        if self._loop.get_debug():
            logger.debug("%r pauses writing", self)

    def resume_writing(self):
        assert self._paused
        self._paused = False
        if self._loop.get_debug():
            logger.debug("%r resumes writing", self)

        waiter = self._drain_waiter
        if waiter is not None:
            self._drain_waiter = None
            if not waiter.done():
                waiter.set_result(None)

    def connection_lost(self, exc):
        self._connection_lost = True
        # Wake up the writer if currently paused.
        if not self._paused:
            return
        waiter = self._drain_waiter
        if waiter is None:
            return
        self._drain_waiter = None
        if waiter.done():
            return
        if exc is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(exc)

    async def _drain_helper(self):
        if self._connection_lost:
            raise ConnectionResetError('Connection lost')
        if not self._paused:
            return
        waiter = self._drain_waiter
        assert waiter is None or waiter.cancelled()
        waiter = self._loop.create_future()
        self._drain_waiter = waiter
        await waiter

    def _get_close_waiter(self, stream):
        raise NotImplementedError


# begin legacy stream APIs

class StreamReaderProtocol(FlowControlMixin, protocols.Protocol):
    """Helper class to adapt between Protocol and StreamReader.

    (This is a helper class instead of making StreamReader itself a
    Protocol subclass, because the StreamReader has other potential
    uses, and to prevent the user of the StreamReader to accidentally
    call inappropriate methods of the protocol.)
    """

    def __init__(self, stream_reader, client_connected_cb=None, loop=None,
                 *, _asyncio_internal=False):
        super().__init__(loop=loop, _asyncio_internal=_asyncio_internal)
        self._stream_reader = stream_reader
        self._stream_writer = None
        self._client_connected_cb = client_connected_cb
        self._over_ssl = False
        self._closed = self._loop.create_future()

    def connection_made(self, transport):
        self._stream_reader.set_transport(transport)
        self._over_ssl = transport.get_extra_info('sslcontext') is not None
        if self._client_connected_cb is not None:
            self._stream_writer = StreamWriter(transport, self,
                                               self._stream_reader,
                                               self._loop)
            res = self._client_connected_cb(self._stream_reader,
                                            self._stream_writer)
            if coroutines.iscoroutine(res):
                self._loop.create_task(res)

    def connection_lost(self, exc):
        if self._stream_reader is not None:
            if exc is None:
                self._stream_reader.feed_eof()
            else:
                self._stream_reader.set_exception(exc)
        if not self._closed.done():
            if exc is None:
                self._closed.set_result(None)
            else:
                self._closed.set_exception(exc)
        super().connection_lost(exc)
        self._stream_reader = None
        self._stream_writer = None

    def data_received(self, data):
        self._stream_reader.feed_data(data)

    def eof_received(self):
        self._stream_reader.feed_eof()
        if self._over_ssl:
            # Prevent a warning in SSLProtocol.eof_received:
            # "returning true from eof_received()
            # has no effect when using ssl"
            return False
        return True

    def __del__(self):
        # Prevent reports about unhandled exceptions.
        # Better than self._closed._log_traceback = False hack
        closed = self._closed
        if closed.done() and not closed.cancelled():
            closed.exception()


class StreamWriter:
    """Wraps a Transport.

    This exposes write(), writelines(), [can_]write_eof(),
    get_extra_info() and close().  It adds drain() which returns an
    optional Future on which you can wait for flow control.  It also
    adds a transport property which references the Transport
    directly.
    """

    def __init__(self, transport, protocol, reader, loop):
        self._transport = transport
        self._protocol = protocol
        # drain() expects that the reader has an exception() method
        assert reader is None or isinstance(reader, StreamReader)
        self._reader = reader
        self._loop = loop

    def __repr__(self):
        info = [self.__class__.__name__, f'transport={self._transport!r}']
        if self._reader is not None:
            info.append(f'reader={self._reader!r}')
        return '<{}>'.format(' '.join(info))

    @property
    def transport(self):
        return self._transport

    def write(self, data):
        self._transport.write(data)

    def writelines(self, data):
        self._transport.writelines(data)

    def write_eof(self):
        return self._transport.write_eof()

    def can_write_eof(self):
        return self._transport.can_write_eof()

    def close(self):
        return self._transport.close()

    def is_closing(self):
        return self._transport.is_closing()

    async def wait_closed(self):
        await self._protocol._closed

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    async def drain(self):
        """Flush the write buffer.

        The intended use is to write

          w.write(data)
          await w.drain()
        """
        if self._reader is not None:
            exc = self._reader.exception()
            if exc is not None:
                raise exc
        if self._transport.is_closing():
            # Yield to the event loop so connection_lost() may be
            # called.  Without this, _drain_helper() would return
            # immediately, and code that calls
            #     write(...); await drain()
            # in a loop would never call connection_lost(), so it
            # would not see an error when the socket is closed.
            await tasks.sleep(0, loop=self._loop)
        await self._protocol._drain_helper()


class StreamReader:

    def __init__(self, limit=_DEFAULT_LIMIT, loop=None):
        # The line length limit is  a security feature;
        # it also doubles as half the buffer limit.

        if limit <= 0:
            raise ValueError('Limit cannot be <= 0')

        self._limit = limit
        if loop is None:
            self._loop = events.get_event_loop()
        else:
            self._loop = loop
        self._buffer = bytearray()
        self._eof = False    # Whether we're done.
        self._waiter = None  # A future used by _wait_for_data()
        self._exception = None
        self._transport = None
        self._paused = False

    def __repr__(self):
        info = ['StreamReader']
        if self._buffer:
            info.append(f'{len(self._buffer)} bytes')
        if self._eof:
            info.append('eof')
        if self._limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._limit}')
        if self._waiter:
            info.append(f'waiter={self._waiter!r}')
        if self._exception:
            info.append(f'exception={self._exception!r}')
        if self._transport:
            info.append(f'transport={self._transport!r}')
        if self._paused:
            info.append('paused')
        return '<{}>'.format(' '.join(info))

    def exception(self):
        return self._exception

    def set_exception(self, exc):
        self._exception = exc

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(exc)

    def _wakeup_waiter(self):
        """Wakeup read*() functions waiting for data or EOF."""
        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_result(None)

    def set_transport(self, transport):
        assert self._transport is None, 'Transport already set'
        self._transport = transport

    def _maybe_resume_transport(self):
        if self._paused and len(self._buffer) <= self._limit:
            self._paused = False
            self._transport.resume_reading()

    def feed_eof(self):
        self._eof = True
        self._wakeup_waiter()

    def at_eof(self):
        """Return True if the buffer is empty and 'feed_eof' was called."""
        return self._eof and not self._buffer

    def feed_data(self, data):
        assert not self._eof, 'feed_data after feed_eof'

        if not data:
            return

        self._buffer.extend(data)
        self._wakeup_waiter()

        if (self._transport is not None and
                not self._paused and
                len(self._buffer) > 2 * self._limit):
            try:
                self._transport.pause_reading()
            except NotImplementedError:
                # The transport can't be paused.
                # We'll just have to buffer all data.
                # Forget the transport so we don't keep trying.
                self._transport = None
            else:
                self._paused = True

    async def _wait_for_data(self, func_name):
        """Wait until feed_data() or feed_eof() is called.

        If stream was paused, automatically resume it.
        """
        # StreamReader uses a future to link the protocol feed_data() method
        # to a read coroutine. Running two read coroutines at the same time
        # would have an unexpected behaviour. It would not possible to know
        # which coroutine would get the next data.
        if self._waiter is not None:
            raise RuntimeError(
                f'{func_name}() called while another coroutine is '
                f'already waiting for incoming data')

        assert not self._eof, '_wait_for_data after EOF'

        # Waiting for data while paused will make deadlock, so prevent it.
        # This is essential for readexactly(n) for case when n > self._limit.
        if self._paused:
            self._paused = False
            self._transport.resume_reading()

        self._waiter = self._loop.create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None

    async def readline(self):
        """Read chunk of data from the stream until newline (b'\n') is found.

        On success, return chunk that ends with newline. If only partial
        line can be read due to EOF, return incomplete line without
        terminating newline. When EOF was reached while no bytes read, empty
        bytes object is returned.

        If limit is reached, ValueError will be raised. In that case, if
        newline was found, complete line including newline will be removed
        from internal buffer. Else, internal buffer will be cleared. Limit is
        compared against part of the line without newline.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        sep = b'\n'
        seplen = len(sep)
        try:
            line = await self.readuntil(sep)
        except exceptions.IncompleteReadError as e:
            return e.partial
        except exceptions.LimitOverrunError as e:
            if self._buffer.startswith(sep, e.consumed):
                del self._buffer[:e.consumed + seplen]
            else:
                self._buffer.clear()
            self._maybe_resume_transport()
            raise ValueError(e.args[0])
        return line

    async def readuntil(self, separator=b'\n'):
        """Read data from the stream until ``separator`` is found.

        On success, the data and separator will be removed from the
        internal buffer (consumed). Returned data will include the
        separator at the end.

        Configured stream limit is used to check result. Limit sets the
        maximal length of data that can be returned, not counting the
        separator.

        If an EOF occurs and the complete separator is still not found,
        an IncompleteReadError exception will be raised, and the internal
        buffer will be reset.  The IncompleteReadError.partial attribute
        may contain the separator partially.

        If the data cannot be read because of over limit, a
        LimitOverrunError exception  will be raised, and the data
        will be left in the internal buffer, so it can be read again.
        """
        seplen = len(separator)
        if seplen == 0:
            raise ValueError('Separator should be at least one-byte string')

        if self._exception is not None:
            raise self._exception

        # Consume whole buffer except last bytes, which length is
        # one less than seplen. Let's check corner cases with
        # separator='SEPARATOR':
        # * we have received almost complete separator (without last
        #   byte). i.e buffer='some textSEPARATO'. In this case we
        #   can safely consume len(separator) - 1 bytes.
        # * last byte of buffer is first byte of separator, i.e.
        #   buffer='abcdefghijklmnopqrS'. We may safely consume
        #   everything except that last byte, but this require to
        #   analyze bytes of buffer that match partial separator.
        #   This is slow and/or require FSM. For this case our
        #   implementation is not optimal, since require rescanning
        #   of data that is known to not belong to separator. In
        #   real world, separator will not be so long to notice
        #   performance problems. Even when reading MIME-encoded
        #   messages :)

        # `offset` is the number of bytes from the beginning of the buffer
        # where there is no occurrence of `separator`.
        offset = 0

        # Loop until we find `separator` in the buffer, exceed the buffer size,
        # or an EOF has happened.
        while True:
            buflen = len(self._buffer)

            # Check if we now have enough data in the buffer for `separator` to
            # fit.
            if buflen - offset >= seplen:
                isep = self._buffer.find(separator, offset)

                if isep != -1:
                    # `separator` is in the buffer. `isep` will be used later
                    # to retrieve the data.
                    break

                # see upper comment for explanation.
                offset = buflen + 1 - seplen
                if offset > self._limit:
                    raise exceptions.LimitOverrunError(
                        'Separator is not found, and chunk exceed the limit',
                        offset)

            # Complete message (with full separator) may be present in buffer
            # even when EOF flag is set. This may happen when the last chunk
            # adds data which makes separator be found. That's why we check for
            # EOF *ater* inspecting the buffer.
            if self._eof:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                raise exceptions.IncompleteReadError(chunk, None)

            # _wait_for_data() will resume reading if stream was paused.
            await self._wait_for_data('readuntil')

        if isep > self._limit:
            raise exceptions.LimitOverrunError(
                'Separator is found, but chunk is longer than limit', isep)

        chunk = self._buffer[:isep + seplen]
        del self._buffer[:isep + seplen]
        self._maybe_resume_transport()
        return bytes(chunk)

    async def read(self, n=-1):
        """Read up to `n` bytes from the stream.

        If n is not provided, or set to -1, read until EOF and return all read
        bytes. If the EOF was received and the internal buffer is empty, return
        an empty bytes object.

        If n is zero, return empty bytes object immediately.

        If n is positive, this function try to read `n` bytes, and may return
        less or equal bytes than requested, but at least one byte. If EOF was
        received before any byte is read, this function returns empty byte
        object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """

        if self._exception is not None:
            raise self._exception

        if n == 0:
            return b''

        if n < 0:
            # This used to just loop creating a new waiter hoping to
            # collect everything in self._buffer, but that would
            # deadlock if the subprocess sends more than self.limit
            # bytes.  So just call self.read(self._limit) until EOF.
            blocks = []
            while True:
                block = await self.read(self._limit)
                if not block:
                    break
                blocks.append(block)
            return b''.join(blocks)

        if not self._buffer and not self._eof:
            await self._wait_for_data('read')

        # This will work right even if buffer is less than n bytes
        data = bytes(self._buffer[:n])
        del self._buffer[:n]

        self._maybe_resume_transport()
        return data

    async def readexactly(self, n):
        """Read exactly `n` bytes.

        Raise an IncompleteReadError if EOF is reached before `n` bytes can be
        read. The IncompleteReadError.partial attribute of the exception will
        contain the partial read bytes.

        if n is zero, return empty bytes object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        if n < 0:
            raise ValueError('readexactly size can not be less than zero')

        if self._exception is not None:
            raise self._exception

        if n == 0:
            return b''

        while len(self._buffer) < n:
            if self._eof:
                incomplete = bytes(self._buffer)
                self._buffer.clear()
                raise exceptions.IncompleteReadError(incomplete, n)

            await self._wait_for_data('readexactly')

        if len(self._buffer) == n:
            data = bytes(self._buffer)
            self._buffer.clear()
        else:
            data = bytes(self._buffer[:n])
            del self._buffer[:n]
        self._maybe_resume_transport()
        return data

    def __aiter__(self):
        return self

    async def __anext__(self):
        val = await self.readline()
        if val == b'':
            raise StopAsyncIteration
        return val


# end legacy stream APIs


class _BaseStreamProtocol(FlowControlMixin, protocols.Protocol):
    """Helper class to adapt between Protocol and StreamReader.

    (This is a helper class instead of making StreamReader itself a
    Protocol subclass, because the StreamReader has other potential
    uses, and to prevent the user of the StreamReader to accidentally
    call inappropriate methods of the protocol.)
    """

    _stream = None  # initialized in derived classes

    def __init__(self, loop=None,
                 *, _asyncio_internal=False):
        super().__init__(loop=loop, _asyncio_internal=_asyncio_internal)
        self._transport = None
        self._over_ssl = False
        self._closed = self._loop.create_future()

    def connection_made(self, transport):
        self._transport = transport
        self._over_ssl = transport.get_extra_info('sslcontext') is not None

    def connection_lost(self, exc):
        stream = self._stream
        if stream is not None:
            if exc is None:
                stream._feed_eof()
            else:
                stream._set_exception(exc)
        if not self._closed.done():
            if exc is None:
                self._closed.set_result(None)
            else:
                self._closed.set_exception(exc)
        super().connection_lost(exc)
        self._transport = None

    def data_received(self, data):
        stream = self._stream
        if stream is not None:
            stream._feed_data(data)

    def eof_received(self):
        stream = self._stream
        if stream is not None:
            stream._feed_eof()
        if self._over_ssl:
            # Prevent a warning in SSLProtocol.eof_received:
            # "returning true from eof_received()
            # has no effect when using ssl"
            return False
        return True

    def _get_close_waiter(self, stream):
        return self._closed

    def __del__(self):
        # Prevent reports about unhandled exceptions.
        # Better than self._closed._log_traceback = False hack
        closed = self._get_close_waiter(self._stream)
        if closed.done() and not closed.cancelled():
            closed.exception()


class _StreamProtocol(_BaseStreamProtocol):
    _source_traceback = None

    def __init__(self, stream, loop=None,
                 *, _asyncio_internal=False):
        super().__init__(loop=loop, _asyncio_internal=_asyncio_internal)
        self._source_traceback = stream._source_traceback
        self._stream_wr = weakref.ref(stream, self._on_gc)
        self._reject_connection = False

    def _on_gc(self, wr):
        transport = self._transport
        if transport is not None:
            # connection_made was called
            context = {
                'message': ('An open stream object is being garbage '
                            'collected; call "await stream.close()" explicitly.')
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)
            transport.abort()
        else:
            self._reject_connection = True
        self._stream_wr = None

    @property
    def _stream(self):
        if self._stream_wr is None:
            return None
        return self._stream_wr()

    def connection_made(self, transport):
        if self._reject_connection:
            context = {
                'message': ('An open stream was garbage collected prior to '
                            'establishing network connection; '
                            'call "await stream.close()" explicitly.')
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)
            transport.abort()
            return
        super().connection_made(transport)
        stream = self._stream
        if stream is None:
            return
        stream._set_transport(transport)
        stream._set_protocol(self)

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self._stream_wr = None


class _ServerStreamProtocol(_BaseStreamProtocol):
    def __init__(self, server, limit, client_connected_cb, loop=None,
                 *, _asyncio_internal=False):
        super().__init__(loop=loop, _asyncio_internal=_asyncio_internal)
        assert self._closed
        self._client_connected_cb = client_connected_cb
        self._limit = limit
        self._server = server
        self._task = None

    def connection_made(self, transport):
        super().connection_made(transport)
        stream = LegacyStream(mode=StreamMode.READWRITE,
                        transport=transport,
                        protocol=self,
                        limit=self._limit,
                        loop=self._loop,
                        is_server_side=True,
                        _asyncio_internal=True)
        self._stream = stream
        # If self._client_connected_cb(self._stream) fails
        # the exception is logged by transport
        self._task = self._loop.create_task(
            self._client_connected_cb(self._stream))
        self._server._attach(stream, self._task)

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self._server._detach(self._stream, self._task)
        self._stream = None


class _OptionalAwait:
    # The class doesn't create a coroutine
    # if not awaited
    # It prevents "coroutine is never awaited" message

    __slots___ = ('_method',)

    def __init__(self, method):
        self._method = method

    def __await__(self):
        return self._method().__await__()

from .sslstream import SSLStream as _Trio_SSLStream

class _SSLStream(_Trio_SSLStream):
    _handshake_timeout = None
    def __init__(self, *a, ssl_handshake_timeout=None, **kw):
        self._handshake_timeout = ssl_handshake_timeout
        _Trio_SSLStream.__init__(self, *a, **kw)

    # TODO add do_handshake that obeys this timeout


class TransportStream(abc.HalfCloseableStream):
    """Wraps a Transport in a Stream.
    """

    _source_traceback = None

    def __init__(self, mode, *,
                 transport=None,
                 protocol=None,
                 loop=None,
                 limit=_DEFAULT_LIMIT,
                 _asyncio_internal=False):
        if not _asyncio_internal:
            raise RuntimeError(f"{self.__class__} should be instantiated "
                               "by asyncio internals only")
        self._mode = mode
        self._transport = transport
        self._protocol = protocol

        if limit <= 0:
            raise ValueError('Limit cannot be <= 0')
        self._limit = limit

        if loop is None:
            loop = events.get_event_loop()
        else:
            warnings.warn("The loop argument is deprecated since Python 3.8, "
                          "and scheduled for removal in Python 3.10.",
                          DeprecationWarning, stacklevel=3)
        self._loop = loop

        self._buffer = bytearray()
        self._eof = False    # Whether we're done.
        self._waiter = None  # A future used by _wait_for_data()
        self._exception = None
        self._paused = False
        self._complete_fut = self._loop.create_future()
        self._complete_fut.set_result(None)

    def __repr__(self):
        info = [self.__class__.__name__]
        info.append(f'mode={self._mode}')
        if self._buffer:
            info.append(f'{len(self._buffer)} bytes')
        if self._eof:
            info.append('eof')
        if self._limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._limit}')
        if self._waiter:
            info.append(f'waiter={self._waiter!r}')
        if self._exception:
            info.append(f'exception={self._exception!r}')
        if self._transport:
            info.append(f'transport={self._transport!r}')
        if self._paused:
            info.append('paused')
        return '<{}>'.format(' '.join(info))

    @property
    def mode(self):
        return self._mode

    @property
    def transport(self):
        warnings.warn("Stream.transport attribute is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        return self._transport

    async def write(self, data):
        _ensure_can_write(self._mode)
        self._transport.write(data)
        await self._fast_drain()

    def _fast_drain(self):
        # The helper tries to use fast-path to return already existing
        # complete future object if underlying transport is not paused
        # and actual waiting for writing resume is not needed
        exc = self._exception
        if exc is not None:
            fut = self._loop.create_future()
            fut.set_exception(exc)
            return fut
        if not self._transport.is_closing():
            if self._protocol._connection_lost:
                fut = self._loop.create_future()
                fut.set_exception(ConnectionResetError('Connection lost'))
                return fut
            if not self._protocol._paused:
                # fast path, the stream is not paused
                # no need to wait for resume signal
                return self._complete_fut
        return self.drain()

    async def drain(self):
        """Flush the write buffer.

        The intended use was to write

          w.write(data)
          await w.drain()
        
        except that now "write" is async-only, thus this is
        """
        _ensure_can_write(self._mode)
        exc = self._exception
        if exc is not None:
            raise exc
        if self._transport.is_closing():
            # Wait for protocol.connection_lost() call
            # Raise connection closing error if any,
            # ConnectionResetError otherwise
            await tasks.sleep(0)
        await self._protocol._drain_helper()

    def write_eof(self):
        _ensure_can_write(self._mode)
        return self._transport.write_eof()

    def can_write_eof(self):
        if not self._mode.is_write():
            return False
        return self._transport.can_write_eof()

    async def close(self):
        self._transport.close()
        return await self.wait_closed()

    def _sync_close(self):
        self._transport.close()

    async def wait_closed(self):
        await self._protocol._get_close_waiter(self)

    def is_closing(self):
        return self._transport.is_closing()

    async def abort(self):
        self._transport.abort()
        await self.wait_closed()

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    def exception(self):
        return self._exception

    def _set_exception(self, exc):
        self._exception = exc

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(exc)

    def _wakeup_waiter(self):
        """Wakeup read*() functions waiting for data or EOF."""
        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_result(None)

    def _set_transport(self, transport):
        if transport is self._transport:
            return
        assert self._transport is None, 'Transport already set'
        self._transport = transport

    def _set_protocol(self, protocol):
        if protocol is self._protocol:
            return
        assert self._protocol is None, 'Protocol already set'
        self._protocol = protocol

    def _maybe_resume_transport(self):
        if self._paused and len(self._buffer) <= self._limit:
            self._paused = False
            self._transport.resume_reading()

    def _feed_eof(self):
        self._eof = True
        self._wakeup_waiter()

    def at_eof(self):
        """Return True if the buffer is empty and 'feed_eof' was called."""
        return self._eof and not self._buffer

    def _feed_data(self, data):
        _ensure_can_read(self._mode)
        assert not self._eof, 'feed_data after feed_eof'

        if not data:
            return

        self._buffer.extend(data)
        self._wakeup_waiter()

        if (self._transport is not None and
                not self._paused and
                len(self._buffer) > 2 * self._limit):
            try:
                self._transport.pause_reading()
            except NotImplementedError:
                # The transport can't be paused.
                # We'll just have to buffer all data.
                # Forget the transport so we don't keep trying.
                self._transport = None
            else:
                self._paused = True

    async def _wait_for_data(self, func_name):
        """Wait until feed_data() or feed_eof() is called.

        If stream was paused, automatically resume it.
        """
        # StreamReader uses a future to link the protocol feed_data() method
        # to a read coroutine. Running two read coroutines at the same time
        # would have an unexpected behaviour. It would not possible to know
        # which coroutine would get the next data.
        if self._waiter is not None:
            raise RuntimeError(
                f'{func_name}() called while another coroutine is '
                f'already waiting for incoming data')

        assert not self._eof, '_wait_for_data after EOF'

        # Waiting for data while paused will make deadlock, so prevent it.
        # This is essential for readexactly(n) for case when n > self._limit.
        if self._paused:
            self._paused = False
            self._transport.resume_reading()

        self._waiter = self._loop.create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None

    async def read(self, n=4096):
        """Read up to `n` bytes from the stream.

        This function try to read `n` bytes, and may return
        less or equal bytes than requested, but at least one byte. If EOF was
        received before any byte is read, this function returns empty byte
        object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        _ensure_can_read(self._mode)

        if self._exception is not None:
            raise self._exception

        if n == 0:
            return b''

        if not self._buffer and not self._eof:
            await self._wait_for_data('read')

        # This will work right even if buffer is less than n bytes
        data = bytes(self._buffer[:n])
        del self._buffer[:n]

        self._maybe_resume_transport()
        return data

    def __aiter__(self):
        _ensure_can_read(self._mode)
        return self

    async def __anext__(self):
        val = await self.read()
        if not val:
            raise StopAsyncIteration
        return val

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


class AbstractStreamModifier(abc.HalfCloseableStream):
    """Interpose on top of a stream.

    Use this class as a base for writing your own stream modifiers.
    """

    def __init__(self, *, lower_stream=None):
        self._lower_stream = lower_stream

    # AsyncResource

    async def __aenter__(self):
        return await self._lower_stream.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._lower_stream.__aexit__(exc_type, exc_val, exc_tb)

    async def close(self):
        await self._lower_stream.close()

    async def wait_closed(self):
        await self._lower_stream.wait_closed()

    # SendStream

    async def write(self, data):
        await self._lower_stream.write(data)

    # no-op, more or less
    async def drain(self):
        await self._lower_stream.drain()

#   async def wait_write_all_might_not_block(self):
#       await self._lower_stream.wait_write_all_might_not_block()

    # ReceiveStream

    async def read(self, max_bytes=None):
        return await self._lower_stream.read(max_bytes)

    # HalfCloseableStream

    async def write_eof(self):
        await self._lower_stream.write_eof()

    def can_write_eof(self):
        return self._lower_stream.can_write_eof()

    # asyncio-specific methods. All of these are or should be deprecated
    # because, in this design they are not necessary.

    def _sync_close(self):
        return self._lower_stream._sync_close()

    def exception(self):
        return self._lower_stream.exception()

    def write_eof(self):
        return self._lower_stream.write_eof()

    def at_eof(self):
        return self._lower_stream.at_eof()

    def is_closing(self):
        return self._lower_stream.is_closing()

    def _set_exception(self, exc):
        return self._lower_stream._set_exception(exc)

    def _set_transport(self, transport):
        return self._lower_stream._set_transport(transport)

    def _set_protocol(self, protocol):
        return self._lower_stream._set_protocol(protocol)

    @property
    def transport(self):
        return self._lower_stream.transport

    @property
    def mode(self):
        return self._lower_stream.mode

    @property
    def _loop(self):
        return self._lower_stream._loop

    @property
    def _buffer(self):
        return self._lower_stream._buffer

    @property
    def _waiter(self):
        return self._lower_stream._waiter

    @property
    def _source_traceback(self):
        return self._lower_stream._source_traceback

    def get_extra_info(self, name, default=None):
        return self._lower_stream.get_extra_info(name, default=default)

    def _is(self, other):
        if self is other:
            return True
        return self._lower_stream._is(other)

    # for testing
    def _feed_data(self, data):
        return self._lower_stream._feed_data(data)

    def _feed_eof(self):
        return self._lower_stream._feed_eof()

    def __aiter__(self):
        self._iter = self._lower_stream.__aiter__()
        return self

    async def __anext__(self):
        return await self._iter.__anext__()


class _TrioWrap(AbstractStreamModifier):
    """a shallow wrapper to translate trio names to asyncio"""
    def __init__(self, wrapped):
        self._wrapped = wrapped

    async def send_all(self,data):
        try:
            await self._wrapped.write(data)
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise

    async def receive_some(self,n=4096):
        try:
            return await self._wrapped.read(n)
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise

    async def aclose(self):
        try:
            await self._wrapped.close()
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise

class _TrioUnwrap:
    """A mixin to translate asyncio calls to trio names"""
    async def write(self, data):
        try:
            await self.send_all(data)
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise

    async def read(self, n=None):
        try:
            return await self.receive_some(n)
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise

    async def close(self):
        try:
            await self.aclose()
        except BaseException as exc:
            import pdb;pdb.set_trace()
            raise


class SSLStream(_TrioUnwrap, AbstractStreamModifier, _SSLStream):
    """Packages a Trio SSLStream"""

    def __init__(self, transport_stream, ssl_context, **kw):
        super().__init__(lower_stream=transport_stream)
        _SSLStream.__init__(self, _TrioWrap(transport_stream), ssl_context, **kw)

    def get_extra_info(self, name, default=None):
        if name == "sslcontext":
            return self.context
        return self._lower_stream.get_extra_info(name, default=default)


class BufferedWriter(AbstractStreamModifier):
    """A stream modifier that buffers written data.

    TODO: optionally add a task that writes in the background.
    """
    def __init__(self, wraps, write_limit=_DEFAULT_LIMIT//2):
        super().__init__(lower_stream=lower_stream)
        self._write_limit = read_limit
        self._send_buffer = bytearray()

    def __repr__(self):
        info = [self.__class__.__name__]
        info.append(f'wraps={self._lower_stream}')
        if self._limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._limit}')
        return '<{}>'.format(' '.join(info))

    async def write(self, data):
        """
        Accumulate written data.
        """
        self._send_buffer.extend(data)
        if len(self._send_buffer) >= self._write_limit:
            await self.drain()

    async def drain(self):
        """
        Flush the data in this buffer.
        """
        buf = bytes(self._send_buffer)
        if not buf:
            return
        self._send_buffer.clear()
        try:
            await self.write_all(buf)
        except BaseException:
            # We have no idea how much has been written,
            # so the buffer is to be considered broken.
            self._send_buffer = None
            raise


class BufferedReader(AbstractStreamModifier):
    """A stream modifier that buffers read data.
    """
    def __init__(self, lower_stream, read_limit=_DEFAULT_LIMIT//2, write_limit=0):
        super().__init__(lower_stream=lower_stream)
        self._read_limit = read_limit
        self._read_buffer = bytearray()

        if read_limit <= 0:
            raise ValueError('Limit cannot be <= 0')

    @property
    def read_buffer(self):
        return self._read_buffer

    @property
    def _buffer(self):
        return self._read_buffer

    def __repr__(self):
        info = [self.__class__.__name__]
        info.append(f'wraps={self._lower_stream}')
        if self._read_limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._read_limit}')
        return '<{}>'.format(' '.join(info))

    async def read(self, n=None):
        """Get at most n bytes from the buffer.

        If the buffer is empty, fill it.
        """
        buf = self._read_buffer
        if not buf:
            data = await self._lower_stream.read(n or self._read_limit)
            if not data:
                return data
            if n is None or len(data) <= n:
                return data
            buf[:] = data[n:]
            data = data[:n]
        elif n is None or len(buf) == n:
            data = bytes(buf)
            buf.clear()
        else:
            data = bytes(buf[:n])
            del buf[:n]
        return data

    async def extend_buffer(self):
        """Extends the buffer with more data.
        
        This method returns the number of new bytes.
        If zero, EOF has been seen.
        """
        data = await self._lower_stream.read()
        if not data:
            return False
        self._read_buffer.extend(data)
        return len(data)


class LegacyStream(AbstractStreamModifier):
    """Front-end for exposing a Stream to legacy code.

    This exposes write(), writelines(), [can_]write_eof(),
    get_extra_info() and close().
    
    Accessing the underlying transport and its attributes is deprecated and
    scheduled for removal in 3.10.

    aclose() is named close() here.
    send_all/send_eof() is named write/write_eof() here.
    receive_some() is named read() here.

    """

    def __init__(self, mode, *,
                 transport=None,
                 protocol=None,
                 loop=None,
                 limit=_DEFAULT_LIMIT,
                 is_server_side=False,
                 _asyncio_internal=False):

        super().__init__(lower_stream=TransportStream(mode,
                 transport=transport,
                 protocol=protocol,
                 loop=loop,
                 limit=limit,
                 _asyncio_internal=_asyncio_internal))

        self._is_server_side = is_server_side

        # The line length limit is  a security feature;
        # it also doubles as half the buffer limit.

        self._limit = limit
        self._buffered = False

    def __repr__(self):
        info = [self.__class__.__name__]
        info.append(f'wraps={self._lower_stream}')
        if self._limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._limit}')
        return '<{}>'.format(' '.join(info))

    @property
    def read_buffer(self):
        if not self._buffered:
            self._lower_stream = BufferedReader(self._lower_stream, self._limit)
        self._buffered = True
        return self._lower_stream.read_buffer

    def is_server_side(self):
        return self._is_server_side

    @property
    def transport(self):
        warnings.warn("Stream.transport attribute is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        return super().transport

    async def writelines(self, data):
        for line in data:
            await self.write(line)

    async def abort(self):
        # TODO call close with a zero timeout
        await self.close()
        await self.wait_closed()

    async def sendfile(self, file, offset=0, count=None, *, fallback=True):
        await self.drain()  # check for stream mode and exceptions
        # TODO if possible. For now, fall back.
        if not fallback:
            raise exceptions.SendfileNotAvailableError

        if offset:
            file.seek(offset)

        blocksize = (
            min(count, constants.SENDFILE_FALLBACK_READBUFFER_SIZE)
            if count else constants.SENDFILE_FALLBACK_READBUFFER_SIZE
        )
        buf = bytearray(blocksize)
        total_sent = 0
        try:
            while True:
                if count:
                    blocksize = min(count - total_sent, blocksize)
                    if blocksize <= 0:
                        break
                view = memoryview(buf)[:blocksize]
                read = await self._loop.run_in_executor(None, file.readinto, view)
                if not read:
                    break  # EOF
                await self.write(view[:read])
                total_sent += read
            return total_sent
        finally:
            if total_sent > 0 and hasattr(file, 'seek'):
                file.seek(offset + total_sent)

        return await self._loop.sendfile(self._transport, file,
                                         offset, count, fallback=fallback)

    def feed_data(self, data):
        warnings.warn("Stream.feed_data() is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        self._feed_data(data)

    def feed_eof(self):
        warnings.warn("Stream.feed_eof() is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        self._feed_eof()

    async def start_tls(self, sslcontext, *,
                        server_hostname=None,
                        https_compatible=True,
                        ssl_handshake_timeout=None):
        # TODO: check for exceptions?

        self._lower_stream = SSLStream(self._lower_stream,
            ssl_context=sslcontext,
            server_side=self._is_server_side,
            server_hostname=server_hostname,
            https_compatible=https_compatible,
            ssl_handshake_timeout=ssl_handshake_timeout)
        self._buffered = False

    def set_exception(self, exc):
        warnings.warn("Stream.set_exception() is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        self._set_exception(exc)

    def set_transport(self, transport):
        warnings.warn("Stream.set_transport() is deprecated "
                      "since Python 3.8 and is scheduled for removal in 3.10; "
                      "it is an internal API",
                      DeprecationWarning,
                      stacklevel=2)
        self._set_transport(transport)

    async def readline(self):
        """Read chunk of data from the stream until newline (b'\n') is found.

        On success, return chunk that ends with newline. If only partial
        line can be read due to EOF, return incomplete line without
        terminating newline. When EOF was reached while no bytes read, empty
        bytes object is returned.

        If limit is reached, ValueError will be raised. In that case, if
        newline was found, complete line including newline will be removed
        from internal buffer. Else, internal buffer will be cleared. Limit is
        compared against part of the line without newline.
        """
        _ensure_can_read(self.mode)
        sep = b'\n'
        seplen = len(sep)
        buf = self.read_buffer
        try:
            line = await self.readuntil(sep)
        except exceptions.IncompleteReadError as e:
            return e.partial
        except exceptions.LimitOverrunError as e:
            if buf.startswith(sep, e.consumed):
                del buf[:e.consumed + seplen]
            else:
                buf.clear()
            raise ValueError(e.args[0])
        return line

    async def readuntil(self, separator=b'\n'):
        """Read data from the stream until ``separator`` is found.

        On success, the data and separator will be removed from the
        internal buffer (consumed). Returned data will include the
        separator at the end.

        Configured stream limit is used to check result. Limit sets the
        maximal length of data that can be returned, not counting the
        separator.

        If an EOF occurs and the complete separator is still not found,
        an IncompleteReadError exception will be raised, and the internal
        buffer will be reset.  The IncompleteReadError.partial attribute
        may contain the separator partially.

        If the data cannot be read because of over limit, a
        LimitOverrunError exception  will be raised, and the data
        will be left in the internal buffer, so it can be read again.
        """
        buf = self.read_buffer
        seplen = len(separator)
        if seplen == 0:
            raise ValueError('Separator should be at least one-byte string')

        exc = self.exception()
        if exc is not None:
            raise exc

        # Consume whole buffer except last bytes, which length is
        # one less than seplen. Let's check corner cases with
        # separator='SEPARATOR':
        # * we have received almost complete separator (without last
        #   byte). i.e buffer='some textSEPARATO'. In this case we
        #   can safely consume len(separator) - 1 bytes.
        # * last byte of buffer is first byte of separator, i.e.
        #   buffer='abcdefghijklmnopqrS'. We may safely consume
        #   everything except that last byte, but this require to
        #   analyze bytes of buffer that match partial separator.
        #   This is slow and/or require FSM. For this case our
        #   implementation is not optimal, since require rescanning
        #   of data that is known to not belong to separator. In
        #   real world, separator will not be so long to notice
        #   performance problems. Even when reading MIME-encoded
        #   messages :)

        # `offset` is the number of bytes from the beginning of the buffer
        # where there is no occurrence of `separator`.
        offset = 0

        # Loop until we find `separator` in the buffer, exceed the buffer size,
        # or an EOF has happened.
        eof = self.at_eof()

        while True:
            buflen = len(buf)

            # Check if we now have enough data in the buffer for `separator` to
            # fit.
            if buflen - offset >= seplen:
                isep = buf.find(separator, offset)

                if isep != -1:
                    # `separator` is in the buffer. `isep` will be used later
                    # to retrieve the data.
                    break

                # see upper comment for explanation.
                offset = buflen + 1 - seplen
                if offset > self._limit:
                    raise exceptions.LimitOverrunError(
                        'Separator is not found, and chunk exceed the limit',
                        offset)

            # Complete message (with full separator) may be present in buffer
            # even when EOF flag is set. This may happen when the last chunk
            # adds data which makes separator be found. That's why we check for
            # EOF *ater* inspecting the buffer.
            if eof:
                chunk = bytes(buf)
                buf.clear()
                raise exceptions.IncompleteReadError(chunk, None)

            # _wait_for_data() will resume reading if stream was paused.
            eof = not await self._lower_stream.extend_buffer()

        if isep > self._limit:
            raise exceptions.LimitOverrunError(
                'Separator is found, but chunk is longer than limit', isep)

        chunk = buf[:isep + seplen]
        del buf[:isep + seplen]
        return bytes(chunk)

    async def read(self, n=-1):
        """Read up to `n` bytes from the stream.

        If n is not provided, or set to -1, read until EOF and return all read
        bytes. If the EOF was received and the internal buffer is empty, return
        an empty bytes object.

        If n is zero, return empty bytes object immediately.

        If n is positive, this function try to read `n` bytes, and may return
        less or equal bytes than requested, but at least one byte. If EOF was
        received before any byte is read, this function returns empty byte
        object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        _ensure_can_read(self.mode)

        exc = self.exception()
        if exc is not None:
            raise exc

        if n == 0:
            return b''

        if n > 0:
            return await self._lower_stream.read(n)

        # Just call self.read(self._limit) until EOF.
        blocks = []
        while True:
            block = await self._lower_stream.read(self._limit)
            if not block:
                break
            blocks.append(block)
        return b''.join(blocks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        val = await self.readline()
        if val == b'':
            raise StopAsyncIteration
        return val

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

Stream = LegacyStream
