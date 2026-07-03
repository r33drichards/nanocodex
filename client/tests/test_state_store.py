"""MemoryStore must pass the full StateStore conformance contract (the same
contract a BYO Redis/Postgres/SQLite store should be verified against)."""

import unittest

from nanocodex_client.agui.state_store import MemoryStore
from nanocodex_client.agui.state_store_conformance import StateStoreConformance


class MemoryStoreConformance(StateStoreConformance, unittest.IsolatedAsyncioTestCase):
    def make_store(self):
        return MemoryStore()


if __name__ == "__main__":
    unittest.main()
