"""
Matrículas (ACA-04), según apis/ms-cursos.openapi.yaml de nubeduca-contratos.

GET    /courses/{courseId}/enrollments?role=&limit=&nextToken=   (TEACHER, ADMIN)
POST   /courses/{courseId}/enrollments          {userId, role, sectionId?}   publica enrollment.created
POST   /courses/{courseId}/enrollments/import   text/csv institutionalCode,role,section (hasta 2.000 filas)
DELETE /courses/{courseId}/enrollments/{enrollmentId}                       publica enrollment.dropped

Escribir: dueño del curso o ADMIN. Leer: además, un docente matriculado en el curso.

Ítems de una matrícula (se escriben y borran juntos, con su evento, en una transacción):
- ENROLL#<courseId>#<userId>       la matrícula (comun.matriculado la usa para los permisos)
- ENROLLID#<enrollmentId>          puntero para el DELETE
- USERCOURSE#<userId>#<courseId>   índice inverso: los cursos del alumno en GET /courses

La importación CSV busca cada código en PROY#USERCODE#<código>, la proyección de
lms.users.user.provisioned.v1 (consumidores/usuarios.py). Las filas con error no detienen
la importación: se devuelven en `errors` con su número de línea (la cabecera es la línea 1).
"""

import base64
import binascii
import csv
import io
import re

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from api import comun
from lib import auth, http, ids, outbox
from lib import tabla as t

CREADA = "lms.courses.enrollment.created.v1"
RETIRADA = "lms.courses.enrollment.dropped.v1"
ROLES = ("STUDENT", "TEACHER")
USER_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECTION_ID = re.compile(r"^sec_[0-9A-HJKMNP-TV-Z]{26}$")
ENROLLMENT_ID = re.compile(r"^enr_[0-9A-HJKMNP-TV-Z]{26}$")
CABECERA = ["institutionalCode", "role", "section"]
MAX_FILAS = 2000
FILAS_POR_TRANSACCION = 20  # 4 operaciones por fila (3 ítems + evento): 80 de 100 posibles


def a_matricula(item: dict) -> dict:
    matricula = {k: item[k] for k in ("enrollmentId", "userId", "role", "createdAt")}
    if item.get("sectionId"):
        matricula["sectionId"] = item["sectionId"]
    return matricula


# --- Escritura -------------------------------------------------------------------


def _operaciones(tenant_id: str, curso: dict, user_id: str, role: str, section_id: str | None,
                 actor_id: str) -> tuple[dict, list[dict], dict]:
    """Devuelve (matrícula, cambios para outbox.guardar, evento enrollment.created)."""
    course_id = curso["courseId"]
    matricula = {"enrollmentId": ids.nuevo("enr"), "userId": user_id, "role": role,
                 "createdAt": outbox.ahora_iso()}
    if section_id:
        matricula["sectionId"] = section_id
    nuevo = "attribute_not_exists(sk)"
    cambios = [
        {"Put": {"Item": {"tenant_id": tenant_id, "sk": comun.sk_matricula(course_id, user_id),
                          "entidad": "ENROLLMENT", "courseId": course_id, "version": 1, **matricula},
                 "ConditionExpression": nuevo}},
        {"Put": {"Item": {"tenant_id": tenant_id, "sk": comun.sk_matricula_id(matricula["enrollmentId"]),
                          "entidad": "ENROLLMENT_ID", "courseId": course_id, "userId": user_id},
                 "ConditionExpression": nuevo}},
        {"Put": {"Item": {"tenant_id": tenant_id, "sk": comun.sk_curso_de_usuario(user_id, course_id),
                          "entidad": "USER_COURSE", "courseId": course_id, "userId": user_id, "role": role,
                          "enrollmentId": matricula["enrollmentId"]},
                 "ConditionExpression": nuevo}},
    ]
    data = {"enrollmentId": matricula["enrollmentId"], "userId": user_id, "role": role}
    if section_id:
        data["sectionId"] = section_id
    evento = outbox.nuevo_evento(
        CREADA,
        tenant_id=tenant_id,
        aggregate_type="enrollment",
        aggregate_id=matricula["enrollmentId"],
        aggregate_version=1,
        actor_id=actor_id,
        course_id=course_id,
        data=data,
    )
    return matricula, cambios, evento


