from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import logging
import threading
import time
import weakref
from collections.abc import AsyncIterator, Callable, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from . import _native
from ._chunks import MidiBytes, MidiChunk, PcmChunk, _midi, _pcm
from ._timecode import (
    _RATES,
    FrameRate,
    FrameRateLike,
    Timecode,
    _frame_rate,
    _from_native,
    _optional_rate,
)
from ._validate import check_int, check_offset, check_timestamp

if TYPE_CHECKING:
    from ._native import NativeEvent

_log = logging.getLogger("timecode_tools")

EVENTS = ("frame", "lock", "lost", "jump", "end", "error")


@dataclass(frozen=True)
class LockInfo:
    """Payload of the ``lock`` event."""

    timecode: Timecode
    frame_rate: FrameRate | None
    #: One-based channel that carries LTC (``None`` for MTC).
    channel: int | None


@dataclass(frozen=True)
class ReceiverEvent:
    """An item from :meth:`TimecodeReceiver.events`: ``type`` is ``lock``,
    ``lost``, ``jump`` or ``end``. ``timecode`` is set for ``lock`` and
    ``jump``; ``frame_rate`` and ``channel`` for ``lock``."""

    type: str
    timecode: Timecode | None = None
    frame_rate: FrameRate | None = None
    channel: int | None = None


class TimecodeWait(concurrent.futures.Future[Timecode]):
    """The result of :meth:`TimecodeReceiver.wait_for`: a
    ``concurrent.futures.Future`` that can also be awaited."""

    def __await__(self) -> Generator[Any, None, Timecode]:
        return asyncio.wrap_future(self).__await__()


