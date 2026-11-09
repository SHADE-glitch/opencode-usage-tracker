"""The family contract is one document in four repos.

CI checks out a single repository, so no test may read a sibling repo's path.
This file pins the sha256 of *this* repo's copy of ``family_contract.json``
and its ``contract_version``; a copy edited in only one repo fails here.

Mirrors the guard in ``test_cross_repo_golden.py``: one fixed file, one digest,
one constant per repo. ``FAMILY_CONTRACT.md`` in all-usage-trackers is the
canonical text — follow its "Changing this contract" steps to bump the version
and refresh every digest in the same change.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

try:
    import _hermetic  # noqa: F401 -- cbut/qcut install the isolation tripwire
except ImportError:
    pass  # ocut (autouse conftest fixture) and aut have no such module

HERE = Path(__file__).resolve().parent
CONTRACT_PATH = HERE / "fixtures" / "family_contract.json"
CANONICAL_SHA256 = "a74ce7f0973481db470b3ab806f24592e93aa3895921f57c923be6f57e24d97d"
EXPECTED_VERSION = 1


def _load():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


class FamilyContractVersionTest(unittest.TestCase):
    def test_contract_version_is_pinned(self):
        self.assertEqual(
            _load()["contract_version"], EXPECTED_VERSION,
            "family_contract.json changed version without updating every repo's "
            "EXPECTED_VERSION in the same change")

    def test_the_copy_is_the_canonical_one(self):
        actual = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            actual, CANONICAL_SHA256,
            "this repo's copy of family_contract.json is not the canonical one. "
            "Edit the canonical copy in all-usage-trackers, re-copy it into the "
            "other three repos, and update CANONICAL_SHA256 in all four tests "
            "in the same change")


if __name__ == "__main__":
    unittest.main()
