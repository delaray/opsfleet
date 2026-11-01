"""Create ./secrets/ for compose.yaml from .env and gcloud Application Default Credentials.

Run once (and again after rotating a credential): uv run python -m scripts.make_docker_secrets

./secrets/ is ignored by Git and excluded from the Docker build context; its files are
mounted into containers at runtime as Compose secrets and never become image layers.
"""

import os
import shutil
from pathlib import Path

from dotenv import dotenv_values

SECRETS = Path('secrets')
NON_SECRET_SETTINGS = ('PROJECT_NAME', 'PROJECT_ID', 'DATASET_ID', 'GEMINI_MODEL_1', 'GEMINI_MODEL_2')


def adc_path() -> Path:
    config = os.getenv('CLOUDSDK_CONFIG') or (Path(os.environ['APPDATA']) / 'gcloud' if os.name == 'nt'
                                               else Path.home() / '.config' / 'gcloud')
    return Path(config) / 'application_default_credentials.json'


def write(name: str, value: str):
    path = SECRETS / name
    path.write_text(value, encoding='utf-8', newline='\n')
    path.chmod(0o600)


def main():
    env = dotenv_values('.env')
    missing = [key for key in ('GEMINI_API_KEY', 'PROJECT_ID', 'DATACHAT_DB_PASSWORD') if not env.get(key)]
    if missing:
        raise SystemExit(f'Missing in .env: {", ".join(missing)} (run scripts.setup_datachatdb first)')
    source = adc_path()
    if not source.is_file():
        raise SystemExit(f'No Application Default Credentials at {source}; run: gcloud auth application-default login')
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    write('gemini_api_key', env['GEMINI_API_KEY'])
    write('datachat_db_password', env['DATACHAT_DB_PASSWORD'])
    write('admindb_password', env.get('DATACHAT_DB_ADMIN_PASSWORD') or 'postgres')
    shutil.copyfile(source, SECRETS / 'gcp_adc.json')
    (SECRETS / 'gcp_adc.json').chmod(0o600)
    write('app.env', ''.join(f'{key}={env[key]}\n' for key in NON_SECRET_SETTINGS if env.get(key)))
    print(f'Wrote {", ".join(sorted(path.name for path in SECRETS.iterdir()))} to {SECRETS.resolve()}')


if __name__ == '__main__':
    main()
