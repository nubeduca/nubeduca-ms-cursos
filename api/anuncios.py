"""
Anuncios por curso (COL-01), según apis/ms-cursos.openapi.yaml de nubeduca-contratos.

GET  /courses/{courseId}/announcements?limit=&nextToken=   quien puede ver el curso; el más reciente primero
POST /courses/{courseId}/announcements   {title, body}     solo un docente matriculado como TEACHER en el
                                                            curso; 409 si el curso no está PUBLISHED.
                                                            Publica lms.courses.announcement.published.v1

Ítem: ANN#<courseId>#<announcementId>. announcementId es un ULID, así que ordenar por sk de forma
descendente da el más reciente primero.

El aviso a cada matriculado lo hace ms-notificaciones: consume announcement.published y avisa a los
miembros del curso según su proyección de enrollment.created. El evento lleva solo ids y el título;
el cuerpo se lee con GET (payload mínimo).
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from api import comun
from lib import auth, http, ids, outbox
from lib import tabla as t

PUBLICADO = "lms.courses.announcement.published.v1"


def sk_anuncio(course_id: str, announcement_id: str = "") -> str:
    return f"ANN#{course_id}#{announcement_id}"


def a_anuncio(item: dict) -> dict:
    return {k: item[k] for k in ("announcementId", "title", "body", "authorId", "publishedAt")}


def _texto(valor, maximo: int) -> bool:
    return isinstance(valor, str) and bool(valor.strip()) and len(valor) <= maximo


def _docente_del_curso(usuario, course_id: str) -> bool:
    """El contrato pide un docente MATRICULADO como TEACHER (ser dueño del curso no alcanza)."""
    if usuario.rol != "TEACHER":
        return False
    clave = {"tenant_id": usuario.tenant_id, "sk": comun.sk_matricula(course_id, usuario.user_id)}
    matricula = t.tabla().get_item(Key=clave, ConsistentRead=True).get("Item")
    return bool(matricula) and matricula.get("role") == "TEACHER"


def publicar(event, usuario, curso):
    if not _docente_del_curso(usuario, curso["courseId"]):
        return http.error(event, 403, "SIN_PERMISO",
                          "Solo un docente matriculado como TEACHER en el curso puede publicar anuncios.")
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict):
        return http.datos_invalidos(event)
    if not _texto(cuerpo.get("title"), 200):
        return http.datos_invalidos(event, "title es obligatorio y tiene hasta 200 caracteres.")
    if not _texto(cuerpo.get("body"), 10000):
        return http.datos_invalidos(event, "body es obligatorio y tiene hasta 10.000 caracteres.")
    if curso["status"] != "PUBLISHED":
        return http.error(event, 409, "CURSO_NO_PUBLICADO",
                          f"El curso está en {curso['status']}: solo se anuncia en cursos publicados.")

    course_id = curso["courseId"]
    anuncio = {
        "announcementId": ids.nuevo("ann"),
        "title": cuerpo["title"].strip(),
        "body": cuerpo["body"],
        "authorId": usuario.user_id,
        "publishedAt": outbox.ahora_iso(),
    }
    evento = outbox.nuevo_evento(
        PUBLICADO,
        tenant_id=usuario.tenant_id,  # del token, nunca del body
        aggregate_type="announcement",
        aggregate_id=anuncio["announcementId"],
        aggregate_version=1,
        actor_id=usuario.user_id,
        course_id=course_id,
        data={"announcementId": anuncio["announcementId"], "authorId": usuario.user_id,
              "title": anuncio["title"]},
    )
    outbox.guardar(
        cambios=[
            {"Put": {"Item": {"tenant_id": usuario.tenant_id, "sk": sk_anuncio(course_id, anuncio["announcementId"]),
                              "entidad": "ANNOUNCEMENT", "courseId": course_id, **anuncio},
                     "ConditionExpression": "attribute_not_exists(sk)"}},
            # El curso no debe cambiar de estado mientras se publica (por ejemplo, archivarse).
            {"ConditionCheck": {"Key": {"tenant_id": usuario.tenant_id, "sk": comun.sk_curso(course_id)},
                                "ConditionExpression": "#s = :publicado",
                                "ExpressionAttributeNames": {"#s": "status"},
                                "ExpressionAttributeValues": {":publicado": "PUBLISHED"}}},
        ],
        eventos=[evento],
    )
    return http.respuesta(201, anuncio)


def listar(event, usuario, curso):
    params = (event or {}).get("queryStringParameters") or {}
    limite = comun.limite(params)
    if limite is None:
        return http.datos_invalidos(event, "limit debe ser un número entre 1 y 100.")
    prefijo = sk_anuncio(curso["courseId"])
    consulta = {
        "KeyConditionExpression": Key("tenant_id").eq(usuario.tenant_id) & Key("sk").begins_with(prefijo),
        "ScanIndexForward": False,
        "Limit": limite,
    }
    if params.get("nextToken"):
        inicio = comun.decodificar(params["nextToken"], usuario.tenant_id, prefijo)
        if inicio is None:
            return http.datos_invalidos(event, "nextToken no es válido.")
        consulta["ExclusiveStartKey"] = inicio
    resultado = t.tabla().query(**consulta)
    return http.respuesta(200, {"items": [a_anuncio(i) for i in resultado.get("Items", [])],
                                "nextToken": comun.codificar(resultado.get("LastEvaluatedKey"))})


def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    curso = comun.leer_curso(usuario, comun.course_id_de(event), consistente=True)
    error = comun.acceso_de_lectura(event, usuario, curso)
    if error:
        return error
    try:
        if (event or {}).get("httpMethod") == "POST":
            return publicar(event, usuario, curso)
        return listar(event, usuario, curso)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            return http.error(event, 409, "CURSO_NO_PUBLICADO", "El curso cambió de estado; vuelve a intentarlo.")
        raise
