# Psique — backend

[![CI](https://github.com/sira616/psique-backend/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sira616/psique-backend/actions/workflows/ci.yml)

API de historias románticas interactivas **para todos los públicos**: el personaje
conversa, y cuando una escena se acerca a lo íntimo, se funde a negro.

FastAPI + SQLAlchemy + SQLite + Alembic, con Claude (`claude-sonnet-5` por defecto) u
Ollama como LLM.

## Principio de diseño

**El código decide el estado; el LLM solo escribe.** El modelo no fija nunca la fase de
la historia ni la afinidad. Lo único que aporta al estado es una lista de señales de un
conjunto cerrado y unos hechos sobre la persona, y ambos pasan por Pydantic y por una
lista blanca antes de guardarse. Pesos, umbrales y transiciones son reglas explícitas
en `app/story/state_machine.py`, y cada transición guarda una razón legible.

## Arranque (Windows, PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Rellena JWT_SECRET (y ANTHROPIC_API_KEY si quieres respuestas reales):
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
.\venv\Scripts\python.exe -m scripts.dev --port 8010
```

`scripts/dev.py` arranca uvicorn y lo reinicia al cambiar `app/`, `migrations/`, `.env` o
`alembic.ini`. No uses `uvicorn --reload` en Windows: su reloader para el proceso con un
Ctrl+C que se pierde si no hay consola compartida (lanzado desde la app de escritorio, un
IDE o en segundo plano) y se queda en "Reloading..." sirviendo el código viejo.

Las migraciones se aplican solas al arrancar. Sin `ANTHROPIC_API_KEY` el modo cloud
responde con un texto de demo, útil para probar el flujo completo sin gastar.

Tests (sin red ni claves, LLM doblado):

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

En cada push y PR a `main`, GitHub Actions (`.github/workflows/ci.yml`) pasa pytest y
comprueba que `alembic upgrade head` construye una SQLite vacía y que `alembic check` no
encuentra cambios de modelos sin migración.

## API

Todo cuelga de `/api` (el frontend proxya `/api` al puerto 8000).

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/auth/register`, `/login`, `/refresh`, `/logout` | Auth local (Argon2id + JWT con refresh rotatorio). El registro exige `accept_terms` y `min_age_confirmed` |
| GET | `/api/me` | Cuenta actual (incluye `termsVersion` vigente y `termsAccepted`) |
| POST | `/api/me/accept-terms` | Acepta la versión vigente de términos y privacidad (`{version, confirm: true}`) |
| GET | `/api/me/incidents` | Partidas cerradas por la política y estado de su apelación |
| POST | `/api/me/incidents/{id}/appeal` | Apela un cierre, una vez (`{text?}`, ≤500, pasa el filtro de entrada) |
| GET | `/api/dev/incidents`, `/api/dev/incidents/{id}` | Cola de moderación (solo dev): filtros, detalle con extracto |
| POST | `/api/dev/incidents/{id}/accept`, `/reject` | Resuelve (`{note?}`); aceptar reabre y recalcula la restricción |
| GET / PATCH | `/api/me/profile` | Perfil propio (handle, bio, enlace, visibilidad de estanterías) |
| POST / DELETE | `/api/me/avatar`, `/api/me/banner` | Imagen de perfil (multipart `file`), servida en `/media/...` |
| GET | `/api/profiles/{handle}` | Perfil público con estanterías "Publicadas" y "Leyendo" |
| GET | `/api/explore` | Historias públicas de todas las cuentas (`limit`, `offset`, `mode`) |
| PATCH | `/api/custom-stories/{id}` | Publicar o despublicar (`{isPublic}`) |
| GET | `/api/characters` | Personajes disponibles |
| GET / POST | `/api/stories` | Listar / empezar una historia (`{characterId}`) |
| GET | `/api/stories/{id}` | Mensajes, estado y memoria |
| POST | `/api/stories/{id}/chat` | Turno por SSE: `{message}` o `{choiceId}` |

Eventos SSE (`event: <nombre>` + `data: <json>`):

- `token {text}` — trozo de respuesta ya revisado por el guardrail.
- `state {phase, phaseLabel, affinity, turnCount, quickChoices, transition, signals}`.
- `error {message}` — p. ej. LLM no disponible; el mensaje del usuario queda guardado.
- `done {}`.

Las *quick choices* salen de reglas por fase; el cliente solo manda el `id` y el
servidor pone el texto y el peso.

## Estructura

```
app/
  core/        config, BD, Argon2, JWT, rate limit, cabeceras, migraciones
  llm/         router (cloud/local, historial multi-turno), prompts/story.py
  story/
    state_machine.py     fases, afinidad desde eventos, transiciones, quick choices
    character_profile.py perfiles JSON validados (extra=forbid, frozen, solo adultos)
    characters/*.json    Lucía (Valencia) y Mateo (Donostia)
    memory.py            extractor LLM → JSON → Pydantic → lista blanca → MemoryFact
    guardrail.py         fundido a negro, cuarta pared, datos sensibles (por frases)
    context.py           ventana de N mensajes + resumen rodante (stub)
  services/    auth, historias, turno de chat en streaming
  api/         auth, me, stories
migrations/    Alembic (render_as_batch para SQLite)
```

## Cambiar un modelo

```powershell
.\venv\Scripts\python.exe -m alembic revision --autogenerate -m "qué cambia"
```

Revisa el fichero generado: autogenerate no adivina renombrados. Un test falla si hay
modelo sin migración.

## Pendiente

- **Resumen rodante**: `app/story/context.py::rolling_summary` es un stub que recorta
  mensajes antiguos. Sustituir por un resumen generado por el LLM cada K turnos.
- **RAG** (lore de personajes, recuerdos largos): no incluido para no arrastrar
  `sentence-transformers`. Punto de partida: `platano-backend/app/rag/`
  (chunking, embeddings, retriever).
- **Guardrail**: es de patrones; valorar un clasificador para eufemismos y paráfrasis.
  También falta un filtro de entrada (hoy la entrada solo influye vía señales).
- **Caché de prompt**: el system prompt ya va ordenado de estable a variable; falta
  partirlo en bloques con `cache_control`.
- Borrado de cuenta e historias (`DELETE`), exportación de datos.
- El rate limiter vive en memoria de un proceso.
