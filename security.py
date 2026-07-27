"""Autenticación por API key en la cabecera X-API-Key."""
import hmac

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

import config

api_key_header = APIKeyHeader(name=config.API_KEY_NAME, auto_error=False)


def verificar_api_key(api_key: str = Depends(api_key_header)):
    """Valida que la API Key provista en la cabecera sea correcta."""
    # compare_digest evita filtrar la clave por diferencias de tiempo. El chequeo
    # previo de API_KEY_SECRETA impide que, si la variable de entorno falta, una
    # cabecera vacía sea aceptada como válida.
    if config.API_KEY_SECRETA and api_key and hmac.compare_digest(
        api_key, config.API_KEY_SECRETA
    ):
        return api_key
    raise HTTPException(
        status_code=403,
        detail="No autorizado. API Key inválida o ausente en la cabecera X-API-Key.",
    )
