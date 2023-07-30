import asyncio
import collections.abc as abc
import inspect
import json
import time
from collections.abc import Iterable
from threading import Lock
from contextlib import AsyncExitStack
from typing import (Any, Awaitable, Callable, Generic, Literal, TypeAlias,
                    TypeVar, cast, get_origin, get_type_hints)

import aiohttp
import six

I = TypeVar('I')
O = TypeVar('O')

def remove_none_value(input: I, recursion: bool = False, check_circular: bool = True, ref_set: set[int] | None = None) -> I:
    if check_circular:
        ref_set = ref_set if ref_set is not None else set()
        if id(input) in ref_set:
            return input
        ref_set.add(id(input))
    if isinstance(input, dict):
        keys = list(input.keys())
        for k in keys:
            v = input[k]
            if v is None:
                del input[k]
            elif recursion:
                remove_none_value(v, recursion=recursion, check_circular=check_circular, ref_set=ref_set)
    elif isinstance(input, Iterable) and not isinstance(input, six.string_types):
        for e in input:
            remove_none_value(e, recursion=recursion, check_circular=check_circular, ref_set=ref_set)
                
    return input

def chain_future(
    future: asyncio.Future[I],
    then: Callable[[I], O] | Callable[[I], Awaitable[O]] | None = None,
    catch: Callable[[BaseException], Any] | Callable[[BaseException], Awaitable] | None = None
) -> asyncio.Future[O]:
    result = asyncio.Future(loop=future.get_loop())
    def callback(f: asyncio.Future[I]):
        if result.done():
            return
        if f.cancelled():
            result.cancel()
            return
        e = f.exception()
        if e is None:
            if then is not None:
                out = then(f.result())
                if inspect.isawaitable(out):
                    f_out = asyncio.ensure_future(out)
                    link_future(result, f_out)
                else:
                    result.set_result(out)
            else:
                result.set_result(f.result())
        else:
            if catch is not None:
                out = catch(e)
                if inspect.isawaitable(out):
                    f_out = asyncio.ensure_future(out)
                    link_future(result, f_out)
                else:
                    result.set_result(out)
            else:
                result.set_exception(e)
    future.add_done_callback(callback)
    return result

def link_future(this_future: asyncio.Future[I], target_future: asyncio.Future[I]) -> None:
    def on_done(f: asyncio.Future):
        if f.cancelled():
            this_future.cancel()
        elif not this_future.done():
            e = f.exception()
            if e:
                this_future.set_exception(e)
            else:
                this_future.set_result(f.result())
    target_future.add_done_callback(on_done)