class _Subscription:
    """An async iterator fed from any thread. With buffer ``latest`` only the
    newest unread item is kept; with ``all`` every item is queued. ``fail``
    ends the iteration with an error once the items before it are read."""

    def __init__(self, owner: TimecodeReceiver, kinds: tuple[str, ...], buffer: str) -> None:
        if buffer not in ("latest", "all"):
            raise ValueError(f"buffer must be 'latest' or 'all', got {buffer!r}")
        self._owner = owner
        self.kinds = kinds
        self._latest = buffer == "latest"
        self._items: collections.deque[Any] = collections.deque()
        self._lock = threading.Lock()
        self._done = False
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiter: asyncio.Future[None] | None = None

    def put(self, item: Any) -> None:
        with self._lock:
            if self._done:
                return
            if self._latest:
                self._items.clear()
            self._items.append(item)
        self._wake()

    def fail(self, error: BaseException) -> None:
        with self._lock:
            if self._done:
                return
            self._failure = error
        self.finish()

    def finish(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._owner._unsubscribe(self)
        self._wake()

    def _wake(self) -> None:
        loop, waiter = self._loop, self._waiter
        if loop is None or waiter is None:
            return

        def wake() -> None:
            if not waiter.done():
                waiter.set_result(None)

        try:
            loop.call_soon_threadsafe(wake)
        except RuntimeError:
            pass  # the loop is closed

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> Any:
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._items:
                    return self._items.popleft()
                if self._done:
                    if self._failure is not None:
                        error, self._failure = self._failure, None
                        raise error
                    raise StopAsyncIteration
                self._loop = loop
                self._waiter = waiter = loop.create_future()
            try:
                await waiter
            finally:
                self._waiter = None


async def _iterate(sub: _Subscription) -> AsyncIterator[Any]:
    # An async generator, so that leaving an `async for` loop (break, return,
    # an exception) closes it and unsubscribes.
    try:
        async for item in sub:
            yield item
    finally:
        sub.finish()


class _Source:
    def __init__(self, source: Any) -> None:
        self.source = source
        self.active = True
        self.task: asyncio.Task[None] | None = None
        self.thread: threading.Thread | None = None


def _watchdog(ref: weakref.ReferenceType[TimecodeReceiver], cond: threading.Condition) -> None:
    # Reports 'lost' when chunks stop arriving. Holds only a weak reference,
    # so an unclosed receiver can still be garbage collected; then it exits.
    while True:
        with cond:
            rx = ref()
            if rx is None or rx._closed:
                return
            deadline = rx._deadline
            now = time.monotonic()
            if deadline is None or now < deadline:
                wait = 1.0 if deadline is None else min(deadline - now, 1.0)
                del rx
                cond.wait(wait)
                continue
            rx._lose()
            del rx


class _NativeReceiver(Protocol):
    @property
    def locked(self) -> bool: ...
    @property
    def detected_rate(self) -> str | None: ...

    offset_frames: int

    def frames_at(self, timestamp: float) -> float | None: ...
    def signal_lost(self) -> list[NativeEvent]: ...


class TimecodeReceiver:
    """The interface shared by :class:`LtcReceiver` and :class:`MtcReceiver`.

    Feed it with :meth:`push` (from any thread) or :meth:`attach`, and listen
    with :meth:`on`, :meth:`frames` / :meth:`events` or :meth:`wait_for`.
    The timecode logic itself runs in the Rust core.

    Events: ``frame`` (a :class:`Timecode`, every frame while locked),
    ``lock`` (:class:`LockInfo`), ``lost`` (no argument), ``jump`` (a
    :class:`Timecode`), ``end`` (the attached input finished) and ``error``
    (the attached input raised; the exception).
    """

    def __init__(self, native: _NativeReceiver, frame_rate: FrameRate, lost_after: float) -> None:
        if type(self) is TimecodeReceiver:
            raise TypeError("use LtcReceiver or MtcReceiver")
        self._native = native
        self._frame_rate = frame_rate
        self._lost_after = lost_after
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Callable[..., Any]]] = {e: [] for e in EVENTS}
        self._subscriptions: list[_Subscription] = []
        self._waiters: dict[TimecodeWait, str] = {}
        self._current: Timecode | None = None
        self._source: _Source | None = None
        self._closed = False
        self._cond = threading.Condition(self._lock)
        self._deadline: float | None = None
        self._watch: threading.Thread | None = None
        self._last_chunk_at: float | None = None
        self._chunk_gap = 0.0

    # Listening ----------------------------------------------------------

    def on(self, event: str, callback: Callable[..., Any] | None = None, *, once: bool = False) -> Any:
        """Calls ``callback`` on ``event``. Without a callback, returns a
        decorator: ``@rx.on("frame")``. Callbacks run on the thread that
        delivered the input (the caller of :meth:`push`, the reading thread
        or task of :meth:`attach`, or the loss timer's thread)."""
        if event not in self._listeners:
            raise ValueError(f"unknown event {event!r}; expected one of {', '.join(EVENTS)}")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            if not callable(fn):
                raise TypeError("callback must be callable")
            if once:

                def wrapper(*args: Any) -> Any:
                    self.off(event, wrapper)
                    return fn(*args)

                wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
                target = wrapper
            else:
                target = fn
            with self._lock:
                self._listeners[event].append(target)
            return fn

        return register if callback is None else register(callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        """Removes a callback added with :meth:`on`."""
        with self._lock:
            listeners = self._listeners.get(event, [])
            for i, fn in enumerate(listeners):
                if fn == callback or getattr(fn, "__wrapped__", None) == callback:
                    del listeners[i]
                    return

    def listener_count(self, event: str) -> int:
        with self._lock:
            return len(self._listeners.get(event, []))

    def frames(self, buffer: str = "latest") -> AsyncIterator[Timecode]:
        """Async iterator of frames (as the ``frame`` event). It subscribes at
        once; leaving the ``async for`` loop unsubscribes. A slow consumer
        gets only the newest frame unless ``buffer="all"``."""
        return _iterate(self._subscribe(_Subscription(self, ("frame",), buffer)))

    def events(self, buffer: str = "all") -> AsyncIterator[ReceiverEvent]:
        """Async iterator of :class:`ReceiverEvent` (``lock``, ``lost``,
        ``jump``, and ``end`` when the attached input finishes). An error
        from the attached input is raised from the iterator. Every event is
        delivered unless ``buffer="latest"``."""
        return _iterate(self._subscribe(_Subscription(self, ("lock", "lost", "jump", "end", "error"), buffer)))

    def _subscribe(self, sub: _Subscription) -> _Subscription:
        with self._lock:
            if self._closed:
                sub.finish()
            else:
                self._subscriptions.append(sub)
        return sub

    def _unsubscribe(self, sub: _Subscription) -> None:
        with self._lock:
            if sub in self._subscriptions:
                self._subscriptions.remove(sub)

    def wait_for(self, target: str | Timecode) -> TimecodeWait:
        """Completes with the first timecode at or past ``target`` that
        directly continues a frame before it, i.e. when normal playback
        passes the target (also across midnight). A jump over or onto the
        target does not count.

        Returns a future you can ``await`` (wrap it in ``asyncio.wait_for``
        for a timeout) or block on with ``.result(timeout)``. ``.cancel()``
        stops waiting. It fails with ``ReceiverClosed`` if the receiver is
        closed first.
        """
        text = str(target)
        Timecode.parse(text)  # validate the format now
        future = TimecodeWait()
        with self._lock:
            if self._closed:
                future.set_exception(ReceiverClosed("receiver closed"))
                return future
            self._waiters[future] = text
        future.add_done_callback(self._forget_waiter)
        return future

    def _forget_waiter(self, future: concurrent.futures.Future[Timecode]) -> None:
        with self._lock:
            self._waiters.pop(future, None)  # type: ignore[call-overload]

    # State ----------------------------------------------------------------

    @property
    def locked(self) -> bool:
        """True between ``lock`` and ``lost``."""
        with self._lock:
            return self._native.locked

    @property
    def current(self) -> Timecode | None:
        """The most recent frame, or the position of an MTC locate."""
        return self._current

    @property
    def frame_rate(self) -> FrameRate:
        """The configured frame rate (``AUTO``: from the source)."""
        return self._frame_rate

    @property
    def detected_rate(self) -> FrameRate | None:
        """The rate in use: configured, or detected; ``None`` until known."""
        with self._lock:
            return _optional_rate(self._native.detected_rate)

    @property
    def offset_frames(self) -> int:
        """Frames added to every timecode (latency compensation). Can be changed."""
        with self._lock:
            return self._native.offset_frames

    @offset_frames.setter
    def offset_frames(self, frames: int) -> None:
        check_offset(frames)
        with self._lock:
            self._native.offset_frames = frames

    @property
    def lost_after(self) -> float:
        """Seconds without a followed frame before ``lost``."""
        return self._lost_after

    @property
    def attached(self) -> bool:
        """True while an input is attached."""
        return self._source is not None

    @property
    def closed(self) -> bool:
        """True after :meth:`close`."""
        return self._closed

    def position_at(self, timestamp: float) -> Timecode | None:
        """The timecode at ``timestamp`` (seconds, the same monotonic clock as
        the chunk timestamps), interpolated or extrapolated from the recent
        signal, with ``offset_frames`` applied. ``None`` until the signal has
        been locked long enough to fit a clock, after it is lost, or after
        :meth:`close`."""
        frames = self.frames_at(timestamp)
        rate = self.detected_rate
        if frames is None or rate is None:
            return None
        tc = Timecode.from_frame_index(int(frames // 1), rate)
        return Timecode(tc.hours, tc.minutes, tc.seconds, tc.frames, tc.drop_frame, rate, float(timestamp))

    def frames_at(self, timestamp: float) -> float | None:
        """:meth:`position_at` as a fractional frame count since 00:00:00:00
        (not wrapped at midnight)."""
        check_timestamp(timestamp)
        with self._lock:
            if self._closed:
                return None
            return self._native.frames_at(float(timestamp))

    # Input ----------------------------------------------------------------

    def push(self, chunk: Any) -> None:
        """Delivers one chunk of input. Safe to call from any thread, for
        example an audio or MIDI callback. Callbacks run before it returns."""
        with self._lock:
            if self._closed:
                raise ReceiverClosed("receiver is closed")
            self._dispatch(self._push_native(chunk))
            self._after_chunk()

    def _push_native(self, chunk: Any) -> list[NativeEvent]:
        raise NotImplementedError

    def attach(self, source: Any) -> TimecodeReceiver:
        """Starts reading chunks from ``source``.

        - An async iterable is read by a task on the running event loop
          (call ``attach`` from a coroutine).
        - Any other iterable (a generator, a file-like reader) is read on a
          daemon thread.

        When the input finishes, ``lost`` is reported if locked, then
        ``end``. If it raises, ``error`` is reported and :meth:`events`
        raises the exception.
        """
        with self._lock:
            if self._closed:
                raise ReceiverClosed("receiver is closed")
            if self._source is not None:
                raise RuntimeError("already attached; call detach() first")
            if hasattr(source, "__aiter__"):
                loop = asyncio.get_running_loop()  # RuntimeError outside a coroutine
                src = _Source(source)
                self._source = src
                src.task = loop.create_task(self._read_async(src))
            elif hasattr(source, "__iter__") and not isinstance(source, (bytes, bytearray, str, PcmChunk)):
                src = _Source(source)
                self._source = src
                src.thread = threading.Thread(
                    target=self._read_sync, args=(src,), daemon=True, name="timecode-tools input"
                )
                src.thread.start()
            else:
                raise TypeError("attach() expects an iterable or async iterable of chunks")
        return self

    async def _read_async(self, src: _Source) -> None:
        error: BaseException | None = None
        try:
            async for chunk in src.source:
                if not src.active:
                    break
                self.push(chunk)
                # Let other tasks (iterators, wait_for awaiters) run between
                # chunks, even when the source never awaits.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - reported as 'error'
            error = e
        finally:
            closer = getattr(src.source, "aclose", None)
            if closer is not None and not src.active:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        self._finish(src, error)

    def _read_sync(self, src: _Source) -> None:
        error: BaseException | None = None
        try:
            for chunk in src.source:
                if not src.active:
                    break
                self.push(chunk)
        except Exception as e:  # noqa: BLE001 - reported as 'error'
            error = e
        self._finish(src, error)

    def _finish(self, src: _Source, error: BaseException | None) -> None:
        with self._lock:
            if error is not None and src.active and not isinstance(error, ReceiverClosed):
                self._emit("error", error)
            if self._source is src:
                self._source = None
                self._lose()
                self._emit("end")

    def detach(self) -> None:
        """Stops reading from the attached input and reports ``lost`` if
        locked. A thread blocked reading the input stops after its current
        read returns. ``wait_for`` futures stay pending."""
        with self._lock:
            src = self._source
            self._source = None
            if src is not None:
                src.active = False
                if src.task is not None:
                    try:
                        src.task.get_loop().call_soon_threadsafe(src.task.cancel)
                    except RuntimeError:
                        pass  # the loop is closed
            self._lose()

    def close(self) -> None:
        """Detaches, ends every :meth:`frames` / :meth:`events` iterator and
        fails pending :meth:`wait_for` futures. The receiver cannot be used
        afterwards. Also called on leaving a ``with`` block."""
        with self._lock:
            if self._closed:
                return
            self.detach()
            self._closed = True
            self._cond.notify_all()
            subs = list(self._subscriptions)
            waiters = list(self._waiters)
            self._waiters.clear()
        for sub in subs:
            sub.finish()
        for waiter in waiters:
            try:
                waiter.set_exception(ReceiverClosed("receiver closed"))
            except concurrent.futures.InvalidStateError:
                pass  # cancelled meanwhile

    def __enter__(self) -> TimecodeReceiver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Internals ------------------------------------------------------------

    def _emit(self, event: str, *args: Any) -> None:
        for fn in list(self._listeners[event]):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - one listener must not break the others
                _log.exception("timecode_tools: %r listener raised", event)
        if (
            event == "error"
            and not self._listeners["error"]
            and not any("error" in s.kinds for s in self._subscriptions)
        ):
            _log.error("timecode_tools: the attached input raised", exc_info=args[0])
        for sub in list(self._subscriptions):
            if event not in sub.kinds:
                continue
            if event == "error":
                sub.fail(args[0])
            elif event == "frame":
                sub.put(args[0])
            elif event == "lock":
                info: LockInfo = args[0]
                sub.put(ReceiverEvent("lock", info.timecode, info.frame_rate, info.channel))
            elif event == "jump":
                sub.put(ReceiverEvent("jump", args[0]))
            else:
                sub.put(ReceiverEvent(event))

    def _dispatch(self, events: list[NativeEvent]) -> None:
        for kind, native_tc, previous in events:
            if kind == "lost":
                self._emit("lost")
                continue
            assert native_tc is not None
            tc = _from_native(native_tc)
            if kind == "lock":
                self._emit("lock", LockInfo(tc, tc.frame_rate, native_tc[9]))
            elif kind == "jump":
                self._current = tc
                self._emit("jump", tc)
            else:
                self._current = tc
                self._emit("frame", tc)
                if previous is not None:
                    self._resolve_waiters(tc, previous)

    def _resolve_waiters(self, tc: Timecode, previous: int) -> None:
        if not self._waiters or tc.frame_rate is None:
            return
        rate = _RATES[tc.frame_rate]
        day = _native.frames_per_day(rate.nominal, rate.drop)
        index = tc.frame_index()
        step = (index - previous) % day
        if step <= 0:
            return
        for waiter, text in list(self._waiters.items()):
            try:
                target = Timecode.parse(text, tc.frame_rate).frame_index()
            except ValueError:
                continue  # not valid at this rate
            if (index - target) % day < step:
                del self._waiters[waiter]
                try:
                    waiter.set_result(tc)
                except concurrent.futures.InvalidStateError:
                    pass  # cancelled meanwhile

    def _lose(self) -> None:
        with self._lock:
            self._deadline = None
            self._dispatch(self._native.signal_lost())

    # Detects loss when chunks stop arriving altogether: no chunk for
    # lost_after plus twice the usual gap between chunks (so input delivered
    # in large blocks is not mistaken for a stop).
    def _after_chunk(self) -> None:
        now = time.monotonic()
        if self._last_chunk_at is not None:
            self._chunk_gap = min(now - self._last_chunk_at, 5.0)
        self._last_chunk_at = now
        if not self._native.locked or self._closed:
            self._deadline = None
            return
        self._deadline = now + self._lost_after + 2 * self._chunk_gap
        if self._watch is None or not self._watch.is_alive():
            self._watch = threading.Thread(
                target=_watchdog, args=(weakref.ref(self), self._cond), daemon=True, name="timecode-tools loss timer"
            )
            self._watch.start()
        else:
            self._cond.notify_all()


class ReceiverClosed(RuntimeError):
    """The receiver was closed."""


def _receiver_args(frame_rate: FrameRateLike, offset_frames: int, lost_after: float) -> tuple[FrameRate, int, float]:
    rate = _frame_rate(frame_rate, True)
    check_offset(offset_frames)
    if isinstance(lost_after, bool) or not isinstance(lost_after, (int, float)) or not lost_after > 0:
        raise ValueError(f"lost_after must be a positive number of seconds, got {lost_after!r}")
    return rate, offset_frames, float(lost_after)


class LtcReceiver(TimecodeReceiver):
    """Follows SMPTE LTC in a stream of :class:`PcmChunk`."""

    def __init__(
        self,
        frame_rate: FrameRateLike = FrameRate.AUTO,
        offset_frames: int = 0,
        channel: int = 1,
        lost_after: float = 0.3,
    ) -> None:
        rate, offset, lost = _receiver_args(frame_rate, offset_frames, lost_after)
        check_int("channel", channel, 1, 64)
        self._ltc = _native.NativeLtcReceiver(rate.value, offset, lost, channel)
        super().__init__(self._ltc, rate, lost)
        self._channel = channel

    @property
    def channel(self) -> int:
        """One-based channel that carries LTC."""
        return self._channel

    @property
    def level(self) -> float:
        """Recent peak input level of the LTC channel, 0..1."""
        with self._lock:
            return self._ltc.level

    def push(self, chunk: PcmChunk) -> None:
        """Delivers one :class:`PcmChunk`. Safe to call from any thread."""
        super().push(chunk)

    def _push_native(self, chunk: Any) -> list[NativeEvent]:
        if not isinstance(chunk, PcmChunk):
            raise TypeError(f"expected a PcmChunk, got {type(chunk).__name__}")
        check_int("sample_rate", chunk.sample_rate, 8000, 768000)
        if chunk.channels is not None:
            check_int("channels", chunk.channels, 1, 64)
        data, float32, channels = _pcm(chunk.samples, chunk.channels)
        check_int("channels", channels, 1, 64)
        at = check_timestamp(chunk.timestamp, allow_none=True)
        if at is None:
            at = time.perf_counter()
        return self._ltc.push(data, float32, chunk.sample_rate, channels, at)


class MtcReceiver(TimecodeReceiver):
    """Follows MIDI Time Code in a stream of MIDI bytes: :class:`MidiChunk`,
    or bare ``bytes`` / ``bytearray`` / sequences of ints (timestamped on
    arrival). A full-frame message (a locate) ends any chase and is reported
    as a ``jump``."""

    def __init__(
        self,
        frame_rate: FrameRateLike = FrameRate.AUTO,
        offset_frames: int = 0,
        lost_after: float = 0.3,
    ) -> None:
        rate, offset, lost = _receiver_args(frame_rate, offset_frames, lost_after)
        self._mtc = _native.NativeMtcReceiver(rate.value, offset, lost)
        super().__init__(self._mtc, rate, lost)

    def push(self, chunk: MidiChunk | MidiBytes) -> None:
        """Delivers MIDI bytes. Safe to call from any thread."""
        super().push(chunk)

    def _push_native(self, chunk: Any) -> list[NativeEvent]:
        if isinstance(chunk, MidiChunk):
            data, at = _midi(chunk.data), check_timestamp(chunk.timestamp, allow_none=True)
        else:
            data, at = _midi(chunk), None
        if at is None:
            at = time.perf_counter()
        return self._mtc.push(data, at)
