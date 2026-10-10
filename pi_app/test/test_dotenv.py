import os

from pi_app.dotenv import load_secrets_env


def test_loads_keys_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_KEY", raising=False)
    path = tmp_path / "secrets.env"
    path.write_text("# a comment\nSOME_TEST_KEY=hello\n\nANOTHER_KEY=world\n")

    load_secrets_env(str(path))

    assert os.environ["SOME_TEST_KEY"] == "hello"
    assert os.environ["ANOTHER_KEY"] == "world"


def test_real_environment_variable_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TEST_KEY", "from-shell")
    path = tmp_path / "secrets.env"
    path.write_text("SOME_TEST_KEY=from-file\n")

    load_secrets_env(str(path))

    assert os.environ["SOME_TEST_KEY"] == "from-shell"


def test_missing_file_is_a_noop(tmp_path):
    load_secrets_env(str(tmp_path / "does_not_exist.env"))  # should not raise