class AsyncExceptionCollector:
    loop: asyncio.AbstractEventLoop | None
    lock: Lock
    future_lock: Lock
    listeners_waiter: asyncio.Future[Literal[True]] | None
    exceptions_waiter: asyncio.Future[Literal[True]] | None
    listeners_counter: int
    exceptions_counter: int
    future: asyncio.Future[None]
    pending_exceptions: list[BaseException]
    listeners: list["AsyncExceptionCollectorListenerWrapper"]
    
    Callback = Callable[[BaseException], bool] | Callable[[BaseException], Awaitable[bool]]
    
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop
        self.future = asyncio.Future(loop=loop)
        self.listeners = []
        self.pending_exceptions = []
        self.lock = Lock()
        self.future_lock = Lock()
        self.listeners_waiter = None
        self.exceptions_waiter = None
        self.listeners_counter = 0
        self.exceptions_counter = 0
    
    def collect(self, consume: bool = True):
        return AsyncExceptionCollectorContextManager(self, consume)
    
    async def lock_listeners(self, timeout: float = 0):
        with self.lock:
            assert self.listeners_counter >= 0
            if self.listeners_counter > 0 or self.exceptions_counter == 0:
                self.listeners_counter += 1
                return True
            elif not self.listeners_waiter:
                waiter = asyncio.Future(loop=self.loop)
                self.listeners_waiter = waiter
            else:
                waiter = self.listeners_waiter
        t0 = time.time()
        if timeout > 0:
            try:
                await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                return False
        else:
            await waiter
        t1 = time.time()
        timeout = timeout - (t1 - t0)
        return await self.lock_listeners(timeout=timeout)
        
        
    def unlock_listeners(self):
        full_unlock = False
        pending_future: asyncio.Future[Literal[True]] | None = None
        with self.lock:
            if self.listeners_counter <= 0:
                raise ValueError('Must unlock when has locked.')
            assert self.listeners_waiter is None
            self.listeners_counter -= 1
            if self.listeners_counter == 0:
                full_unlock = True
                pending_future = self.exceptions_waiter
        if full_unlock and pending_future:
            pending_future.set_result(True)
    
        
    async def lock_exceptions(self, timeout: float = 0):
        with self.lock:
            assert self.exceptions_counter >= 0
            if self.exceptions_counter > 0 or self.listeners_counter == 0:
                self.exceptions_counter += 1
                return True
            elif not self.exceptions_waiter:
                waiter = asyncio.Future(loop=self.loop)
                self.exceptions_waiter = waiter
            else:
                waiter = self.exceptions_waiter
        t0 = time.time()
        if timeout > 0:
            try:
                await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                return False
        else:
            await waiter
        t1 = time.time()
        timeout = timeout - (t1 - t0)
        return await self.lock_listeners(timeout=timeout)
    
    def unlock_exceptions(self):
        full_unlock = False
        pending_future: asyncio.Future[Literal[True]] | None = None
        with self.lock:
            if self.exceptions_counter <= 0:
                raise ValueError('Must unlock when has locked.')
            assert self.exceptions_waiter is None
            self.exceptions_counter -= 1
            if self.exceptions_counter == 0:
                full_unlock = True
                pending_future = self.listeners_waiter
        if full_unlock and pending_future:
            pending_future.set_result(True)
            
    async def invoke_listenr(self, listener: "AsyncExceptionCollectorListenerWrapper", e: BaseException):
        try:
            res = listener.invoke(e, self)
        except BaseException as le:
            self.pending_exceptions.append(le)
            res = False
        if inspect.isawaitable(res):
            res = await res
        if res:
            return True
        else:
            return False
    
    async def add_listener(self, listener: "AsyncExceptionCollectorListener") -> "AsyncExceptionCollector":
        await self.lock_listeners()
        l = next((l for l in self.listeners if l.listener == listener), None)
        if l:
            return self
        real_listener = AsyncExceptionCollectorListenerWrapper(listener)
        try:
            if self.future.done() and not self.future.cancelled():
                e = self.future.exception()
                if e:
                    consumed = await self.invoke_listenr(real_listener, e)
                    if consumed:
                        raise RuntimeError(f'The collector has stopped, it\'s impossible to consume the exception: {e}')
            self.listeners.append(real_listener)
            return self
        finally:
            self.unlock_listeners()
    
    def remove_listener(self, listener: "AsyncExceptionCollectorListener") -> bool:
        l = next((l for l in self.listeners if l.listener == listener), None)
        if l:
            try:
                self.listeners.remove(l)
                return True
            except ValueError:
                return False
        else:
            return False
        
    async def add_exception(self, e: BaseException) -> "AsyncExceptionCollector":
        await self.lock_exceptions()
        try:
            consume = False
            for listener in self.listeners:
                try:
                    res = listener.invoke(e, self)
                except BaseException as le:
                    self.pending_exceptions.append(le)
                    res = False
                if inspect.isawaitable(res):
                    try:
                        res = await res
                    except BaseException as le:
                        self.pending_exceptions.append(le)
                        res = False
                if res:
                    consume = True
                    break
            if not consume:
                with self.future_lock:
                    if self.future.done():
                        self.pending_exceptions.append(e)
                    else:
                        self.future.set_exception(e)
            return self
        finally:
            self.unlock_exceptions()
            
    def cancel(self):
        return self.future.cancel()
    
    def error(self, e: BaseException):
        with self.future_lock:
            if not self.future.done():
                self.future.set_exception(e)
                
    def complete(self):
        with self.future_lock:
            if not self.future.done():
                self.future.set_result(None)
    
    async def wait_until_error(self, timeout: int = 0, loop: asyncio.AbstractEventLoop | None = None):
        future: asyncio.Future[None] = asyncio.Future(loop=loop or self.loop)
        lock = Lock()
        def on_done(f: asyncio.Future[None]):
            if f.cancelled():
                future.cancel()
            else:
                e = f.exception()
                with lock:
                    if not future.done():
                        if e:
                            future.set_exception(e)
                        else:
                            future.set_result(None)
        with self.future_lock:
            if not self.future.done():
                self.future.add_done_callback(on_done)
        if self.future.done():
            on_done(self.future)
                
        if timeout > 0:
            await asyncio.wait_for(future, timeout=timeout)
        else:
            await future
                    

