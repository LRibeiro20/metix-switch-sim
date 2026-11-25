from __future__ import annotations

import os

import uvicorn

from cbs.config import get_cbs_settings
from shared.pki import build_server_mtls_uvicorn_kwargs, ensure_pki_bundle


def main() -> None:
    cfg = get_cbs_settings()
    ensure_pki_bundle(cfg.certs_dir)
    port = int(os.getenv("CBS_PORT", "8001"))
    ssl_kwargs: dict[str, object] = {}
    if cfg.mtls_enabled:
        ssl_kwargs = build_server_mtls_uvicorn_kwargs(cfg.certs_dir, cfg.service_id)
    uvicorn.run("cbs.main:app", host="0.0.0.0", port=port, **ssl_kwargs)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
