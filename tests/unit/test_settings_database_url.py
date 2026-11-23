from pathlib import Path, PurePosixPath

from sqlalchemy.engine import make_url

from runway.config import Settings


def test_a_plain_data_directory_keeps_the_plain_sqlite_url(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")

    assert settings.database_url == f"sqlite:///{settings.database_path.as_posix()}"


def test_a_question_mark_in_the_path_is_not_read_as_a_url_query(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "what?mode=rw" / "data")

    url = make_url(settings.database_url)

    assert url.query == {"uri": "true"}
    assert url.database is not None
    assert url.database.startswith("file:")
    assert "what%3Fmode%3Drw" in url.database
    assert PurePosixPath(settings.database_path.as_posix()).name in url.database
