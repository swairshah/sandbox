import os
from pathlib import Path

import modal

APP_NAME = "monios-api"
ROOT_DIR = Path(__file__).resolve().parent
CODE_PATH = "/code"
SECRET_NAME = os.environ.get("MONIOS_SECRET_NAME", "monios-secrets")

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "sess",
}

def _ignore(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


app = modal.App(APP_NAME)

requirements_path = ROOT_DIR / "requirements.txt"
if requirements_path.exists():
    base_image = modal.Image.debian_slim().pip_install_from_requirements(str(requirements_path))
else:
    print(f"[modal_app] WARNING: missing {requirements_path}, using fallback deps")
    base_image = modal.Image.debian_slim().pip_install(
        "fastapi==0.109.0",
        "uvicorn[standard]==0.27.0",
        "python-jose[cryptography]==3.3.0",
        "google-auth==2.27.0",
        "google-auth-oauthlib==1.2.0",
        "requests==2.31.0",
        "pydantic==2.5.3",
        "python-dotenv==1.0.0",
        "claude-agent-sdk",
        "watchdog==4.0.0",
        "aiofiles==23.2.1",
    )

image = base_image.pip_install("modal")
if modal.is_local():
    image = image.add_local_dir(str(ROOT_DIR), CODE_PATH, ignore=_ignore, copy=True)

secrets = [modal.Secret.from_name(SECRET_NAME)]


@app.function(
    image=image,
    secrets=secrets,
    env={
        "MODAL_ENVIRONMENT": "1",
        "IS_SANDBOX": "1",
        "MONIOS_SECRET_NAME": SECRET_NAME,
        "PYTHONPATH": CODE_PATH,
    },
)
@modal.asgi_app()
def fastapi_app():
    import modal as _modal

    import sandbox_manager as _sandbox_manager

    secret_name = os.environ.get("MONIOS_SECRET_NAME", "")

    sandbox_secrets = []
    if secret_name:
        sandbox_secrets.append(_modal.Secret.from_name(secret_name))

    _sandbox_manager.init(
        app=app,
        sandbox_image=image,
        secrets=sandbox_secrets,
        code_volume=None,
    )

    from main import app as fastapi_app

    return fastapi_app
