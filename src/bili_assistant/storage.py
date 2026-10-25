"""Bilibili data profile over the shared core store; old public imports remain valid."""
from companion_core.storage import Store as CoreStore
from companion_core.storage import (
    StrictLoader,
    atomic_write,
    digest,
    recorded_time,
    search_terms,
    searchable_text,
    utcnow,
)

from .profiles import profile


class Store(CoreStore):
    def __init__(self, root):
        super().__init__(root, profile())

    def backup_service(self):
        from . import backups
        return backups

__all__ = ['StrictLoader', 'atomic_write', 'digest', 'recorded_time', 'searchable_text', 'search_terms', 'utcnow']
