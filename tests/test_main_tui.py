from pathlib import Path

from app.agent.utils import get_agent_db_path


def test_agent_db_path_resolves_under_resource_dir():
    db_path = get_agent_db_path()

    assert db_path.is_absolute()
    assert db_path.parent.name == "resource"
    assert db_path.name == "agent.db"
    assert db_path.parent.exists()
