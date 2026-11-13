"""Which generation an installer and a worker read; file state only, no database or vault.

These checks pin the wiring that keeps a portable restore from falling back to the original
(empty) generation: the installer derives its database, store and credential slot from
``installation_generation.load_selection``, and the worker refuses a configuration that does
not name the generation its installation root selected.
"""
import base64
import json
import sys
from pathlib import Path

import pytest

# The probe scripts are executed from their directory, not installed as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe'))

import installation  # noqa: E402
import installation_generation  # noqa: E402
import storage_worker  # noqa: E402

from companion_core.errors import AssistantError  # noqa: E402

INSTALLATION = '0123456789abcdef0123456789abcdef'
GENERATION = 'fedcba9876543210fedcba9876543210'
PORT = 54329
ORIGINAL = {'installation': INSTALLATION, 'generation': None}


class Vault:
    """Stand-in for the Windows credential store; records every identifier it is asked for."""

    def __init__(self, **slots):
        self.slots, self.reads = dict(slots), []

    def get_password(self, service, identifier):
        self.reads.append((service, identifier))
        return self.slots.get(identifier)

    def set_password(self, service, identifier, value):
        self.slots[identifier] = value


def slot(key, password):
    return json.dumps({'data_key': base64.b64encode(key).decode('ascii'),
                       'database_password': password})


@pytest.fixture
def root(tmp_path):
    """A ready installation whose original generation still owns <root>/store."""
    directory = tmp_path / 'installation'
    (directory / 'store').mkdir(parents=True)
    (directory / 'database').mkdir()
    (directory / 'installation.json').write_text(json.dumps(
        {'format': 1, 'installation': INSTALLATION, 'owner': 'a' * 48, 'port': PORT,
         'phase': 'ready'}), encoding='utf8')
    return directory.resolve()


def restored(root, pointer=True):
    """Layout and register a restored generation; selecting it is the commit point."""
    directory = root / 'generations' / GENERATION
    (directory / 'store').mkdir(parents=True)
    installation_generation.prepare_selection(root, INSTALLATION, GENERATION)
    if pointer:
        installation_generation.commit_selection(root, INSTALLATION, GENERATION, ORIGINAL)
    return directory


def state(root):
    return json.loads((root / 'installation.json').read_text(encoding='utf8'))


def test_original_generation_keeps_the_installers_own_addresses(root):
    settings = installation.generation_settings(root, INSTALLATION)
    assert settings == {'generation': None, 'database': 'qiban', 'data': root / 'store',
                        'credential_id': INSTALLATION, 'manifest_sha256': None}
    assert installation.original_settings(root, INSTALLATION) == settings
    assert not (root / 'active-generation.json').exists()


def test_selected_generation_moves_database_store_and_credential_together(root):
    directory = restored(root)
    settings = installation.generation_settings(root, INSTALLATION)
    assert settings['generation'] == GENERATION
    assert settings['database'] == 'qiban_restore_' + GENERATION
    assert settings['data'] == directory / 'store'
    assert settings['credential_id'] == GENERATION
    assert settings['manifest_sha256'] == installation_generation.load_selection(
        root, INSTALLATION)['manifest_sha256']
    # The original generation's own address is untouched and still on disk.
    assert (root / 'store').is_dir()


def test_a_prepared_but_uncommitted_generation_is_not_selected(root):
    restored(root, pointer=False)
    settings = installation.generation_settings(root, INSTALLATION)
    assert settings['generation'] is None and settings['database'] == 'qiban'
    assert settings['data'] == root / 'store'


def test_a_broken_selection_is_refused_instead_of_selecting_the_original(root):
    directory = restored(root)
    (directory / 'generation.json').unlink()
    with pytest.raises(AssistantError) as missing:
        installation.generation_settings(root, INSTALLATION)
    assert missing.value.code == 'generation_manifest_missing'
    (root / 'active-generation.json').write_text('{', encoding='utf8')
    with pytest.raises(AssistantError) as unreadable:
        installation.generation_settings(root, INSTALLATION)
    assert unreadable.value.code == 'generation_pointer_invalid'


def test_a_ready_installation_without_its_store_is_not_treated_as_fresh(root):
    (root / 'store').rmdir()
    with pytest.raises(AssistantError) as error:
        installation.generation_settings(root, INSTALLATION)
    assert error.value.code == 'generation_store_missing'


