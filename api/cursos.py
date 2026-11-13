"""
Cursos (ACA-03), según apis/ms-cursos.openapi.yaml de nubeduca-contratos.

GET   /courses                       (STUDENT, TEACHER, ADMIN)  STUDENT: PUBLISHED donde está matriculado
                                                                 (ACA-04); TEACHER: los suyos; ADMIN: todos
POST  /courses                       (TEACHER, ADMIN)           DRAFT, publica course.created
GET   /courses/{courseId}            (STUDENT, TEACHER, ADMIN)
PATCH /courses/{courseId}            (TEACHER dueño, ADMIN)     title y description
POST  /courses/{courseId}/publish    (TEACHER dueño, ADMIN)     DRAFT -> PUBLISHED, publica course.published
POST  /courses/{courseId}/archive    (TEACHER dueño, ADMIN)     DRAFT|PUBLISHED -> ARCHIVED, publica course.archived

Ítems: COURSE#<courseId> (el curso) y COURSECODE#<termId>#<code> (código único por periodo).
Un curso solo se crea en un periodo ACTIVE: PROY#TERM#<termId>, que llena consumidores/periodos.py.
"""

import re

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from api import comun
from lib import auth, http, ids, outbox
from lib import tabla as t

CREADO = "lms.courses.course.created.v1"
PUBLICADO = "lms.courses.course.published.v1"
ARCHIVADO = "lms.courses.course.archived.v1"
CODIGO = re.compile(r"^[A-Z0-9-]{2,20}$")
ESTADOS = ("DRAFT", "PUBLISHED", "ARCHIVED")
ROLES_ESCRITURA = ("TEACHER", "ADMIN")
# Transiciones: acción -> (estados de origen permitidos, estado destino, evento, código del 409)
TRANSICIONES = {
    "publish": (("DRAFT",), "PUBLISHED", PUBLICADO, "CURSO_NO_EN_BORRADOR"),
    "archive": (("DRAFT", "PUBLISHED"), "ARCHIVED", ARCHIVADO, "CURSO_ARCHIVADO"),
}


def _texto(valor, minimo: int, maximo: int) -> bool:
    return isinstance(valor, str) and minimo <= len(valor.strip()) and len(valor) <= maximo


def _validar_nuevo(cuerpo: dict) -> str | None:
    if not isinstance(cuerpo.get("termId"), str) or not comun.TERM_ID.match(cuerpo["termId"]):
        return "termId es obligatorio y debe ser un id de periodo (trm_...)."
    if not isinstance(cuerpo.get("code"), str) or not CODIGO.match(cuerpo["code"]):
        return "code es obligatorio: 2 a 20 caracteres en mayúsculas, números o guiones."
    if not _texto(cuerpo.get("title"), 1, 200):
        return "title es obligatorio y tiene hasta 200 caracteres."
    if "description" in cuerpo and not _texto(cuerpo["description"], 0, 2000):
        return "description tiene hasta 2000 caracteres."
    return None


