"""Local program/data upgrade transaction (UPGRADE-177).

One upgrade pairs one published program with one data generation, and the two cannot move in
one atomic step. The pieces are deliberately small and single-purpose:

    journal      the durable transaction record, its strict shape and its phase rules
    registry     which generations and program copies this installation's upgrades created
    program      published identities, the verified maintenance carrier, exact-set restore
    data         the data side, built only from the existing preserve/clone/recovery engines
    gate         who may open this root for business while a transaction is open
    verification the host's Windows-verification receipt a rollback must carry
    cleanup      D61/D60 cleanup of registered artifacts only
    emergency    the offline seal of a faulty installation and the way back from it
    service      the verbs, in the order a transaction may run them
    startup      what one program start does with an unfinished transaction

Invariants every module keeps:

* nothing is switched before the preservation, the carrier and the transaction are durable;
* the old selection is what ``installation_generation.load_selection`` really returned - it is
  not assumed to be the original generation;
* a clone's id is derived from the transaction id, so a crash right after the pointer move is
  attributable without scanning anything;
* data is only ever migrated inside a clone; the old generation is never written by the new
  program;
* a rollback needs a host-written Windows verification receipt, re-selects the recorded
  fallback with the existing recovery rules applied, and removes the newer build's extra files
  by comparing two verified published documents;
* the emergency way back is only taken when the new version's own backend cannot run: the whole
  installation is sealed offline first, the archive is fault material and never a claim that it
  can be imported again, and every later cleanup keeps the sealed generation, the sealed build
  and the archive itself;
* cleanup touches only registry entries that no unfinished transaction, no current selection,
  no retained previous version and no emergency archive refers to, and never removes a
  preservation copy, the manifest it is enumerated through, or the credential slot that
  decrypts it.

This package is backend only. The native host entry, the installer branch and any page route
are a later stage; see ``docs/QIBAN_R7_UPGRADE_TRANSACTION.md`` for the protocol.
"""
from __future__ import annotations

VERBS = ('prepare', 'confirm', 'rollback-prepare', 'rollback', 'emergency-seal',
         'emergency-rollback', 'cleanup', 'status', 'abort', 'startup-confirm')
PACKAGE = 'qiban-upgrade'
