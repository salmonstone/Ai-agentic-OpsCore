# Pull secrets from the OS keychain into the environment before anything
# (agent.config's Settings, load_dotenv) reads configuration. Never raises:
# a missing or broken keychain falls back to .env exactly as before.
from agent.core.secrets import load_into_environ as _load_secrets

_load_secrets()
