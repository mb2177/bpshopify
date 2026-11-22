import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["FAKE_LLM"] = "1"
os.environ["ADMIN_EMAIL"] = "owner@test.local"
os.environ["ADMIN_PASSWORD"] = "secret123"
os.environ["SECRET_KEY"] = "test"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