def _cancelada(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "TransactionCanceledException"


def _seccion_existe(tenant_id: str, course_id: str, section_id: str) -> bool:
    clave = {"tenant_id": tenant_id, "sk": comun.sk_seccion(course_id, section_id)}
    return t.tabla().get_item(Key=clave).get("Item") is not None


def _archivado(event) -> dict:
    return http.error(event, 409, "CURSO_ARCHIVADO", "El curso está archivado: es de solo lectura.")


def crear(event, usuario, curso):
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict):
        return http.datos_invalidos(event)
    user_id, role, section_id = cuerpo.get("userId"), cuerpo.get("role"), cuerpo.get("sectionId")
    if not isinstance(user_id, str) or not USER_ID.match(user_id):
        return http.datos_invalidos(event, "userId es obligatorio y debe ser un uuid.")
    if role not in ROLES:
        return http.datos_invalidos(event, "role debe ser STUDENT o TEACHER.")
    if section_id is not None and (not isinstance(section_id, str) or not SECTION_ID.match(section_id)):
        return http.datos_invalidos(event, "sectionId debe ser un id de sección (sec_...).")
    if curso["status"] == "ARCHIVED":
        return _archivado(event)
    if section_id and not _seccion_existe(usuario.tenant_id, curso["courseId"], section_id):
        return http.datos_invalidos(event, "La sección no existe en este curso.")

    matricula, cambios, evento = _operaciones(usuario.tenant_id, curso, user_id, role, section_id,
                                              usuario.user_id)
    try:
        outbox.guardar(cambios=cambios, eventos=[evento])
    except ClientError as exc:
        if _cancelada(exc):
            return http.error(event, 409, "YA_MATRICULADO", "El usuario ya está matriculado en el curso.")
        raise
    return http.respuesta(201, matricula)


def retirar(event, usuario, curso, enrollment_id: str):
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    no_existe = http.error(event, 404, "MATRICULA_NO_ENCONTRADA", "No existe la matrícula en este curso.")
    if not ENROLLMENT_ID.match(enrollment_id or ""):
        return no_existe
    tenant_id, course_id = usuario.tenant_id, curso["courseId"]
    puntero = t.tabla().get_item(Key={"tenant_id": tenant_id, "sk": comun.sk_matricula_id(enrollment_id)},
                                 ConsistentRead=True).get("Item")
    if not puntero or puntero["courseId"] != course_id:
        return no_existe
    user_id = puntero["userId"]
    existe = "attribute_exists(sk)"
    evento = outbox.nuevo_evento(
        RETIRADA,
        tenant_id=tenant_id,
        aggregate_type="enrollment",
        aggregate_id=enrollment_id,
        aggregate_version=2,
        actor_id=usuario.user_id,
        course_id=course_id,
        data={"enrollmentId": enrollment_id, "userId": user_id},
    )
    try:
        outbox.guardar(
            cambios=[
                {"Delete": {"Key": {"tenant_id": tenant_id, "sk": comun.sk_matricula(course_id, user_id)},
                            "ConditionExpression": existe}},
                {"Delete": {"Key": {"tenant_id": tenant_id, "sk": comun.sk_matricula_id(enrollment_id)},
                            "ConditionExpression": existe}},
                {"Delete": {"Key": {"tenant_id": tenant_id, "sk": comun.sk_curso_de_usuario(user_id, course_id)}}},
            ],
            eventos=[evento],
        )
    except ClientError as exc:
        if _cancelada(exc):  # otra llamada la retiró primero
            return no_existe
        raise
    return {"statusCode": 204, "headers": dict(http.CORS), "body": ""}


