"""Configuración del servicio, leída de las variables de entorno que pone serverless.yml."""

import os

TENANTS = ("utec", "upc", "pucp")
ROLES = ("STUDENT", "TEACHER", "ADMIN")

# Cambia estos dos valores al copiar la plantilla (en este servicio: "ms-cursos" y "lms.courses").
SERVICIO = os.environ.get("SERVICIO", "ms-cursos")
SOURCE = os.environ.get("EVENT_SOURCE", "lms.courses")


def tabla_nombre() -> str:
    return os.environ.get("TABLA", "dev-t_cursos")


def bus_nombre() -> str:
    return os.environ.get("EVENT_BUS", "lms")


def auth_mode() -> str:
    """cognito: claims del authorizer de API Gateway · mock: token de demo del frontend."""
    return os.environ.get("AUTH_MODE", "cognito").lower()