class AsyncExceptionCollectorContextManager:
    collector: AsyncExceptionCollector
    consume: bool
    
    def __init__(self, collector: AsyncExceptionCollector, consume: bool = True) -> None:
        self.collector = collector
        self.consume = consume
        
    def __enter__(self):
        raise NotImplementedError('AsyncExceptionCollectorContextManager is an async context manager, please use it by "async with" instead of "with".')
    
    def __exit__(self):
        return False
    
    async def __aenter__(self):
        return self.collector
    
    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if exc_value:
            await self.collector.add_exception(exc_value)
        return self.consume
    
AsyncExceptionCollectorListener: TypeAlias = Callable[[BaseException, AsyncExceptionCollector], Any] | Callable[[BaseException], Any] | Callable[[], Any]

class AsyncExceptionCollectorListenerWrapper:
    listener: AsyncExceptionCollectorListener
    type_hints: dict[str, Any]
    exception_type: type[BaseException] | None
    accept_collector: bool
    exception_var_name: str | None
    collector_var_name: str | None
    reverse_order: bool
    valid: bool
    
    def __init__(self, listener: AsyncExceptionCollectorListener) -> None:
        self.listener = listener
        self.type_hints = self.get_type_hints()
        self.exception_type = None
        self.accept_collector = False
        self.exception_var_name = None
        self.collector_var_name = None
        self.reverse_order = False
        self.valid = False
        sig = inspect.signature(self.listener)
        self.analyze_signature(sig)
        if not self.valid:
            raise ValueError(f'Invalid input listener: {sig}')
                
    def is_collector_name(self, name: str):
        return name in ['collector']
    
    def is_collector_type(self, p: inspect.Parameter):
        t = self.type_hints.get(p.name)
        e_list_type = self.get_type(t, AsyncExceptionCollector)
        return p.default == inspect.Parameter.empty and e_list_type is not None
    
    def is_exception_name(self, name: str):
        return name in ['e', 'exception']
    
    def calc_exception_type(self, p: inspect.Parameter):
        if p.default != inspect.Parameter.empty:
            return None
        t = self.type_hints.get(p.name)
        e_type = self.get_type(t, BaseException)
        return e_type
    
    def invoke(self, exception: BaseException, collector: AsyncExceptionCollector):
        if not self.exception_type or isinstance(exception, self.exception_type):
            args = []
            if self.exception_type and not self.exception_var_name:
                args.append(exception)
            if self.accept_collector and not self.collector_var_name:
                args.append(collector)
            if self.reverse_order:
                args.reverse()
            kw_args = {}
            if self.exception_type and self.exception_var_name:
                kw_args[self.exception_var_name] = exception
            if self.accept_collector and self.collector_var_name:
                kw_args[self.collector_var_name] = collector
            return self.listener(*args, **kw_args)
        else:
            return None
            
                
    def analyze_signature(self, sig: inspect.Signature):
        e_backable = False
        e_list_backable = False
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.POSITIONAL_ONLY or p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                if self.is_exception_name(p.name) and self.calc_exception_type(p):
                    if self.exception_type:
                        if e_backable:
                            self.exception_type = self.calc_exception_type(p)
                            self.exception_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                            self.reverse_order = self.accept_collector
                            e_backable = False
                        elif p.default == inspect.Parameter.empty:
                            self.exception_type = None
                            self.accept_collector = False
                            return
                    else:
                        self.exception_type = self.calc_exception_type(p)
                        self.exception_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                        self.reverse_order = self.accept_collector
                        e_backable = False
                elif self.is_collector_name(p.name) and self.is_collector_type(p):
                    if self.accept_collector:
                        if e_list_backable:
                            self.accept_collector = True
                            self.collector_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                            self.reverse_order = not self.exception_type
                            e_list_backable = False
                        elif p.default == inspect.Parameter.empty:
                            self.exception_type = None
                            self.accept_collector = False
                            return
                    else:
                        self.accept_collector = True
                        self.collector_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                        self.reverse_order = not self.exception_type
                        e_list_backable = False
                elif not self.exception_type and self.calc_exception_type(p):
                    self.exception_type = self.calc_exception_type(p)
                    self.exception_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                    self.reverse_order = self.accept_collector
                    e_backable = p.default != inspect.Parameter.empty
                elif not self.accept_collector and self.is_collector_type(p):
                    self.accept_collector = True
                    self.collector_var_name = p.name if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD else None
                    self.reverse_order = not self.exception_type
                    e_list_backable = p.default != inspect.Parameter.empty
                elif p.default == inspect.Parameter.empty:
                    self.exception_type = None
                    self.accept_collector = False
                    return
            elif p.kind == inspect.Parameter.VAR_POSITIONAL:
                if not self.exception_type:
                    self.exception_type = self.calc_exception_type(p)
                    self.exception_var_name = None
                    self.reverse_order = self.accept_collector
                    e_backable = True
                if not self.accept_collector:
                    self.accept_collector = True
                    self.collector_var_name = None
                    self.reverse_order = not self.exception_type
                    e_list_backable = True
            elif p.kind == inspect.Parameter.KEYWORD_ONLY:
                if self.is_exception_name(p.name) and self.calc_exception_type(p):
                    if self.exception_type:
                        if e_backable:
                            self.exception_type = self.calc_exception_type(p)
                            self.exception_var_name = p.name
                            e_backable = False
                        elif p.default == inspect.Parameter.empty:
                            self.exception_type = None
                            self.accept_collector = False
                            return
                    else:
                        self.exception_type = self.calc_exception_type(p)
                        self.exception_var_name = p.name
                        e_backable = False
                elif self.is_collector_name(p.name) and self.is_collector_type(p):
                    if self.accept_collector:
                        if e_list_backable:
                            self.accept_collector = True
                            self.collector_var_name = p.name
                            e_list_backable = False
                        elif p.default == inspect.Parameter.empty:
                            self.exception_type = None
                            self.accept_collector = False
                            return
                    else:
                        self.accept_collector = True
                        self.collector_var_name = p.name
                        e_list_backable = False
                elif p.default == inspect.Parameter.empty:
                    self.exception_type = None
                    self.accept_collector = False
                    return
            elif p.kind == inspect.Parameter.VAR_KEYWORD:
                if not self.exception_type:
                    self.exception_type = self.calc_exception_type(p)
                    self.exception_var_name = 'exception'
                    e_backable = True
                if not self.accept_collector:
                    self.accept_collector = True
                    self.collector_var_name = 'collector'
                    e_list_backable = True
        self.valid = True

                    
    def extract_fun(self):
        if not inspect.isroutine(self.listener) and isinstance(self.listener, abc.Callable) and hasattr(self.listener, '__call__'):
            return self.listener.__call__
        else:
            return self.listener
                    
    def get_type_hints(self) -> dict[str, Any]:
        fun = self.extract_fun()
        try:
            return get_type_hints(fun)
        except:
            return {}
                        
    def get_type(self, type: Any, target_type: Any):
        if type is None:
            return target_type
        if isinstance(type, TypeVar):
            if hasattr(type, '__bound__'):
                bound = getattr(type, '__bound__')
                return self.get_type(bound, target_type)
            else:
                return target_type
        else:
            o_type = get_origin(type)
            type = o_type if o_type is not None else type
            if inspect.isclass(type):
                return type if issubclass(type, target_type) else None
            else:
                return target_type
            