# --- Importación CSV -------------------------------------------------------------


def _texto_csv(event) -> str | None:
    cuerpo = (event or {}).get("body")
    if not isinstance(cuerpo, str):
        return None
    if (event or {}).get("isBase64Encoded"):
        try:
            cuerpo = base64.b64decode(cuerpo).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return cuerpo.lstrip("﻿")  # Excel guarda el CSV con BOM


def _filas(texto: str) -> list[list[str]] | None:
    """Filas del CSV sin la cabecera, o None si la cabecera no es la del contrato."""
    lineas = list(csv.reader(io.StringIO(texto)))
    if not lineas or [c.strip().lower() for c in lineas[0]] != [c.lower() for c in CABECERA]:
        return None
    return lineas[1:]


def _resolver(tenant_id: str, filas: list[list[str]], secciones: dict[str, str]):
    """Valida cada fila. Devuelve (filas válidas [(línea, código, userId, rol, sectionId)], errores)."""
    validas, errores, codigos = [], [], {}
    for numero, fila in enumerate(filas, start=2):
        if not any(c.strip() for c in fila):
            continue  # línea en blanco
        celdas = [c.strip() for c in fila] + [""] * 3
        codigo, rol, seccion = celdas[0], celdas[1].upper(), celdas[2]
        if not codigo:
            errores.append({"line": numero, "code": "CODIGO_VACIO"})
            continue
        if rol not in ROLES:
            errores.append({"line": numero, "code": "ROL_INVALIDO", "institutionalCode": codigo})
            continue
        section_id = None
        if seccion:
            section_id = secciones.get(seccion.lower())
            if section_id is None:
                errores.append({"line": numero, "code": "SECCION_NO_EXISTE", "institutionalCode": codigo})
                continue
        if codigo not in codigos:
            proy = t.tabla().get_item(Key={"tenant_id": tenant_id, "sk": comun.sk_codigo_usuario(codigo)}).get("Item")
            codigos[codigo] = proy["userId"] if proy else None
        if codigos[codigo] is None:
            errores.append({"line": numero, "code": "USUARIO_NO_EXISTE", "institutionalCode": codigo})
            continue
        validas.append((numero, codigo, codigos[codigo], rol, section_id))
    return validas, errores


def _secciones_por_nombre(tenant_id: str, course_id: str) -> dict[str, str]:
    resultado = t.tabla().query(
        KeyConditionExpression=Key("tenant_id").eq(tenant_id) & Key("sk").begins_with(comun.sk_seccion(course_id))
    )
    return {i["name"].strip().lower(): i["sectionId"] for i in resultado.get("Items", [])}


def _matriculados(tenant_id: str, course_id: str) -> set[str]:
    usuarios, clave = set(), None
    while True:
        consulta = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id)
                    & Key("sk").begins_with(comun.sk_matricula(course_id)),
                    "ProjectionExpression": "userId"}
        if clave:
            consulta["ExclusiveStartKey"] = clave
        resultado = t.tabla().query(**consulta)
        usuarios |= {i["userId"] for i in resultado.get("Items", [])}
        clave = resultado.get("LastEvaluatedKey")
        if not clave:
            return usuarios


def _guardar_lote(tenant_id: str, curso: dict, lote: list, actor_id: str) -> tuple[int, int]:
    """Guarda un lote en una transacción. Si otra llamada ganó alguna fila, va de a una. (creadas, omitidas)"""
    preparadas = [_operaciones(tenant_id, curso, user_id, rol, section_id, actor_id)
                  for _, _, user_id, rol, section_id in lote]
    try:
        outbox.guardar(cambios=[c for _, cambios, _ in preparadas for c in cambios],
                       eventos=[evento for _, _, evento in preparadas])
        return len(lote), 0
    except ClientError as exc:
        if not _cancelada(exc):
            raise
        if len(lote) == 1:
            return 0, 1
    creadas = omitidas = 0
    for fila in lote:
        c, o = _guardar_lote(tenant_id, curso, [fila], actor_id)
        creadas, omitidas = creadas + c, omitidas + o
    return creadas, omitidas


