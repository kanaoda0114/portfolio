"""テスト全体で使う準備（pytest が自動で読み込む）。"""
import pytest

from services import db as dbm
from tests.helpers import load_samples


@pytest.fixture
def sample_db(tmp_path):
    """架空のマスタと売上データが入った、テスト専用のデータベース。"""
    con = dbm.connect(str(tmp_path / "returns.db"))
    load_samples(con)
    yield con
    con.close()
