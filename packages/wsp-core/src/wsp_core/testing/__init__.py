"""Contract test suites for ports (PLAN §12.2).

Each module here exposes a base test class for one port. An implementation's test file subclasses
it and provides a fixture that builds the implementation, e.g.::

    class TestMyLedger(LedgerContract):
        @pytest.fixture
        def ledger(self, tmp_path): return MyLedger(tmp_path)
"""
