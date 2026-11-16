"""Opt-in instrumentation of Google GenAI's public model methods.

Install once in each process. No prompts, responses, API keys or user IDs leave
this module. Streaming usage is cumulative: persist the last metadata once.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os

from .usage_client import record, _get

_context = ContextVar('ai_usage_context', default={})


@contextmanager
def usage_context(*, operation=None, kind=None, dataset=None):
    values = {**_context.get()}
    values.update({k: v for k, v in {'operation': operation, 'kind': kind, 'dataset': dataset}.items() if v is not None})
    token = _context.set(values)
    try:
        yield
    finally:
        _context.reset(token)


def _details(method, kwargs):
    config = kwargs.get('config')
    modalities = _get(config, 'response_modalities', 'responseModalities', []) or []
    modality = 'audio' if any(str(m).upper().endswith('AUDIO') for m in modalities) else 'unknown'
    if method == 'embed_content':
        modality = 'embedding'
    # Only classify text when no binary/URI parts are sent. Never serialize contents.
    if modality == 'unknown':
        contents = kwargs.get('contents')
        def text_only(value):
            if isinstance(value, str): return True
            if isinstance(value, (list, tuple)): return all(text_only(v) for v in value)
            parts = _get(value, 'parts')
            if parts is not None: return all(_get(p, 'text') is not None for p in parts)
            return False
        if text_only(contents): modality = 'text'
        else:
            def image_or_text(value):
                if isinstance(value, str): return True
                if isinstance(value, (list, tuple)): return all(image_or_text(v) for v in value)
                parts = _get(value, 'parts')
                if parts is not None: return all(image_or_text(p) for p in parts)
                if _get(value, 'text') is not None: return True
                data = _get(value, 'inline_data', 'inlineData') or _get(value, 'file_data', 'fileData')
                return bool(data and str(_get(data, 'mime_type', 'mimeType', '')).startswith('image/'))
            if image_or_text(contents): modality = 'image'
    return dict(model=kwargs.get('model', 'unknown'),
                kind=os.getenv('AI_USAGE_KIND', 'unknown'), operation=method,
                modality=modality, **{k:v for k,v in _context.get().items() if k == 'dataset'}) | {
                    k:v for k,v in _context.get().items() if k in {'kind','operation'}}


def _sync_wrapper(original, method):
    @wraps(original)
    def wrapped(self, *args, **kwargs):
        if not os.getenv('AI_USAGE_TOKEN'):
            return original(self, *args, **kwargs)
        details = _details(method, kwargs)
        try:
            result = original(self, *args, **kwargs)
        except Exception:
            record(status='error', **details)
            raise
        if method != 'generate_content_stream':
            record(result, **details)
            return result
        def stream():
            latest = None
            status = 'cancelled'
            try:
                for chunk in result:
                    if _get(chunk, 'usage_metadata', 'usageMetadata') is not None: latest = chunk
                    yield chunk
                status = 'ok'
            except Exception:
                status = 'error'
                raise
            finally:
                record(latest, status=status, **details)
                close = getattr(result, 'close', None)
                if close: close()
        return stream()
    return wrapped


def _async_wrapper(original, method):
    @wraps(original)
    async def wrapped(self, *args, **kwargs):
        if not os.getenv('AI_USAGE_TOKEN'):
            return await original(self, *args, **kwargs)
        details = _details(method, kwargs)
        try:
            result = await original(self, *args, **kwargs)
        except asyncio.CancelledError:
            await asyncio.to_thread(record, status='cancelled', **details)
            raise
        except Exception:
            await asyncio.to_thread(record, status='error', **details)
            raise
        if method != 'generate_content_stream':
            await asyncio.to_thread(record, result, **details)
            return result
        async def stream():
            latest = None
            status = 'cancelled'
            try:
                async for chunk in result:
                    if _get(chunk, 'usage_metadata', 'usageMetadata') is not None: latest = chunk
                    yield chunk
                status = 'ok'
            except Exception:
                status = 'error'
                raise
            finally:
                await asyncio.to_thread(record, latest, status=status, **details)
                close = getattr(result, 'aclose', None)
                if close: await close()
        return stream()
    return wrapped


def install():
    """Idempotent; optional instrumentation must never stop an app importing."""
    try:
        from google.genai.models import Models, AsyncModels
        for cls, factory in ((Models, _sync_wrapper), (AsyncModels, _async_wrapper)):
            for name in ('generate_content', 'generate_content_stream', 'embed_content'):
                original = getattr(cls, name, None)
                if original is None or getattr(original, '_ai_usage_wrapped', False): continue
                wrapped = factory(original, name)
                wrapped._ai_usage_wrapped = True
                setattr(cls, name, wrapped)
    except ImportError:
        pass


def dataset_usage(function):
    """Allowlist Politikk attribution; never emit arbitrary database filenames."""
    import inspect
    from pathlib import Path
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        path = bound.arguments.get('db_path')
        dataset = 'politikk' if path and Path(path).name in {'stortinget.db', 'unytt_stortinget.db'} else None
        with usage_context(dataset=dataset, operation=function.__name__):
            return function(*args, **kwargs)
    return wrapped
