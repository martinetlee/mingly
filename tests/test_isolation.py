"""Guard: tests must never touch the user's real app data directory."""


def test_data_paths_redirected_to_tmp(_isolate_data_dir):
    import backend.config as config
    import backend.prefs as prefs
    import backend.secrets as secrets
    from backend.llm.config import _config_path

    for path in (prefs.PREFS_FILE, secrets._VAULT_FILE, config.DATA_DIR, _config_path()):
        assert str(path).startswith(str(_isolate_data_dir)), path