def importar(event, usuario, curso):
    if not comun.puede_editar(usuario, curso):
        return http.sin_permiso(event)
    texto = _texto_csv(event)
    filas = _filas(texto) if texto is not None else None
    if filas is None:
        return http.datos_invalidos(event, f"El CSV debe empezar con la cabecera {','.join(CABECERA)}.")
    if len(filas) > MAX_FILAS:
        return http.datos_invalidos(event, f"El CSV tiene más de {MAX_FILAS} filas.")
    if curso["status"] == "ARCHIVED":
        return _archivado(event)

    tenant_id, course_id = usuario.tenant_id, curso["courseId"]
    validas, errores = _resolver(tenant_id, filas, _secciones_por_nombre(tenant_id, course_id))
    ya, nuevas = _matriculados(tenant_id, course_id), []
    omitidas = 0
    for fila in validas:
        if fila[2] in ya:  # ya matriculado (o repetido en el mismo CSV)
            omitidas += 1
            continue
        ya.add(fila[2])
        nuevas.append(fila)

    creadas = 0
    for inicio in range(0, len(nuevas), FILAS_POR_TRANSACCION):
        c, o = _guardar_lote(tenant_id, curso, nuevas[inicio:inicio + FILAS_POR_TRANSACCION], usuario.user_id)
        creadas, omitidas = creadas + c, omitidas + o
    return http.respuesta(200, {"created": creadas, "skipped": omitidas, "errors": errores})


# --- Lectura ---------------------------------------------------------------------


def listar(event, usuario, curso):
    if usuario.rol == "STUDENT" or not comun.puede_ver(usuario, curso):
        return http.sin_permiso(event)
    params = (event or {}).get("queryStringParameters") or {}
    limite = comun.limite(params)
    if limite is None:
        return http.datos_invalidos(event, "limit debe ser un número entre 1 y 100.")
    if params.get("role") and params["role"] not in ROLES:
        return http.datos_invalidos(event, "role debe ser STUDENT o TEACHER.")
    prefijo = comun.sk_matricula(curso["courseId"])
    clave = None
    if params.get("nextToken"):
        clave = comun.decodificar(params["nextToken"], usuario.tenant_id, prefijo)
        if clave is None:
            return http.datos_invalidos(event, "nextToken no es válido.")

    encontrados = []
    while True:
        consulta = {"KeyConditionExpression": Key("tenant_id").eq(usuario.tenant_id) & Key("sk").begins_with(prefijo),
                    "Limit": limite - len(encontrados)}
        if params.get("role"):
            consulta["FilterExpression"] = Attr("role").eq(params["role"])
        if clave:
            consulta["ExclusiveStartKey"] = clave
        resultado = t.tabla().query(**consulta)
        encontrados += resultado.get("Items", [])
        clave = resultado.get("LastEvaluatedKey")
        if clave is None or len(encontrados) >= limite:
            break
    return http.respuesta(200, {"items": [a_matricula(i) for i in encontrados],
                                "nextToken": comun.codificar(clave)})


def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    if usuario.rol not in ("TEACHER", "ADMIN"):
        return http.sin_permiso(event)
    escribir = (event or {}).get("httpMethod") in ("POST", "DELETE")
    curso = comun.leer_curso(usuario, comun.course_id_de(event), consistente=escribir)
    if curso is None:
        return comun.no_encontrado(event)
    metodo = (event or {}).get("httpMethod", "GET")
    recurso = (event or {}).get("resource") or ""
    enrollment_id = ((event or {}).get("pathParameters") or {}).get("enrollmentId")
    if metodo == "DELETE":
        return retirar(event, usuario, curso, enrollment_id or "")
    if recurso.endswith("/import"):
        return importar(event, usuario, curso)
    if metodo == "POST":
        return crear(event, usuario, curso)
    return listar(event, usuario, curso)
