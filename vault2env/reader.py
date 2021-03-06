import logging
from http import HTTPStatus
from typing import Dict, List, Optional, Tuple

import hvac
import hvac.exceptions
import requests

from vault2env.auth import Auth
from vault2env.exception import AuthenticationError, TypeError, UnsupportedError

logger = logging.getLogger(__name__)


class KVReader:
    """Read secrets from Vault KV engine."""

    def __init__(self, url: str, auth: Auth) -> None:
        """
        Parameters
        ----------
        url : str
            Full URL to Vault.
        auth : Auth
            Authentication method and credentials.
        """
        if not isinstance(url, str):
            raise TypeError("Expect str for url, got {}", type(url).__name__)
        if not isinstance(auth, Auth):
            raise TypeError(
                "Expect Auth instance for auth, got {}", type(auth).__name__
            )

        self.url = url
        self.auth = auth

        self._client: Optional[hvac.Client] = None

    @property
    def client(self) -> hvac.Client:
        """Returns a Vault client."""
        if self._client:
            return self._client

        logger.debug(
            "Vault client initialization requested. URL= %s, Auth type= %s",
            self.url,
            self.auth.method,
        )

        client = hvac.Client(self.url)

        self.auth.apply(client)
        if not client.is_authenticated():
            raise AuthenticationError("Authentication failed")

        self._client = client
        return client

    def get_engine_and_version(self, path: str) -> Tuple[Optional[str], Optional[int]]:
        """Query for the KV engine mount point and version of a secret.

        Parameters
        ----------
        path : str
            Path to the secret

        Returns
        -------
        mount_point : str
            The path the secret engine mounted on.
        version : int
            The secret engine version

        See also
        --------
        consul-template implementation
            https://github.com/hashicorp/consul-template/blob/9b0db8d7e76ee01ecde4db53fbd5c11f7eb9a6a8/dependency/vault_common.go#L294-L357
        """
        try:
            resp = self.client.adapter.get(
                f"/v1/sys/internal/ui/mounts/{path}", raise_exception=False
            )
        except requests.RequestException:
            logger.exception("Error occurs during checking engine metadata")
            return None, None

        if isinstance(resp, dict):
            data = resp.get("data", {})

            mount_point = data.get("path")
            type_ = data.get("type")
            version = data.get("options", {}).get("version")

            if version == "2" and type_ == "kv":
                return mount_point, 2
            elif version == "1":
                return mount_point, 1
            else:
                logging.error("Unknown version %s for path %s", version, path)
                logging.debug("Raw response: %s", resp)
                return None, None

        assert isinstance(resp, requests.Response)
        if resp.status_code == HTTPStatus.NOT_FOUND:
            # 404 is expected on an older version of vault, default to version 1
            # https://github.com/hashicorp/consul-template/blob/9b0db8d7e76ee01ecde4db53fbd5c11f7eb9a6a8/dependency/vault_common.go#L310-L311
            return "", 1
        elif resp.status_code == HTTPStatus.FORBIDDEN:
            logger.error("The used token has no access to path %s", path)
            return None, None

        logger.error("Error occurs during checking engine metadata for %s", path)
        logger.debug("Raw response: %s", resp.text)
        return None, None

    def get_secrets(self, path: str) -> Optional[dict]:
        """Query for a secret set.

        Parameters
        ----------
        path: str
            Path to the secret.

        Returns
        -------
        secrets : dict
            The secret set if matched, or None when data not found.
        """
        if not isinstance(path, str):
            raise TypeError("Expect str for path, got {}", type(path).__name__)

        mount_point, version = self.get_engine_and_version(path)
        if not mount_point:
            return None

        logger.debug("Secret %s is mounted at %s (kv%d)", path, mount_point, version)
        secret_path = removeprefix(path, mount_point)

        # there's separated API endpoints for different versioned KV engine,
        # but they shares a same function signature
        if version == 1:
            query_func = self.client.secrets.kv.v1.read_secret
        elif version == 2:
            query_func = self.client.secrets.kv.v2.read_secret
        else:
            raise UnsupportedError("Unknown engine version {}", version)

        try:
            resp = query_func(secret_path, mount_point)
        except requests.RequestException:
            logger.exception("Error occurs during query secret %s", path)
            return None
        except hvac.exceptions.InvalidPath:
            logger.error("Secret not found: %s", path)
            return None
        except hvac.exceptions.VaultError as e:
            logger.error(
                "Error during query secret '%s': %s",
                path,
                e.args[0],
            )
            return None

        assert isinstance(resp, dict)
        if version == 1:
            return resp["data"]
        elif version == 2:
            return resp["data"]["data"]

    def get_value(self, path: str, key: str) -> Optional[str]:
        """Get one value in a secret set.

        This function does not cache the secret set. Use `get_values` for
        getting multiple values efficiently.

        Parameters
        ----------
        path: str
            Path to the secret.
        key: str
            Key to the desired value.

        Returns
        -------
        value : str
            The secret value if matched, or None when value not found.
        """
        if not isinstance(key, str):
            raise TypeError("Expect str for key, got {}", type(key).__name__)

        secret_set = self.get_secrets(path)
        if not secret_set:
            return None

        value = get_value(secret_set, key)
        logger.debug(
            "Query for %s#%s %s.",
            path,
            key,
            "succeed" if value is not None else "failed",
        )

        return value

    def get_values(self, pairs: List[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
        """Get multiple secret values.

        Parameters
        ----------
        pairs : List[Tuple[str,str]]
            Pairs of secret path and key.

        Returns
        -------
        values : Dict[Tuple[str,str], str]
            The secret values. The dictionary key is the given secret path and
            secret key pair, and its value is the secret value. The value could
            be none on query error.
        """
        outputs = {}

        cache = {}
        for secret_path, secret_key in pairs:
            # get secret set
            secret_set = cache.get(secret_path)
            if secret_set is None:
                secret_set = self.get_secrets(secret_path)
                cache[secret_path] = secret_set

            # handle secret set not exists or empty
            if not secret_set:
                outputs[secret_path, secret_key] = None

            # get value
            outputs[secret_path, secret_key] = get_value(secret_set, secret_key)

        logger.debug(
            "Query for %d values, %d succeed.",
            len(pairs),
            sum(1 for value in outputs.values() if value is not None),
        )
        return outputs


def split_key(key: str) -> List[str]:
    """Splits the given key string into subsequences for getting the value. It
    supports nested structure by joining the keys with dots, e.g. `aa.bb`.

    For a path with special symbols, user must use quote symbols to wrap that
    name, e.g. `"aa.bb".cc`. If a quote symbol is intended, user must use a
    backslash to escape.

    Parameters
    ----------
    key : str
        Key to get the value.

    Returns
    -------
    path : List[str]
        Resolved path
    """
    output = []
    quote_stack = []
    escape = False
    buffer = ""

    for c in key:
        if c == "\\":
            escape = True
            continue

        if c == "." and not quote_stack:
            output.append(buffer)
            buffer = ""
            continue

        if c in "'\"" and not escape:
            if quote_stack and quote_stack[-1] == c:
                # closing quote
                quote_stack.pop()
            else:
                # opening quote
                quote_stack.append(c)
            continue

        buffer += c
        escape = False

    if buffer or output:
        output.append(buffer)

    return output


def get_value(data: dict, key: str) -> Optional[str]:
    """Traverse the data dict to get the value along with the given key."""
    if not isinstance(key, str):
        raise TypeError("Expect str for key, got {}", type(key).__name__)

    for k in split_key(key):
        if not isinstance(data, dict):
            logger.warning("Key %s not exists", key)
            return None
        data = data.get(k)

    if not isinstance(data, str):
        logger.warning("Key %s is not pointing to a value", key)
        return None

    return data


def removeprefix(s: str, prefix: str) -> str:
    """Remove the prefix if it exists"""
    if s.startswith(prefix):
        return s[len(prefix) :]
    return s
