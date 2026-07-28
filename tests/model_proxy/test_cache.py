"""能力探测缓存单测:租户隔离 + TTL + singleflight + 主动失效。"""

import asyncio
import threading
import time

from ksadk.model_proxy.cache import CapabilityCache
from ksadk.model_proxy.detect import ModelCapabilities


def _caps(verdict, proto="chat"):
    c = ModelCapabilities(verdict=verdict, preferred_protocol=proto)
    c.responses_supported = verdict == "supported"
    return c


def test_credential_scope_isolates_same_model_base():
    cache = CapabilityCache(ttl=3600)
    cache.put("m", "https://x/v1", "key-A", _caps("supported", "responses"))
    cache.put("m", "https://x/v1", "key-B", _caps("unsupported", "chat"))
    # 同 model+base,不同 key -> 不同结论(租户隔离)
    assert cache.get("m", "https://x/v1", "key-A").preferred_protocol == "responses"
    assert cache.get("m", "https://x/v1", "key-B").preferred_protocol == "chat"


def test_ttl_expiry_returns_none():
    cache = CapabilityCache(ttl=0.05)
    cache.put("m", "https://x/v1", "k", _caps("supported"))
    assert cache.get("m", "https://x/v1", "k") is not None
    time.sleep(0.06)
    assert cache.get("m", "https://x/v1", "k") is None


def test_unknown_not_cached():
    cache = CapabilityCache(ttl=3600)
    cache.put("m", "https://x/v1", "k", _caps("unknown"))
    assert cache.get("m", "https://x/v1", "k") is None  # unknown 不缓存


def test_invalidate_and_clear():
    cache = CapabilityCache(ttl=3600)
    cache.put("m", "https://x/v1", "k", _caps("supported"))
    cache.invalidate("m", "https://x/v1", "k")
    assert cache.get("m", "https://x/v1", "k") is None
    cache.put("m", "https://x/v1", "k", _caps("supported"))
    cache.clear()
    assert cache.get("m", "https://x/v1", "k") is None


def test_singleflight_only_one_probe():
    cache = CapabilityCache(ttl=3600)
    probe_count = 0
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def probe(model, base, key):
        nonlocal probe_count
        with lock:
            probe_count += 1
        started.set()
        release.wait(timeout=2.0)  # 阻塞,让并发请求汇聚
        return _caps("supported", "responses")

    results = []

    def worker():
        results.append(cache.get_or_probe("m", "https://x/v1", "k", probe))

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    started.wait(timeout=2.0)
    release.set()
    for t in ts:
        t.join(timeout=3.0)
    # 4 个并发只探测 1 次(singleflight),都拿到 supported
    assert probe_count == 1
    assert all(r.preferred_protocol == "responses" for r in results)


def test_singleflight_async():
    cache = CapabilityCache(ttl=3600)
    probe_count = 0

    async def probe(model, base, key):
        nonlocal probe_count
        probe_count += 1
        await asyncio.sleep(0.05)
        return _caps("unsupported", "chat")

    async def worker():
        return await cache.aget_or_probe("m", "https://x/v1", "k", probe)

    async def main():
        results = await asyncio.gather(*[worker() for _ in range(5)])
        return results

    results = asyncio.run(main())
    assert probe_count == 1
    assert all(r.preferred_protocol == "chat" for r in results)
