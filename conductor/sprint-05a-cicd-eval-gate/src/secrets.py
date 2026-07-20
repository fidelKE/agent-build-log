"""
Credential injection layer for Conductor.

The model never receives secrets. The harness fetches credentials here,
at tool execution time, and injects them directly into the outbound call.
The model only sees tool names and parameters - never the auth material.

Two implementations share the same protocol:
  - LocalStubSecretStore: reads from environment variables (CI, dev, no Podman)
  - VaultSecretStore: reads from HashiCorp Vault dev mode (production-equivalent)

Switching is a single constructor change in agent.py. No other code changes.
"""

import os
import re
import logging
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

# Regex that matches common credential shapes for log redaction.
# Covers Bearer tokens, UUIDs, and long alphanumeric strings (32+ chars).
_CREDENTIAL_PATTERN = re.compile(
    r"(Bearer\s+\S+|[A-Za-z0-9_\-]{32,})"
)


def redact(text: str, placeholder: str = "[REDACTED]") -> str:
    """Scrub credential-shaped strings from any text before it hits a log."""
    return _CREDENTIAL_PATTERN.sub(placeholder, text)


class RedactingFormatter(logging.Formatter):
    """Log formatter that scrubs credentials from every message."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact(formatted)


def configure_redacting_logger(name: str) -> logging.Logger:
    """Return a logger whose output is scrubbed through RedactingFormatter."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
    return log


@runtime_checkable
class SecretStore(Protocol):
    """Protocol satisfied by both stub and Vault implementations."""

    def get(self, key: str) -> str:
        """Fetch a secret by key. Raises KeyError if not found."""
        ...

    def available(self) -> bool:
        """Return True if the backing store is reachable."""
        ...


class LocalStubSecretStore:
    """
    Reads secrets from environment variables.

    Key mapping: secret key → env var name (uppercased, hyphens → underscores).
    Example: 'catalog-api-token' → env var 'CATALOG_API_TOKEN'

    Used in CI and local dev when Podman/Vault is not available.
    Falls back automatically when VaultSecretStore.available() returns False.
    """

    def get(self, key: str) -> str:
        env_var = key.upper().replace("-", "_")
        value = os.environ.get(env_var)
        if value is None:
            raise KeyError(
                f"Secret '{key}' not found. Expected env var: {env_var}"
            )
        return value

    def available(self) -> bool:
        return True


class VaultSecretStore:
    """
    Reads secrets from HashiCorp Vault KV v2 (dev mode via Podman).

    Dev mode Vault: no unsealing, root token is 'dev-root-token', runs on
    localhost:8200. All secrets stored under 'secret/conductor/<scope>/<key>'.

    scope enforces per-agent identity: Setup and Troubleshooting modes get
    separate Vault paths so cross-mode credential access fails at fetch time.

    In production, swap the address and token source - no other code changes.
    """

    def __init__(
        self,
        address: str = "http://localhost:8200",
        token: str = "dev-root-token",
        mount: str = "secret",
        path_prefix: str = "conductor",
        scope: str = "troubleshooting",
    ):
        self._address = address
        self._token = token
        self._mount = mount
        self._path_prefix = path_prefix
        self._scope = scope
        self._headers = {"X-Vault-Token": token}

    def get(self, key: str) -> str:
        url = f"{self._address}/v1/{self._mount}/data/{self._path_prefix}/{self._scope}/{key}"
        try:
            response = httpx.get(url, headers=self._headers, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            value = data["data"]["data"]["value"].strip()
            if not value:
                raise KeyError(
                    f"Vault: secret '{key}' is empty (scope={self._scope}) — "
                    f"check seed script (vault kv get secret/conductor/{self._scope}/{key})"
                )
            return value
        except httpx.HTTPStatusError as e:
            raise KeyError(
                f"Vault: secret '{key}' not found (scope={self._scope}, status={e.response.status_code})"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Vault unreachable at {self._address}: {e}") from e

    def available(self) -> bool:
        try:
            response = httpx.get(
                f"{self._address}/v1/sys/health",
                headers=self._headers,
                timeout=2.0,
            )
            return response.status_code in (200, 429, 472, 473)
        except httpx.RequestError:
            return False


def make_secret_store(prefer_vault: bool = True, scope: str = "troubleshooting") -> SecretStore:
    """
    Factory that returns VaultSecretStore when Vault is reachable,
    LocalStubSecretStore otherwise. This is the single switch point.

    scope controls the Vault path prefix, enforcing per-agent identity.
    Set prefer_vault=False in tests that don't spin up Podman.
    """
    if prefer_vault:
        vault = VaultSecretStore(scope=scope)
        if vault.available():
            logger.info("SecretStore: using Vault at %s (scope=%s)", vault._address, scope)
            return vault
        logger.warning("Vault not reachable - falling back to LocalStubSecretStore")
    return LocalStubSecretStore()
