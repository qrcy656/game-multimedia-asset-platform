"""Community configuration; model access lives in the shared registry."""
from dataclasses import dataclass
import os
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("ANIMATION_DATABASE_URL", os.getenv("DATABASE_URL", "postgresql://animation:animation@postgres:5432/animation"))
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    data_root: Path = Path(os.getenv("ANIMATION_DATA_DIR", "/data/animation"))
    identity_secret: str = os.getenv("COMMUNITY_IDENTITY_SECRET", "")
    identity_issuer: str = "fantasy-main"
    identity_audience: str = "animation-studio"
    app_origin: str = os.getenv("APP_ORIGIN", "http://localhost:18082")
    ffmpeg: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe: str = os.getenv("FFPROBE_BIN", "ffprobe")
    poll_seconds: float = float(os.getenv("ANIMATION_POLL_SECONDS", "5"))
    max_poll_seconds: int = int(os.getenv("ANIMATION_MAX_POLL_SECONDS", "7200"))
    upload_max_bytes: int = int(os.getenv("UPLOAD_MAX_BYTES", str(200 * 1024 * 1024)))

settings = Settings()
