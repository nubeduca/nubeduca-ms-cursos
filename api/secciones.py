"""
Secciones de un curso (ACA-03).

GET  /courses/{courseId}/sections  (quien puede ver el curso)
POST /courses/{courseId}/sections  (TEACHER dueño, ADMIN)   409 si el curso está archivado

Ítem: SECTION#<courseId>#<sectionId>. sectionId es un ULID: el orden por sk es el de creación.
"""

from boto3.dynamodb.conditions import Key

from api import comun
from lib import auth, http, ids, outbox
from lib import tabla as t


def crear(event, usuario, curso):
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    cuerpo = http.leer_json(event)
    nombre = cuerpo.get("name") if isinstance(cuerpo, dict) else None
    if not isinstance(nombre, str) or not nombre.strip() or len(nombre.strip()) > 40:
        return http.datos_invalidos(event, "name es obligatorio y tiene hasta 40 caracteres.")
    if curso["status"] == "ARCHIVED":
        return http.error(event, 409, "CURSO_ARCHIVADO", "El curso está archivado: es de solo lectura.")
    seccion = {"sectionId": ids.nuevo("sec"), "name": nombre.strip()}
    t.tabla().put_item(
        Item={"tenant_id": usuario.tenant_id, "sk": comun.sk_seccion(curso["courseId"], seccion["sectionId"]),
              "entidad": "SECTION", "courseId": curso["courseId"], "createdBy": usuario.user_id,
              "createdAt": outbox.ahora_iso(), **seccion},
        ConditionExpression="attribute_not_exists(sk)",
    )
    return http.respuesta(201, seccion)


def listar(event, usuario, curso):
    error = comun.acceso_de_lectura(event, usuario, curso)
    if error:
        return error
    resultado = t.tabla().query(
        KeyConditionExpression=Key("tenant_id").eq(usuario.tenant_id)
        & Key("sk").begins_with(comun.sk_seccion(curso["courseId"]))
    )
    return http.respuesta(200, {"items": [{"sectionId": i["sectionId"], "name": i["name"]}
                                          for i in resultado.get("Items", [])]})


def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    curso = comun.leer_curso(usuario, comun.course_id_de(event))
    if curso is None:
        return comun.no_encontrado(event)
    if (event or {}).get("httpMethod") == "POST":
        return crear(event, usuario, curso)
    return listar(event, usuario, curso)
