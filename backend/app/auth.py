import os
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)


def get_edge_backend_secret() -> str:
    return os.environ.get("EDGE_BACKEND_SECRET", "")


def get_allowed_user_id() -> int:
    try:
        return int(os.environ.get("ALLOWED_USER_ID", "0"))
    except ValueError:
        return 0


def verify_bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> str:
    expected_secret = get_edge_backend_secret()
    
    # If a secret is configured, require and validate it
    if expected_secret:
        if not credentials or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if credentials.credentials != expected_secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid edge-backend authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials
    return ""


def verify_allowed_user(user_id: int) -> None:
    allowed_user_id = get_allowed_user_id()
    if allowed_user_id != 0 and user_id != allowed_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Disallowed user ID: {user_id}",
        )
