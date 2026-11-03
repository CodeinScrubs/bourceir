"""Pinned, narrow compatibility for the lossless CODAL storage migration.

Legacy studies still revalidate stored input hashes and recompute every financial
fact. Only the reviewed storage-only implementation transition may differ.
"""

import hashlib

from config import settings

LEGACY = {
    'ops/codal_documents.py': '4f9c8966a0675438310db15c53faaaa935c852be95a0ddf60ec263b4d825280c',
    'ops/capital_actions.py': '271cb7b2a953ad05659dbf9c27e8a700fac5346293661b0e18e15998747257c6',
    'ops/rights_registration.py': 'bdf6e4bdc9dcf6ec3979dcabf9d720f0ef85c800947e25315e5a952b0a1c9727',
}
CURRENT = {
    'ops/codal_documents.py': 'a55ef01bc2e246398edfff3ee5d8830033e317294f52fcfaf9ffa4b22448ed70',
    'ops/capital_actions.py': 'ac23ec376179fd447fd1e8a8b2a77d8ab8bb8ef6a6b228fc3967be4851ce0762',
    'ops/rights_registration.py': '6d0c14ddb3d6894fd5a391481bdaa727300e3d536b2e429ab7de3b72a0a9348e',
}
COMPACT_SHA256 = '85753605c0a48827cc87c9688d814650780b0f0e7ca892cb4084864b41f8e8cd'


def semantic_source_compatible(name, recorded, current):
    """Accept exact matches or one fully pinned, previously reviewed IO transition."""
    if recorded == current:
        return True
    if name not in LEGACY or recorded != LEGACY[name] or current != CURRENT.get(name):
        return False
    compact = settings.BASE_DIR/'ops/codal_compact.py'
    return hashlib.sha256(compact.read_bytes().replace(b'\r\n', b'\n')).hexdigest() == COMPACT_SHA256
