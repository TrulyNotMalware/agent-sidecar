import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "sidecar.app:app",
        host=s.bind,
        port=s.port,
        log_level=s.log_level.lower(),
        # Streams get SHUTDOWN_GRACE_SEC (sse-starlette) to finish or end with
        # `error: cancelled`; uvicorn must not cut connections before that.
        timeout_graceful_shutdown=s.shutdown_grace_sec + 2,
    )


if __name__ == "__main__":
    main()
