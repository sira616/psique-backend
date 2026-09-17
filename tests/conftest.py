import json
import os
import shutil
import tempfile
import uuid

import pytest

# Debe fijarse antes de importar app.core.config / app.core.database.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
# Avatares y banners de los tests, nunca en el ./media del desarrollo.
MEDIA_DIR = tempfile.mkdtemp(prefix="psique-media-")
os.environ["MEDIA_DIR"] = MEDIA_DIR
os.environ["MEDIA_BASE_URL"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["JWT_SECRET"] = "secreto-solo-para-tests-con-longitud-de-sobra"
# El cliente es de sesión y varios módulos comparten /register: un límite bajo haría que
# un test le robara cupo a otro.
os.environ["RATE_LIMIT_MAX_REQUESTS"] = "10000"
os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
# Nunca un Ollama real, pase lo que pase en el .env de quien los ejecute.
os.environ["LLM_PROVIDER"] = "cloud"
# El TestClient habla http: una cookie Secure no volvería nunca al servidor.
os.environ["COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.llm import router as llm_router  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "ContrasenaLarga123"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
    engine.dispose()
    shutil.rmtree(MEDIA_DIR, ignore_errors=True)
    os.close(_db_fd)
    try:
        os.unlink(_db_path)
    except OSError:
        pass  # Windows a veces tarda en soltar el handle


def register(client: TestClient, username: str | None = None) -> dict:
    username = username or f"u{uuid.uuid4().hex[:12]}"
    resp = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()


def auth_headers(client: TestClient) -> dict:
    return {"Authorization": f"Bearer {register(client)['access_token']}"}


class FakeLLM:
    """Doble del router: respuesta de chat y JSON de extracción configurables."""

    def __init__(self):
        self.reply_chunks = ["*Sonríe.* ", "Me alegra que hayas vuelto. ", "¿Qué te trae por aquí?"]
        self.extraction: dict = {"hechos": {}, "senales": []}
        self.stream_error: Exception | None = None
        self.stream_calls: list[tuple[str, list]] = []
        self.generate_calls: list[tuple[str, list]] = []

    def stream(self, system_prompt, messages):
        self.stream_calls.append((system_prompt, messages))
        if self.stream_error:
            raise self.stream_error
        yield from self.reply_chunks

    def generate(self, system_prompt, messages, *, max_tokens=None):
        self.generate_calls.append((system_prompt, messages))
        return "Aquí va el JSON:\n" + json.dumps(self.extraction, ensure_ascii=False)


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_router, "stream", fake.stream)
    monkeypatch.setattr(llm_router, "generate", fake.generate)
    return fake


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events
