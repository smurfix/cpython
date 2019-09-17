__all__ = ('AsyncResource', 'SendStream', 'ReceiveStream',
           'AbstractStream', 'HalfCloseableStream')


from abc import ABCMeta, abstractmethod

from . import exceptions

# minimal classes, copied from Trio. Work in progress.

class AsyncResource(metaclass=ABCMeta):
    @abstractmethod
    async def close(self):
        """Close this resource, possibly blocking."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def _is(self, other):
        return self is other


class SendStream(AsyncResource):
    @abstractmethod
    async def write(self, data):
        """Sends the given data through the stream, blocking if
        necessary."""

    def can_write_eof(self):
        return False


class ReceiveStream(AsyncResource):
    @abstractmethod
    async def read(self, max_bytes=None):
        """Wait until there is data available on this stream, and then return
        some of it."""
    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.receive_some()
        if not data:
            raise StopAsyncIteration
        return data

    # helper that almost every protocol-minded user needs
    async def readexactly(self, n):
        """Read exactly `n` bytes.

        Raise an IncompleteReadError if EOF is reached before `n` bytes can be
        read. The IncompleteReadError.partial attribute of the exception will
        contain the partial read bytes.

        If n is zero, return empty bytes object.
        """
        if n < 0:
            raise ValueError('readexactly size can not be less than zero')

        data = await self.read(n)
        if not data:
            exceptions.IncompleteReadError('', n)

        # optimize a bit
        so_far = len(data)
        if so_far == n:
            return data

        buf = [data]
        while so_far < n:
            data = await self.read(n - so_far)
            if not data:
                incomplete = b''.join(buf)
                raise exceptions.IncompleteReadError(incomplete, n)

            buf.append(data)
            so_far += len(data)

        return ''.join(buf)


class AbstractStream(SendStream, ReceiveStream):
    pass


class HalfCloseableStream(AbstractStream):
    @abstractmethod
    async def write_eof(self):
        """Send an end-of-file indication on this stream, if possible."""

    def can_write_eof(self):
        return True

