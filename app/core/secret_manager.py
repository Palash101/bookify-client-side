import os
import threading
from typing import Dict, Optional, Union

try:
    from google.cloud import secretmanager
except ModuleNotFoundError:  # pragma: no cover
    secretmanager = None

from app.core.settings import settings


class SecretManager:
    """
    Process-cached secret provider.
    """

    _instance: Optional["SecretManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "SecretManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_once()
        return cls._instance

    def _init_once(self) -> None:
        self._cache_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._client = None
        self._jwt_secret: Optional[str] = None
        self._tenant_db_secrets: Dict[str, str] = {}

    @staticmethod
    def _tenant_key(tenant_id: Union[str, "object"]) -> str:
        return str(tenant_id).strip()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._jwt_secret = None
            self._tenant_db_secrets.clear()

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    if secretmanager is None:
                        raise RuntimeError(
                            "google-cloud-secret-manager is required to use GCP Secret Manager."
                        )
                    # Uses default application credentials.
                    self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @staticmethod
    def _access_latest_secret(project_id: str, secret_id: str, client) -> str:
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")

    def get_jwt_secret(self) -> Optional[str]:
        """
        Non-tenant secret used to sign/verify JWTs in middleware.

        Lookup order:
        - GCP Secret Manager: bookify-dev-auth-secret (versions/latest) [requires GCP_PROJECT_ID]
        - SECRET_MANAGER__BOOKIFY_DEV_AUTH_SECRET (env fallback)
        - JWT_SECRET_KEY (env fallback)
        """
        with self._cache_lock:
            if self._jwt_secret:
                return self._jwt_secret

        secret: Optional[str] = None

        project_id = settings.GCP_PROJECT_ID
        if project_id:
            secret = self._access_latest_secret(
                project_id, settings.JWT_SIGNING_SECRET_ID, self._get_client()
            )
        if secret:
            secret = secret.strip()

        if secret:
            with self._cache_lock:
                self._jwt_secret = secret

        return secret

    def get_tenant_db_secret(self, tenant_id: Union[str, "object"]) -> Optional[str]:
        """
        Tenant-based secret used to create tenant DB sessions.

        Expected to be a DSN/URL or other DB-secret string per tenant.
        
        Lookup order:
        - GCP Secret Manager: template (versions/latest) [requires GCP_PROJECT_ID]
        - SECRET_MANAGER__TENANT_DB_SECRET__<TENANT_ID> (env fallback)
        - SECRET_MANAGER__TENANT_DB_SECRET (env fallback)
        """
        tenant_key = self._tenant_key(tenant_id)
        if not tenant_key:
            return None

        with self._cache_lock:
            cached = self._tenant_db_secrets.get(tenant_key)
            if cached:
                return cached

        secret: Optional[str] = None

        project_id = settings.GCP_PROJECT_ID
        if project_id:
            template = os.getenv("TENANT_DB_SECRET_NAME_TEMPLATE", "{tenant_id}-db-postgres")
            secret_id = template.format(tenant_id=tenant_key)
            secret = self._access_latest_secret(project_id, secret_id, self._get_client())
            print(secret,'secret')

        if not secret:
            secret = os.getenv(f"SECRET_MANAGER__TENANT_DB_SECRET__{tenant_key}") or os.getenv(
                "SECRET_MANAGER__TENANT_DB_SECRET"
            )
        if secret:
            secret = secret.strip()
            with self._cache_lock:
                self._tenant_db_secrets[tenant_key] = secret

        return secret


def get_secret_manager() -> SecretManager:
    return SecretManager()


def get_jwt_secret() -> Optional[str]:
    return get_secret_manager().get_jwt_secret()


def get_tenant_db_secret(tenant_id: Union[str, "object"]) -> Optional[str]:
    return get_secret_manager().get_tenant_db_secret(tenant_id)
