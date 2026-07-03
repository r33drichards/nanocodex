"""The full StateStore contract as a reusable unittest mixin — the Python
port of ``@copilotkit/bot``'s ``runStateStoreConformance``.

Run it against any backend before wiring the store into the bridge:

    import unittest
    from nanocodex_client.agui.state_store_conformance import StateStoreConformance

    class MyStoreConformance(StateStoreConformance, unittest.IsolatedAsyncioTestCase):
        def make_store(self):
            return MyDurableStore(...)

        async def teardown_store(self, store):  # optional
            await store.close()

The mixin deliberately has no TestCase base so it isn't collected on its
own; the concrete subclass supplies ``IsolatedAsyncioTestCase``.
"""

from __future__ import annotations

import asyncio

from .state_store import StateStore


class StateStoreConformance:
    def make_store(self) -> StateStore:
        raise NotImplementedError("subclass must build a fresh store per test")

    async def teardown_store(self, store: StateStore) -> None:
        pass

    async def asyncSetUp(self):
        self.store = self.make_store()
        if asyncio.iscoroutine(self.store):
            self.store = await self.store

    async def asyncTearDown(self):
        await self.teardown_store(self.store)

    # kv ------------------------------------------------------------------

    async def test_kv_set_get_delete_round_trips(self):
        await self.store.kv.set("a", {"n": 1})
        self.assertEqual(await self.store.kv.get("a"), {"n": 1})
        await self.store.kv.delete("a")
        self.assertIsNone(await self.store.kv.get("a"))

    async def test_kv_missing_key_is_none(self):
        self.assertIsNone(await self.store.kv.get("nope"))

    async def test_kv_expires_after_ttl(self):
        await self.store.kv.set("t", 1, 30)
        await asyncio.sleep(0.06)
        self.assertIsNone(await self.store.kv.get("t"))

    # list ----------------------------------------------------------------

    async def test_list_appends_oldest_first_and_ranges(self):
        for v in ("a", "b", "c"):
            await self.store.list.append("L", v)
        self.assertEqual(await self.store.list.range("L"), ["a", "b", "c"])
        self.assertEqual(await self.store.list.range("L", 0, 1), ["a", "b"])

    async def test_list_caps_with_max_len_on_append(self):
        for v in ("a", "b", "c", "d"):
            await self.store.list.append("C", v, max_len=2)
        self.assertEqual(await self.store.list.range("C"), ["c", "d"])

    async def test_list_trim_keeps_newest(self):
        for v in ("a", "b", "c"):
            await self.store.list.append("T", v)
        await self.store.list.trim("T", 2)
        self.assertEqual(await self.store.list.range("T"), ["b", "c"])

    async def test_list_delete_clears(self):
        await self.store.list.append("D", "x")
        await self.store.list.delete("D")
        self.assertEqual(await self.store.list.range("D"), [])

    async def test_list_mixed_ttl_append_keeps_whole_list_expiry(self):
        await self.store.list.append("M", "a", ttl_ms=1000)
        await self.store.list.append("M", "b")  # no ttl: must not clobber expiry
        await asyncio.sleep(0.04)
        self.assertEqual(len(await self.store.list.range("M")), 2)

    # lock ----------------------------------------------------------------

    async def test_lock_blocks_second_acquire_until_release(self):
        a = await self.store.lock.acquire("k")
        self.assertIsNotNone(a)
        self.assertIsNone(await self.store.lock.acquire("k"))
        await self.store.lock.release("k", a)
        self.assertIsNotNone(await self.store.lock.acquire("k"))

    async def test_lock_stale_token_release_does_not_free_reacquired(self):
        a = await self.store.lock.acquire("k", ttl_ms=20)
        await asyncio.sleep(0.04)  # a expires
        b = await self.store.lock.acquire("k")
        self.assertIsNotNone(b)
        await self.store.lock.release("k", a)  # stale — must NOT release b
        self.assertIsNone(await self.store.lock.acquire("k"))
        await self.store.lock.release("k", b)

    # dedup ---------------------------------------------------------------

    async def test_dedup_first_false_second_true_within_ttl(self):
        self.assertFalse(await self.store.dedup.seen("e1", 1000))
        self.assertTrue(await self.store.dedup.seen("e1", 1000))

    async def test_dedup_forgets_after_ttl(self):
        self.assertFalse(await self.store.dedup.seen("e2", 30))
        await asyncio.sleep(0.06)
        self.assertFalse(await self.store.dedup.seen("e2", 30))

    # queue ---------------------------------------------------------------

    async def test_queue_fifo(self):
        await self.store.queue.enqueue("q", 1)
        await self.store.queue.enqueue("q", 2)
        self.assertEqual(await self.store.queue.depth("q"), 2)
        self.assertEqual(await self.store.queue.dequeue("q"), 1)
        self.assertEqual(await self.store.queue.dequeue("q"), 2)
        self.assertIsNone(await self.store.queue.dequeue("q"))

    async def test_queue_max_size_drop_oldest_evicts_head(self):
        for v in (1, 2, 3):
            await self.store.queue.enqueue("q2", v, max_size=2, on_full="drop-oldest")
        self.assertEqual(await self.store.queue.dequeue("q2"), 2)
        self.assertEqual(await self.store.queue.dequeue("q2"), 3)

    async def test_queue_max_size_drop_newest_rejects_incoming(self):
        for v in (1, 2, 3):
            await self.store.queue.enqueue("q3", v, max_size=2, on_full="drop-newest")
        self.assertEqual(await self.store.queue.dequeue("q3"), 1)
        self.assertEqual(await self.store.queue.dequeue("q3"), 2)

    # cross-namespace ------------------------------------------------------

    async def test_kv_and_lock_keyspaces_do_not_collide(self):
        await self.store.kv.set("x", {"v": 1})
        a = await self.store.lock.acquire("x")
        self.assertIsNotNone(a)
        self.assertEqual(await self.store.kv.get("x"), {"v": 1})
        await self.store.lock.release("x", a)