def crear(event, usuario):
    if usuario.rol not in ROLES_ESCRITURA:
        return http.sin_permiso(event)
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict):
        return http.datos_invalidos(event)
    problema = _validar_nuevo(cuerpo)
    if problema:
        return http.datos_invalidos(event, problema)

    tenant_id = usuario.tenant_id  # del token, nunca del body
    term_id, code = cuerpo["termId"], cuerpo["code"]
    periodo = t.tabla().get_item(Key={"tenant_id": tenant_id, "sk": comun.sk_periodo(term_id)}).get("Item")
    if not periodo or periodo.get("status") != "ACTIVE":
        return http.error(event, 409, "PERIODO_NO_ACTIVO", "El periodo no existe o no está activo.")

    course_id = ids.nuevo("crs")
    curso = {
        "courseId": course_id,
        "termId": term_id,
        "code": code,
        "title": cuerpo["title"].strip(),
        "status": "DRAFT",
        "ownerId": usuario.user_id,
        "createdAt": outbox.ahora_iso(),
    }
    if "description" in cuerpo:
        curso["description"] = cuerpo["description"]
    evento = outbox.nuevo_evento(
        CREADO,
        tenant_id=tenant_id,
        aggregate_type="course",
        aggregate_id=course_id,
        aggregate_version=1,
        actor_id=usuario.user_id,
        course_id=course_id,
        data={"termId": term_id, "code": code, "title": curso["title"], "ownerId": usuario.user_id},
    )
    try:
        outbox.guardar(
            cambios=[
                {"Put": {"Item": {"tenant_id": tenant_id, "sk": comun.sk_curso(course_id), "entidad": "COURSE",
                                  "version": 1, **curso},
                         "ConditionExpression": "attribute_not_exists(sk)"}},
                {"Put": {"Item": {"tenant_id": tenant_id, "sk": comun.sk_codigo(term_id, code),
                                  "entidad": "COURSECODE", "courseId": course_id},
                         "ConditionExpression": "attribute_not_exists(sk)"}},
            ],
            eventos=[evento],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            return http.error(event, 409, "CURSO_DUPLICADO", f"Ya existe un curso {code} en ese periodo.")
        raise
    return http.respuesta(201, curso)


def obtener(event, usuario):
    curso = comun.leer_curso(usuario, comun.course_id_de(event))
    error = comun.acceso_de_lectura(event, usuario, curso)
    return error or http.respuesta(200, comun.a_curso(curso))


def editar(event, usuario):
    if usuario.rol not in ROLES_ESCRITURA:
        return http.sin_permiso(event)
    curso = comun.leer_curso(usuario, comun.course_id_de(event), consistente=True)
    if curso is None:
        return comun.no_encontrado(event)
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict) or not cuerpo or set(cuerpo) - {"title", "description"}:
        return http.datos_invalidos(event, "Solo se pueden cambiar title y description.")
    if "title" in cuerpo and not _texto(cuerpo["title"], 1, 200):
        return http.datos_invalidos(event, "title tiene entre 1 y 200 caracteres.")
    if "description" in cuerpo and not _texto(cuerpo["description"], 0, 2000):
        return http.datos_invalidos(event, "description tiene hasta 2000 caracteres.")
    if curso["status"] == "ARCHIVED":
        return http.error(event, 409, "CURSO_ARCHIVADO", "El curso está archivado: es de solo lectura.")

    cambios = {k: (v.strip() if k == "title" else v) for k, v in cuerpo.items()}
    nombres = {f"#{k}": k for k in cambios} | {"#v": "version", "#s": "status"}
    valores = {f":{k}": v for k, v in cambios.items()} | {":uno": 1, ":archivado": "ARCHIVED",
                                                          ":actual": curso["version"]}
    asignaciones = ", ".join(f"#{k} = :{k}" for k in cambios)
    try:
        t.tabla().update_item(
            Key={"tenant_id": usuario.tenant_id, "sk": comun.sk_curso(curso["courseId"])},
            UpdateExpression=f"SET {asignaciones}, #v = #v + :uno",
            ConditionExpression="#s <> :archivado AND #v = :actual",
            ExpressionAttributeNames=nombres,
            ExpressionAttributeValues=valores,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return http.error(event, 409, "CURSO_CAMBIO", "El curso cambió mientras lo editabas; vuelve a intentarlo.")
        raise
    return http.respuesta(200, comun.a_curso(curso | cambios))


def transicion(event, usuario, accion: str):
    if usuario.rol not in ROLES_ESCRITURA:
        return http.sin_permiso(event)
    curso = comun.leer_curso(usuario, comun.course_id_de(event), consistente=True)
    if curso is None:
        return comun.no_encontrado(event)
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    origenes, destino, detail_type, codigo_409 = TRANSICIONES[accion]
    if curso["status"] not in origenes:
        return http.error(event, 409, codigo_409, f"El curso está en {curso['status']}; no se puede {accion}.")

    version = int(curso["version"]) + 1
    course_id = curso["courseId"]
    evento = outbox.nuevo_evento(
        detail_type,
        tenant_id=usuario.tenant_id,
        aggregate_type="course",
        aggregate_id=course_id,
        aggregate_version=version,
        actor_id=usuario.user_id,
        course_id=course_id,
        data={"title": curso["title"]} if detail_type == PUBLICADO else {},
    )
    try:
        outbox.guardar(
            cambios=[{"Update": {
                "Key": {"tenant_id": usuario.tenant_id, "sk": comun.sk_curso(course_id)},
                "UpdateExpression": "SET #s = :destino, #v = :nueva",
                # Si dos llamadas compiten, solo una pasa y solo una publica.
                "ConditionExpression": "#s = :origen AND #v = :actual",
                "ExpressionAttributeNames": {"#s": "status", "#v": "version"},
                "ExpressionAttributeValues": {":destino": destino, ":origen": curso["status"],
                                              ":nueva": version, ":actual": version - 1},
            }}],
            eventos=[evento],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            return http.error(event, 409, codigo_409, "El curso cambió de estado; vuelve a intentarlo.")
        raise
    return http.respuesta(200, comun.a_curso(curso | {"status": destino}))


def listar(event, usuario):
    params = (event or {}).get("queryStringParameters") or {}
    limite = comun.limite(params)
    if limite is None:
        return http.datos_invalidos(event, "limit debe ser un número entre 1 y 100.")
    if params.get("status") and params["status"] not in ESTADOS:
        return http.datos_invalidos(event, "status debe ser DRAFT, PUBLISHED o ARCHIVED.")
    if params.get("termId") and not comun.TERM_ID.match(params["termId"]):
        return http.datos_invalidos(event, "termId no es un id de periodo válido.")
    inicio = None
    if params.get("nextToken"):
        inicio = comun.decodificar(params["nextToken"], usuario.tenant_id, "COURSE#")
        if inicio is None:
            return http.datos_invalidos(event, "nextToken no es válido.")

    if usuario.rol == "STUDENT":
        # Los cursos del alumno salen de sus matrículas (ACA-04). Sin matrículas, no ve ninguno.
        return http.respuesta(200, {"items": [], "nextToken": None})

    filtro = None
    for condicion in (
        Attr("ownerId").eq(usuario.user_id) if usuario.rol == "TEACHER" else None,
        Attr("status").eq(params["status"]) if params.get("status") else None,
        Attr("termId").eq(params["termId"]) if params.get("termId") else None,
    ):
        if condicion is not None:
            filtro = condicion if filtro is None else filtro & condicion

    # Con filtro, DynamoDB aplica Limit antes de filtrar: se sigue leyendo hasta llenar la página.
    encontrados, clave = [], inicio
    while True:
        consulta = {
            "KeyConditionExpression": Key("tenant_id").eq(usuario.tenant_id) & Key("sk").begins_with("COURSE#"),
            "Limit": limite - len(encontrados),
        }
        if filtro is not None:
            consulta["FilterExpression"] = filtro
        if clave:
            consulta["ExclusiveStartKey"] = clave
        resultado = t.tabla().query(**consulta)
        encontrados += resultado.get("Items", [])
        clave = resultado.get("LastEvaluatedKey")
        if clave is None or len(encontrados) >= limite:
            break
    return http.respuesta(200, {"items": [comun.a_curso(i) for i in encontrados],
                                "nextToken": comun.codificar(clave)})


def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    metodo = (event or {}).get("httpMethod", "GET")
    recurso = (event or {}).get("resource") or ""
    if recurso.endswith("/publish"):
        return transicion(event, usuario, "publish")
    if recurso.endswith("/archive"):
        return transicion(event, usuario, "archive")
    if comun.course_id_de(event):
        return editar(event, usuario) if metodo == "PATCH" else obtener(event, usuario)
    return crear(event, usuario) if metodo == "POST" else listar(event, usuario)