def test_storage_configuration_records_the_selected_generation_only(root):
    directory = restored(root)
    settings = installation.generation_settings(root, INSTALLATION)
    worker = Path('C:/app/resources/storage/Qiban.Storage.exe')
    config = installation.storage_configuration(root, state(root), settings, worker)
    assert config == {'installation': INSTALLATION, 'credential_id': GENERATION,
                      'data': str(directory / 'store'), 'desktop_management': True,
                      'qq_runtime': str(worker.parent.parent / 'qq'),
                      'database': {'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime',
                                   'dbname': 'qiban_restore_' + GENERATION}}
    original = installation.storage_configuration(
        root, state(root), installation.original_settings(root, INSTALLATION), worker)
    # The install root and the platform account directories never move with the generation.
    assert original['qq_runtime'] == config['qq_runtime']
    assert original['credential_id'] == INSTALLATION and original['database']['dbname'] == 'qiban'
    assert original['data'] == str(root / 'store')


def test_the_original_generation_key_is_read_from_the_installation_slot(root):
    key, password = bytes(range(32)), 'r' * 40
    vault = Vault(**{INSTALLATION: slot(key, password)})
    settings = installation.original_settings(root, INSTALLATION)
    assert installation.selected_credential(vault, INSTALLATION, settings, key, password) == (key, password)
    assert vault.reads == []


def test_a_restored_generation_keeps_its_own_key_and_the_cluster_password(root):
    base, key, password = bytes(range(32)), bytes(range(32, 64)), 'r' * 40
    vault = Vault(**{GENERATION: slot(key, password)})
    restored(root)
    settings = installation.generation_settings(root, INSTALLATION)
    assert installation.selected_credential(vault, INSTALLATION, settings, base, password) == (key, password)
    assert vault.reads == [(installation.VAULT, GENERATION)]
    for broken, expected in ((slot(key, 'x' * 40), 'storage_vault_invalid'),
                             (slot(bytes(31), password), 'storage_vault_invalid'),
                             ('{', 'storage_vault_invalid')):
        vault.slots[GENERATION] = broken
        with pytest.raises(AssistantError) as refused:
            installation.selected_credential(vault, INSTALLATION, settings, base, password)
        assert refused.value.code == expected
    vault.slots.pop(GENERATION)
    with pytest.raises(AssistantError) as absent:
        installation.selected_credential(vault, INSTALLATION, settings, base, password)
    assert absent.value.code == 'storage_vault_missing'


def test_credentials_only_accept_exact_identifiers_without_touching_the_vault():
    for identifier, keyref in (('installation', None), (INSTALLATION.upper(), None),
                               ('f' * 31, None), (INSTALLATION, '..'), (INSTALLATION, GENERATION + 'a')):
        with pytest.raises(AssistantError) as refused:
            storage_worker.credentials(identifier, keyref)
        assert refused.value.code == 'storage_config'


def config(root, **overrides):
    base = {'installation': INSTALLATION, 'data': str(root / 'store'),
            'desktop_management': True, 'qq_runtime': str(root / 'qq'),
            'database': {'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime', 'dbname': 'qiban'}}
    value = {**base, **overrides}
    path = root / 'written-storage-config.json'
    path.write_text(json.dumps(value), encoding='utf8')
    return path


def test_the_hand_written_format_is_not_held_to_the_installed_login(tmp_path):
    directory = tmp_path / 'hand-written-probe'
    directory.mkdir()
    path = config(directory.resolve(), database={'host': '127.0.0.1', 'port': PORT,
                                                 'user': 'qiban_probe', 'dbname': 'companion_test'})
    spec = storage_worker.config_spec(path)
    assert spec['root'] is None and spec['connection']['user'] == 'qiban_probe'
    with pytest.raises(AssistantError) as refused:
        storage_worker.portable_host(object(), path, 'application-session')
    assert refused.value.code == 'portable_unavailable'


@pytest.mark.parametrize('pointer, expected', [
    (None, False),
    ({'format': 1, 'installation': INSTALLATION, 'generation': None, 'manifest_sha256': None}, False),
    ({'format': 1, 'installation': INSTALLATION, 'generation': GENERATION,
      'manifest_sha256': 'a' * 64}, True),
    ('{', True),
])
def test_the_worker_only_keeps_writing_when_the_commit_point_names_the_original(root, pointer, expected):
    if pointer is not None:
        (root / 'active-generation.json').write_text(
            pointer if isinstance(pointer, str) else json.dumps(pointer), encoding='utf8')
    path = config(root)
    assert storage_worker.portable_switch_applied(path) is expected


def test_worker_switch_detection_compares_against_its_opened_restored_generation(root):
    directory = restored(root)
    path = config(root, credential_id=GENERATION, data=str(directory/'store'),
                  database={'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime',
                            'dbname': 'qiban_restore_' + GENERATION})
    assert storage_worker.portable_switch_applied(path) is False
    (root/'active-generation.json').unlink()
    assert storage_worker.portable_switch_applied(path) is True


