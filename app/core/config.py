from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Por defecto production, que es el valor seguro: un despliegue al que se le olvide
    # definirlo no expone /docs.
    ENVIRONMENT: str = "production"

    DATABASE_URL: str = "sqlite:///./psique.db"

    # --- Autenticación local ---
    # Sin secreto no se arranca: generarlo al vuelo cerraría todas las sesiones en cada
    # reinicio. Generar uno:  python -c "import secrets; print(secrets.token_urlsafe(48))"
    JWT_SECRET: str = ""
    ACCESS_TOKEN_MINUTES: int = 30
    REFRESH_TOKEN_MINUTES: int = 60 * 24 * 30
    # None = Secure solo en producción. En local se sirve por http y el navegador
    # descartaría una cookie Secure.
    COOKIE_SECURE: bool | None = None

    # --- LLM ---
    # "cloud" (Claude) o "local" (Ollama). Son conversaciones íntimas: el modo local existe
    # para quien no quiera mandarlas fuera de su máquina.
    LLM_PROVIDER: str = "cloud"
    ANTHROPIC_API_KEY: str = ""
    LLM_CLOUD_MODEL: str = "claude-sonnet-5"
    # Respuestas de 1-3 párrafos; el margen evita cortar una frase a la mitad.
    LLM_MAX_TOKENS: int = 2048
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"
    OLLAMA_API_KEY: str = ""
    OLLAMA_KEEP_ALIVE: str = "10m"

    # --- Contexto de la historia ---
    # Mensajes literales que ve el modelo; lo anterior entra solo como resumen.
    CONTEXT_MESSAGES: int = 20

    ALLOWED_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]

    # --- Imágenes de perfil ---
    # Fuera del repo en producción. Lo que hay dentro lo sirve /media tal cual: solo se
    # escriben ficheros ya re-codificados por `app.services.media_service`.
    MEDIA_DIR: str = "./media"
    # Prefijo de las URLs de imágenes. Vacío = relativas ("/media/..."), para un frontend
    # que proxya /media igual que /api; con otro dominio, p. ej. "https://api.psique.app".
    MEDIA_BASE_URL: str = ""
    AVATAR_MAX_BYTES: int = 2 * 1024 * 1024
    BANNER_MAX_BYTES: int = 4 * 1024 * 1024

    # --- Economía (óbolos) ---
    # Los fija el servidor: el cliente nunca manda un importe, solo pide acciones.
    WELCOME_OBOLOS: int = 5
    SCRATCH_DAILY_CARDS: int = 3
    SCRATCH_PRIZE: int = 5
    SCRATCH_WINNING_NUMBER: int = 7
    CHAPTER_COST: int = 2
    # Empezar un libro que ya leíste (o cuya primera lectura no es gratis) y releerlo.
    READ_COST: int = 3
    # El "día" del rasca y gana es el de España, no el UTC del servidor: si no, el cupo
    # se renovaría a la 1 o las 2 de la madrugada.
    ECONOMY_TIMEZONE: str = "Europe/Madrid"

    RATE_LIMIT_MAX_REQUESTS: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

# --- Fail-fast de configuración ---------------------------------------------------
# Preferimos no arrancar y decir qué falta a un 500 opaco en la primera petición.

IS_PRODUCTION = settings.ENVIRONMENT.strip().lower() == "production"

if not settings.JWT_SECRET.strip():
    raise RuntimeError(
        "Falta JWT_SECRET. Genera uno con:  "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\"  y ponlo en el .env."
    )

if len(settings.JWT_SECRET.strip()) < 32:
    raise RuntimeError("JWT_SECRET es demasiado corto: hacen falta al menos 32 caracteres.")

# Con allow_credentials=True un comodín anularía el control de qué frontend llama a la API.
if "*" in settings.ALLOWED_ORIGINS:
    raise RuntimeError("ALLOWED_ORIGINS no puede contener '*': hace falta una lista explícita.")
