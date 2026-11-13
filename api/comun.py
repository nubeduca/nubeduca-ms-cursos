"""Utilidades de las rutas de ms-cursos: ids, lectura del curso, permisos y paginación."""

import base64
import binascii
import json
import re

from lib import http
from lib import tabla as t

COURSE_ID = re.compile(r"^crs_[0-9A-HJKMNP-TV-Z]{26}$")
TERM_ID = re.compile(r"^trm_[0-9A-HJKMNP-TV-Z]{26}$")
LIMITE_DEFECTO, LIMITE_MAXIMO = 20, 100


def sk_curso(course_id: str) -> str:
    return f"COURSE#{course_id}"


def sk_codigo(term_id: str, code: str) -> str:
    return f"COURSECODE#{term_id}#{code}"


def sk_seccion(course_id: str, section_id: str = "") -> str:
    return f"SECTION#{course_id}#{section_id}"


def sk_periodo(term_id: str) -> str:
    return f"PROY#TERM#{term_id}"


def sk_matricula(course_id: str, user_id: str) -> str:
    """Reservado para ACA-04: existe si el usuario está matriculado en el curso."""
    return f"ENROLL#{course_id}#{user_id}"


def a_curso(item: dict) -> dict:
    curso = {k: item[k] for k in ("courseId", "termId", "code", "title", "status", "ownerId", "createdAt")}
    if item.get("description") is not None:
        curso["description"] = item["description"]
    return curso


def no_encontrado(event: dict) -> dict:
    return http.error(event, 404, "CURSO_NO_ENCONTRADO", "No existe el curso en este tenant.")


def leer_curso(usuario, course_id: str, consistente: bool = False) -> dict | None:
    if not COURSE_ID.match(course_id or ""):
        return None
    clave = {"tenant_id": usuario.tenant_id, "sk": sk_curso(course_id)}
    return t.tabla().get_item(Key=clave, ConsistentRead=consistente).get("Item")


def matriculado(usuario, course_id: str) -> bool:
    clave = {"tenant_id": usuario.tenant_id, "sk": sk_matricula(course_id, usuario.user_id)}
    return t.tabla().get_item(Key=clave).get("Item") is not None


def puede_ver(usuario, curso: dict) -> bool:
    if usuario.rol == "ADMIN" or curso["ownerId"] == usuario.user_id:
        return True
    if usuario.rol == "STUDENT":
        return curso["status"] == "PUBLISHED" and matriculado(usuario, curso["courseId"])
    return False


def puede_editar(usuario, curso: dict) -> bool:
    return usuario.rol == "ADMIN" or (usuario.rol == "TEACHER" and curso["ownerId"] == usuario.user_id)


def acceso_de_lectura(event, usuario, curso) -> dict | None:
    """Respuesta de error si el usuario no puede ver el curso; None si puede.

    Al alumno sin acceso se le responde 404 (no revela que el curso existe); al docente
    que no es dueño, 403 (el recurso es de otro usuario).
    """
    if curso is None:
        return no_encontrado(event)
    if puede_ver(usuario, curso):
        return None
    return no_encontrado(event) if usuario.rol == "STUDENT" else http.sin_permiso(event)


def course_id_de(event: dict) -> str:
    return ((event or {}).get("pathParameters") or {}).get("courseId") or ""


def limite(params: dict) -> int | None:
    try:
        valor = int(params.get("limit", LIMITE_DEFECTO))
    except (TypeError, ValueError):
        return None
    return valor if 1 <= valor <= LIMITE_MAXIMO else None


def codificar(clave: dict | None) -> str | None:
    if not clave:
        return None
    return base64.urlsafe_b64encode(json.dumps(clave, separators=(",", ":")).encode()).decode()


def decodificar(token: str, tenant_id: str, prefijo: str) -> dict | None:
    try:
        clave = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    valido = (isinstance(clave, dict) and set(clave) == {"tenant_id", "sk"} and clave["tenant_id"] == tenant_id
              and isinstance(clave["sk"], str) and clave["sk"].startswith(prefijo))
    return clave if valido else None