def test_a_hand_written_test_configuration_without_a_root_journal_is_still_readable(tmp_path):
    directory = tmp_path / 'hand-written'
    directory.mkdir()
    path = config(directory.resolve())
    spec = storage_worker.config_spec(path)
    assert spec['root'] is None and spec['selection'] is None
    assert spec['installation'] == INSTALLATION and spec['keyref'] == INSTALLATION
    assert spec['data'] == directory.resolve() / 'store'
    # It may not claim a generation that no trusted root record backs.
    with pytest.raises(AssistantError) as refused:
        storage_worker.config_spec(config(directory.resolve(), credential_id=GENERATION))
    assert refused.value.code == 'storage_config'


def test_the_worker_reads_the_selected_generation_not_the_original_database(root):
    directory = restored(root)
    spec = storage_worker.config_spec(config(root, credential_id=GENERATION,
                                            data=str(directory / 'store'),
                                            database={'host': '127.0.0.1', 'port': PORT,
                                                      'user': 'qiban_runtime',
                                                      'dbname': 'qiban_restore_' + GENERATION}))
    assert spec['root'] == root and spec['selection']['generation'] == GENERATION
    assert spec['installation'] == INSTALLATION and spec['keyref'] == GENERATION
    assert spec['data'] == directory / 'store'
    assert spec['connection']['dbname'] == 'qiban_restore_' + GENERATION
    # An older configuration that still names the original database is refused, not opened.
    with pytest.raises(AssistantError) as stale:
        storage_worker.config_spec(config(root, credential_id=INSTALLATION,
                                         data=str(directory / 'store')))
    assert stale.value.code == 'storage_config'


def test_the_worker_refuses_configurations_that_disagree_with_the_root(root):
    restored(root)
    selected = {'credential_id': GENERATION, 'data': str(root / 'generations' / GENERATION / 'store'),
                'database': {'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime',
                             'dbname': 'qiban_restore_' + GENERATION}}
    with pytest.raises(AssistantError) as wrong_data:
        storage_worker.config_spec(config(root, **{**selected, 'data': str(root / 'store')}))
    assert wrong_data.value.code == 'storage_config'
    with pytest.raises(AssistantError) as wrong_name:
        storage_worker.config_spec(config(root, **{**selected, 'database': {
            'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime', 'dbname': 'qiban'}}))
    assert wrong_name.value.code == 'storage_config'
    with pytest.raises(AssistantError) as wrong_slot:
        storage_worker.config_spec(config(root, **{**selected, 'credential_id': INSTALLATION}))
    assert wrong_slot.value.code == 'storage_config'
    with pytest.raises(AssistantError) as wrong_port:
        storage_worker.config_spec(config(root, **{**selected, 'database': {
            'host': '127.0.0.1', 'port': PORT + 1, 'user': 'qiban_runtime',
            'dbname': 'qiban_restore_' + GENERATION}}))
    assert wrong_port.value.code == 'storage_config'
    with pytest.raises(AssistantError) as wrong_user:
        storage_worker.config_spec(config(root, **{**selected, 'database': {
            'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_setup',
            'dbname': 'qiban_restore_' + GENERATION}}))
    assert wrong_user.value.code == 'storage_config'
    (root / 'installation.json').write_text(json.dumps({'format': 1, 'port': PORT, 'phase': 'ready'}),
                                            encoding='utf8')
    with pytest.raises(AssistantError) as no_identity:
        storage_worker.config_spec(config(root, **selected))
    assert no_identity.value.code == 'storage_config'
    (root / 'installation.json').write_text(json.dumps({'format': 1, 'installation': INSTALLATION,
                                                        'port': PORT, 'phase': 'schema'}),
                                            encoding='utf8')
    with pytest.raises(AssistantError) as not_ready:
        storage_worker.config_spec(config(root, **selected))
    assert not_ready.value.code == 'storage_config'


@pytest.mark.parametrize('changes', [
    {'installation': 'installation'}, {'data': 'store'}, {'database': {'host': '0.0.0.0', 'port': PORT,
                                                                     'user': 'qiban_runtime', 'dbname': 'qiban'}},
    {'database': {'host': '127.0.0.1', 'port': '5432', 'user': 'qiban_runtime', 'dbname': 'qiban'}},
    {'database': {'host': '127.0.0.1', 'port': 80, 'user': 'qiban_runtime', 'dbname': 'qiban'}},
    {'database': {'host': '127.0.0.1', 'port': PORT, 'user': 'qiban_runtime'}},
])
def test_explicit_configuration_fields_are_validated(root, changes):
    with pytest.raises(AssistantError) as refused:
        storage_worker.config_spec(config(root, **changes))
    assert refused.value.code == 'storage_config'
