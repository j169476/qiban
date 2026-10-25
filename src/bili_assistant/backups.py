"""Full backup compatibility with the Bilibili data profile."""
from companion_core import backups as core
from companion_core.backups import FORMAT, MANIFEST, MAX_FILE, MAX_TOTAL, sha

from .profiles import profile as data_profile
from .storage import Store


def allowed(name):
    return core.allowed(name, data_profile())

def create(store, destination):
    return core.create(store, destination)

def pause_database(path):
    return core.pause_database(path, data_profile())

def restore(archive, destination):
    return core.restore(archive, destination, profile=data_profile(), store_factory=Store, pause=pause_database)

def verify(archive, *, profile=None):
    return core.verify(archive, profile=profile or data_profile())

__all__ = ['FORMAT', 'MANIFEST', 'MAX_FILE', 'MAX_TOTAL', 'sha']
