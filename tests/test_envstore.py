import stat

import pytest

from dreamina.envstore import EnvStore, account_env_key, read_env_file


def test_round_trip_preserves_unrelated_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep me\nOTHER=1\nDREAMINA_EMAIL=old@example.com\n")
    store = EnvStore(path)
    store.set_many({"DREAMINA_EMAIL": "new@example.com", "NEW_KEY": 'has space "quote"'})
    text = path.read_text()
    assert text.startswith("# keep me\nOTHER=1\n")
    assert read_env_file(path) == {
        "OTHER": "1",
        "DREAMINA_EMAIL": "new@example.com",
        "NEW_KEY": 'has space "quote"',
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_remove_and_restore(tmp_path):
    path = tmp_path / ".env"
    store = EnvStore(path)
    store.set_account_credentials("main-2", "a@example.com", "secret#1 x")
    snapshot = store.snapshot()
    assert store.account_credentials("main-2") == ("a@example.com", "secret#1 x")
    store.remove_account_credentials("main-2")
    assert store.account_credentials("main-2") == (None, None)
    store.restore(snapshot)
    assert store.account_ids() == ["main-2"]


def test_failed_write_leaves_previous_content(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    store = EnvStore(path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("dreamina.envstore.os.replace", boom)
    with pytest.raises(OSError):
        store.set_many({"A": "2"})
    assert path.read_text() == "A=1\n"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".env.") and p.suffix != ".lock"]


def test_legacy_credentials_map_to_default(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DREAMINA_EMAIL=me@example.com\nDREAMINA_PASSWORD=pw\n")
    store = EnvStore(path)
    assert store.account_ids() == ["default"]
    assert store.account_credentials("default") == ("me@example.com", "pw")


def test_account_env_key():
    assert account_env_key("main-2") == "MAIN_2"
    with pytest.raises(ValueError):
        account_env_key("---")
