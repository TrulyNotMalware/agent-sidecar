import uvicorn

from .config import get_settings


def main() -> None:
    s = get_settings()
    uvicorn.run(
        "sidecar.app:create_app",
        factory=True,  # the app is built on demand: importing sidecar.app does nothing
        host=s.bind,
        port=s.port,
        log_level=s.log_level.lower(),
        # No uvicorn log config: its loggers propagate to the root handler that
        # create_app() installs, so their lines are JSON like the sidecar's own.
        log_config=None,
        # Streams get SHUTDOWN_GRACE_SEC (sse-starlette) to finish or end with
        # `error: cancelled`; uvicorn must not cut connections before that.
        timeout_graceful_shutdown=s.shutdown_grace_sec + 2,
    )


if __name__ == "__main__":
    main()
