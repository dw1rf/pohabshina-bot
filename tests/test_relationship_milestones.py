import sqlite3

from cogs.weddings import backup_weddings_before_gameplay_migration, eligible_relationship_milestones


def test_relationship_milestones_unlock_by_xp_and_days() -> None:
    assert eligible_relationship_milestones(99, 6) == []
    assert eligible_relationship_milestones(100, 7) == ["days_7", "xp_100"]
    assert eligible_relationship_milestones(1600, 40) == [
        "days_7",
        "days_30",
        "xp_100",
        "xp_500",
        "xp_1500",
    ]


def test_weddings_database_is_backed_up_before_gameplay_migration(tmp_path) -> None:
    path = tmp_path / "weddings.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE wedding_marriages(id INTEGER PRIMARY KEY, proposer_id INTEGER)")
        db.execute("INSERT INTO wedding_marriages VALUES (1, 42)")
        db.commit()

    backup = backup_weddings_before_gameplay_migration(path)
    assert backup is not None and backup.exists()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT proposer_id FROM wedding_marriages WHERE id=1").fetchone()[0] == 42