class NullAsyncContext:
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_value, exc_tb):
        return False
    
R = TypeVar('R')
    
def ensure_future(callback: Callable[..., R] | Callable[..., Awaitable[R]], *args, loop: asyncio.AbstractEventLoop | None = None, **kwargs) -> asyncio.Future[R]:
    exc = None 
    try:
        res = callback(*args, **kwargs)
    except BaseException as e:
        res = None
        exc = e
    if exc is None and inspect.isawaitable(res):
        future = asyncio.ensure_future(res, loop=loop)
    else:
        future = asyncio.Future(loop=loop)
        if exc is None:
            future.set_result(res)
        else:
            future.set_exception(exc)
    return future

def real_attribute_name(inst: Any, attr_name: str):
    if attr_name.startswith('__'):
        return f'_{inst.__class__.__name__}{attr_name}'
    else:
        return attr_name

class Postponer:
    LOCK: Lock = Lock()
    lock: Lock
    
    future: asyncio.Future[None]
    
    def __init__(self) -> None:
        self.lock = Lock()
        self.future = asyncio.Future()
        self.future.set_result(None)
    
    def _chain_task(self, callback: Callable[[], Awaitable[None]]):
        async def then(_):
            await callback()
        with self.lock:
            self.future = chain_future(self.future, then=then)
            
    async def postpone(self, inst: Any, *methods, always: bool = True):
        stack = AsyncExitStack()
        try:
            for method in methods:
                if isinstance(method, six.string_types):
                    real_method_name = real_attribute_name(inst, method)
                    if not hasattr(inst, real_method_name) or not inspect.ismethod(getattr(inst, real_method_name)):
                        raise ValueError(f'{method} is not {inst.__class__.__name__}\'s instance method.')
                elif inspect.ismethod(method):
                    if not hasattr(method, '__org_name__'):
                        raise ValueError(f'{method} is not decorated by Postponer.enchance.')
                    real_method_name = real_attribute_name(inst, getattr(method, '__org_name__'))
                    if not hasattr(inst, real_method_name) or getattr(inst, real_method_name) != method:
                        raise ValueError(f'{method} is not {inst.__class__.__name__}\'s instance method.')
                else:
                    raise ValueError(f'{method} is not {inst.__class__.__name__}\'s instance method.')
                await stack.enter_async_context(Postponer.ContextManager(inst, self, real_method_name, always=always))
            return stack
        except:
            await stack.aclose()
            raise
    
            
    class ContextManager:
        inst: Any
        postponer: "Postponer"
        method: str
        always: bool
        
        def __init__(self, inst: Any, postponer: "Postponer", method: str, always: bool = True) -> None:
            self.inst = inst
            self.postponer = postponer
            self.method = method
            self.always = always
        
        async def __aenter__(self):
            Postponer.acquire_postponer(self.inst, self.method, self.postponer)
            return self.postponer
        
        async def __aexit__(self, exc_type, exc_value, exc_tb):
            Postponer.release_postponer(self.inst, self.method, self.postponer)
            if self.always or not exc_value:
                await self.postponer.future
            return False
                
    @staticmethod
    def get_postponers(inst, method_name: str, create: bool = False) -> list["Postponer"] | None:
        if hasattr(inst, '__postponers__'):
            postponers_map = cast(dict, getattr(inst, '__postponers__'))
        elif create:
            with Postponer.LOCK:
                if hasattr(inst, '__postponers__'):
                    postponers_map = getattr(inst, '__postponers__')
                else:
                    postponers_map = {}
                    setattr(inst, '__postponers__', postponers_map)
        else:
            return None
        postponers = postponers_map.get(method_name)
        if postponers:
            return postponers
        if create:
            with Postponer.LOCK:
                postponers = postponers_map.get(method_name)
                if not postponers:
                    postponers = postponers_map[method_name] = []
                return postponers
        else:
            return None
    
    @staticmethod
    def acquire_postponer(inst, method_name: str, postponer: "Postponer"):
        postponers = Postponer.get_postponers(inst, method_name, create=True)
        assert postponers is not None
        postponers.append(postponer)
        
    @staticmethod
    def release_postponer(inst, method_name: str, postponer: "Postponer"):
        postponers = Postponer.get_postponers(inst, method_name, create=False)
        assert postponers is not None
        postponers.remove(postponer)
    
    @staticmethod
    def enchance(method):
        if not inspect.iscoroutinefunction(method):
            raise ValueError(f'The Postponer.enchance must be decorated on the async method, but was decorated on method: {method.__qualname__}')
        if method.__qualname__.find('.') == -1:
            raise ValueError(f'The Postponer.enchance must be decorated on the instance method, but was decorated on method: {method.__qualname__}')
        async def wrapper(*args):
            self = args[0]
            postponers = Postponer.get_postponers(self, method.__name__,)
            if postponers:
                async def callback():
                    await method(*args)
                for postponer in postponers:
                    postponer._chain_task(callback=callback)
                return
            else:
                return await method(*args)
        wrapper.__org_name__ = method.__name__
        wrapper.__org_qualname__ = method.__qualname__
        return wrapper
            
        
                
