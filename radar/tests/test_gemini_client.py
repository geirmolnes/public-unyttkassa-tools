"""The shared Gemini client under the thread pool.

`classify_pending` and `_coverage_by_model` both run at CLASSIFY_WORKERS, and
the client is built lazily on first use — so the first call of a fresh process
is always a race between eight threads. It used to lose that race in a way that
produced one failed article per container start.
"""

import gc
import threading
import time

import google.genai as genai_mod
import httpx
import pytest
from google.genai import _api_client as genai_api_client
from google.genai import errors as genai_errors

from radar import config, gemini

WORKERS = 8
FAKE_KEY = "x" * 39


@pytest.fixture
def fresh_client(monkeypatch):
    """A module with no client yet, and a key that never leaves the process."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(gemini, "_client", None)
    yield
    gemini._client = None


def race(target, workers=WORKERS):
    """Run `target(i)` on `workers` threads released at the same instant."""
    barrier = threading.Barrier(workers)

    def run(i):
        barrier.wait()
        target(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_only_one_client_is_ever_built(fresh_client, monkeypatch):
    """Eight workers, one client.

    Every extra client is an orphan the moment the next thread overwrites the
    global, and `genai.Client.__del__` closes its httpx transport on collection.
    """
    built = []
    lock = threading.Lock()
    real = genai_mod.Client

    def counting(*args, **kwargs):
        with lock:
            built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(genai_mod, "Client", counting)
    race(lambda i: gemini.client())

    assert len(built) == 1


def test_the_transport_survives_a_call_in_flight(fresh_client):
    """The failure this reproduces: «Cannot send a request, as the client has been closed».

    `generate_json` evaluates `client().models`, and `Models` holds only the
    BaseApiClient — never the `Client` wrapper. An orphaned wrapper collected
    mid-call therefore closes the transport underneath a request that is already
    on its way.
    """
    closed = []

    def worker(i):
        models = gemini.client().models  # what generate_json evaluates
        time.sleep(0.02)  # stand-in for the HTTP round trip
        gc.collect()
        if models._api_client._httpx_client.is_closed:
            closed.append(i)

    race(worker)

    assert closed == []


def test_every_worker_gets_the_same_client(fresh_client):
    seen = []
    race(lambda i: seen.append(id(gemini.client())))
    assert len(set(seen)) == 1


def test_transient_transport_errors_are_retried_in_the_call(fresh_client):
    """2026-11-12: one TLS handshake failure (an httpx.ConnectError) failed an
    article, and the admin hub paged for a tick the next one healed."""
    options = gemini.client()._api_client._http_options.retry_options
    assert options is not None and options.attempts >= 2
    retry = genai_api_client.retry_args(options)["retry"]
    assert retry.predicate(httpx.ConnectError("[SSL: TLSV1_ALERT_DECODE_ERROR] decode error"))
    assert retry.predicate(genai_errors.APIError(503, {"error": {"status": "UNAVAILABLE"}}))


def test_a_missing_key_still_raises(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(gemini, "_client", None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        gemini.client()
    gemini._client = None


def test_dropping_the_wrapper_closes_the_transport():
    """The upstream behaviour the lock exists to avoid tripping over.

    Pinned so that if google-genai ever stops closing on __del__, the reason
    for the lock is still on record rather than looking like superstition.
    """
    c = genai_mod.Client(api_key=FAKE_KEY)
    api = c.models._api_client
    assert api._httpx_client.is_closed is False
    del c
    gc.collect()
    assert api._httpx_client.is_closed is True


# --- the thinking-config probe ------------------------------------------------


@pytest.fixture
def at_variant_zero(monkeypatch):
    monkeypatch.setattr(gemini, "_variant", 0)
    yield
    gemini._variant = 0


def test_concurrent_rejections_advance_the_probe_once(at_variant_zero, monkeypatch):
    """Eight workers hitting the same rejection must not skip past the variant
    that works — unguarded, each one advanced it."""
    calls = []
    lock = threading.Lock()

    class Boom(Exception):
        pass

    def exploding(*args, **kwargs):
        with lock:
            calls.append(gemini._variant)
        # Fail only while the probe is on the first variant, so the recursion
        # terminates once it has advanced exactly one step.
        if gemini._variant == 0:
            raise Boom("400 INVALID_ARGUMENT")
        return type("R", (), {"text": "{}", "usage_metadata": None})()

    monkeypatch.setattr(
        gemini,
        "client",
        lambda: type(
            "C", (), {"models": type("M", (), {"generate_content": staticmethod(exploding)})()}
        )(),
    )
    race(lambda i: gemini.generate_json("s", {}, "c"))

    assert gemini._variant == 1
