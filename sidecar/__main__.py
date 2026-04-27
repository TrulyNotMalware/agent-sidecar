import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "sidecar.app:app",
        host=s.bind,
        port=s.port,
        log_level=s.log_level.lower(),
        timeout_graceful_shutdown=s.shutdown_grace_sec,
    )


if __name__ == "__main__":
    main()