async def create_token_from_owt_demo(url: str, username: str, role: str = 'presenter', room: str | None = None) -> str:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(verify_ssl=False)) as session:
        data = { 'room': room or '', 'user': username, 'role': role}
        async with session.post(f'{url}/tokens', headers={ 'Content-Type': 'application/json' }, data=json.dumps(data, separators=(',', ':'), ensure_ascii=True)) as r:
            return await r.text(encoding='utf-8')
        
        
if __name__ == '__main__':
    def t_f1(a):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f1))
    assert l.exception_type == BaseException
    assert l.exception_var_name == 'a'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid
    
    def t_f2(a, b):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f2))
    assert l.exception_type == BaseException
    assert l.exception_var_name == 'a'
    assert l.accept_collector
    assert l.collector_var_name == 'b'
    assert not l.reverse_order
    assert l.valid
    
    def t_f3(a: Exception, b: AsyncExceptionCollector):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f3))
    assert l.exception_type == Exception
    assert l.exception_var_name == 'a'
    assert l.accept_collector
    assert l.collector_var_name == 'b'
    assert not l.reverse_order
    assert l.valid
    
    def t_f4(a: AsyncExceptionCollector, b: Exception):
        pass

    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f4))
    assert l.exception_type == Exception
    assert l.exception_var_name == 'b'
    assert l.accept_collector
    assert l.collector_var_name == 'a'
    assert l.reverse_order
    assert l.valid
    
    def t_f5(a, b=1):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f5))
    assert l.exception_type == BaseException
    assert l.exception_var_name == 'a'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid
    
    def t_f6(collector):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f6))
    assert l.exception_type is None
    assert l.exception_var_name is None
    assert l.accept_collector
    assert l.collector_var_name == 'collector'
    assert l.valid
    
    def t_f7(*, collector, exception):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f7))
    assert l.exception_type == BaseException
    assert l.exception_var_name == 'exception'
    assert l.accept_collector
    assert l.collector_var_name == 'collector'
    assert l.valid
    
    def t_f8(*args, **kwargs):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f8))
    assert l.exception_type == BaseException
    assert l.exception_var_name is None
    assert l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid
    
    def t_f9(e: ValueError):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f9))
    assert l.exception_type == ValueError
    assert l.exception_var_name == 'e'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid
    
    def t_f10(e: "ValueError"):
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f10))
    assert l.exception_type == ValueError
    assert l.exception_var_name == 'e'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid

    class t_f11:
        def __call__(self, es: AsyncExceptionCollector) -> Any:
            pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f11()))
    assert l.exception_type is None
    assert l.exception_var_name is None
    assert l.accept_collector
    assert l.collector_var_name == 'es'
    assert l.valid
    
    class t_f12:
        def __call__(self, *args: Any, **kwds: Any) -> Any:
            pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f12()))
    assert l.exception_type == BaseException
    assert l.exception_var_name is None
    assert l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid

    class t_f13:
        def __call__(self, es: AsyncExceptionCollector, /, a=True) -> Any:
            pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f13()))
    assert l.exception_type is None
    assert l.exception_var_name is None
    assert l.accept_collector
    assert l.collector_var_name is None
    assert l.valid
    
    ET = TypeVar('ET', bound=Exception)
    
    class t_f14(Generic[ET]):
        def __call__(self, a: ET) -> Any:
            pass
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f14[ValueError]()))
    assert l.exception_type == Exception
    assert l.exception_var_name == 'a'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert l.valid
    
    def t_f15():
        pass
    
    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f15))
    assert l.exception_type is None
    assert l.exception_var_name is None
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid
    
    def t_f16(a: int):
        pass

    try:
        l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f16))
    except ValueError:
        l = None
    assert not l
    
    def t_f17(a: SyntaxError, b: int):
        pass

    try:
        l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f17))
    except ValueError:
        l = None
    assert not l
    
    def t_f18(a: SyntaxError, b: int=0):
        pass

    l = AsyncExceptionCollectorListenerWrapper(cast(AsyncExceptionCollectorListener, t_f18))
    assert l.exception_type is SyntaxError
    assert l.exception_var_name == 'a'
    assert not l.accept_collector
    assert l.collector_var_name is None
    assert not l.reverse_order
    assert l.valid