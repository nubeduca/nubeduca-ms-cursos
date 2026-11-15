"""COL-01: anuncios por curso. Publicar uno emite lms.courses.announcement.published.v1."""

import json
import re

import boto3
import pytest

from api import anuncios, cursos, matriculas
from consumidores import periodos
from lib import esquemas
from relay import stream_a_bus
from tests.conftest import (ADMIN, ALUMNO, BUS, DOCENTE, OTRO_DOCENTE, TERM, eventos, items,
                            mensaje_term_started, peticion, registros_stream, token)

ANN = re.compile(r"^ann_[0-9A-HJKMNP-TV-Z]{26}$")
PUBLICADO = "lms.courses.announcement.published.v1"
AVISO = {"title": "Cambio de horario", "body": "La clase del jueves pasa a las 10:00."}
RECURSO = "/courses/{courseId}/announcements"


def _curso(publicar=True):
    """Curso de DOCENTE (dueño) con OTRO_DOCENTE matriculado como TEACHER y ALUMNO como STUDENT."""
    periodos.lambda_handler({"Records": [{"messageId": "m", "body": mensaje_term_started()}]}, None)
    r = cursos.lambda_handler(peticion("POST", token(), {"termId": TERM, "code": "CS2032", "title": "Cloud"},
                                       resource="/courses"), None)
    cid = json.loads(r["body"])["courseId"]
    for user, rol in ((OTRO_DOCENTE, "TEACHER"), (ALUMNO, "STUDENT")):
        r = matriculas.lambda_handler(peticion("POST", token(), {"userId": user, "role": rol}, {"courseId": cid},
                                               resource="/courses/{courseId}/enrollments"), None)
        assert r["statusCode"] == 201, r["body"]
    if publicar:
        cursos.lambda_handler(peticion("POST", token(), path_params={"courseId": cid},
                                       resource="/courses/{courseId}/publish"), None)
    return cid


def _publicar(cid, body=AVISO, headers=None):
    headers = token(user=OTRO_DOCENTE) if headers is None else headers
    return anuncios.lambda_handler(peticion("POST", headers, body, {"courseId": cid}, resource=RECURSO), None)


def _listar(cid, headers=None, query=None):
    headers = token(rol="STUDENT", user=ALUMNO) if headers is None else headers
    return anuncios.lambda_handler(peticion("GET", headers, None, {"courseId": cid}, query, resource=RECURSO), None)


def _cuerpo(r):
    return json.loads(r["body"])


def _anuncios_publicados():
    return [e for e in eventos() if e["_detailType"] == PUBLICADO]


def test_docente_matriculado_publica_y_emite_el_evento(aws):
    cid = _curso()
    r = _publicar(cid)
    assert r["statusCode"] == 201, r["body"]
    anuncio = _cuerpo(r)
    assert ANN.match(anuncio["announcementId"])
    assert anuncio | {"announcementId": "x", "publishedAt": "t"} == {
        **AVISO, "announcementId": "x", "publishedAt": "t", "authorId": OTRO_DOCENTE}

    evs = _anuncios_publicados()
    assert len(evs) == 1
    ev = evs[0]
    assert ev["_source"] == "lms.courses"
    assert ev["metadata"]["courseId"] == cid
    assert ev["metadata"]["aggregateType"] == "announcement"
    assert ev["metadata"]["aggregateId"] == anuncio["announcementId"]
    assert ev["metadata"]["aggregateVersion"] == 1
    assert ev["metadata"]["actorId"] == OTRO_DOCENTE
    # Payload mínimo: el cuerpo del anuncio no viaja en el evento.
    assert ev["data"] == {"announcementId": anuncio["announcementId"], "authorId": OTRO_DOCENTE,
                          "title": AVISO["title"]}
    assert esquemas.errores(PUBLICADO, {"metadata": ev["metadata"], "data": ev["data"]}) == []


def test_el_anuncio_viaja_por_el_bus_hasta_la_cola_de_ms_notificaciones(aws):
    """Outbox -> Stream -> relay -> bus -> regla de ms-notificaciones -> su cola."""
    sqs, events = boto3.client("sqs"), boto3.client("events")
    cola = sqs.create_queue(QueueName="notificaciones-eventos")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=cola, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    events.put_rule(Name="notificaciones-anuncios", EventBusName=BUS,
                    EventPattern=json.dumps({"source": ["lms.courses"], "detail-type": [PUBLICADO]}))
    events.put_targets(Rule="notificaciones-anuncios", EventBusName=BUS, Targets=[{"Id": "1", "Arn": arn}])

    cid = _curso()
    anuncio = _cuerpo(_publicar(cid))
    stream_a_bus.publicar(registros_stream())
    mensajes = sqs.receive_message(QueueUrl=cola, MaxNumberOfMessages=10)["Messages"]
    assert len(mensajes) == 1
    cuerpo = json.loads(mensajes[0]["Body"])
    assert cuerpo["detail-type"] == PUBLICADO
    assert cuerpo["detail"]["metadata"]["courseId"] == cid
    assert cuerpo["detail"]["data"]["announcementId"] == anuncio["announcementId"]


def test_solo_un_docente_matriculado_como_teacher_puede_publicar(aws):
    cid = _curso()
    assert _publicar(cid, headers={})["statusCode"] == 401
    assert _publicar(cid, headers=token())["statusCode"] == 403          # dueño, pero no matriculado
    assert _publicar(cid, headers=token(rol="ADMIN", user=ADMIN))["statusCode"] == 403
    assert _publicar(cid, headers=token(rol="STUDENT", user=ALUMNO))["statusCode"] == 403
    otro = "5d0c2b7e-8a1f-4c3d-9e6b-7f8a9b0c1d2e"
    assert _publicar(cid, headers=token(user=otro))["statusCode"] == 403  # docente ajeno (como en ACA-03)
    assert _anuncios_publicados() == []


def test_curso_no_publicado_da_409(aws):
    cid = _curso(publicar=False)
    r = _publicar(cid)
    assert r["statusCode"] == 409
    assert _cuerpo(r)["code"] == "CURSO_NO_PUBLICADO"
    cursos.lambda_handler(peticion("POST", token(), path_params={"courseId": cid},
                                   resource="/courses/{courseId}/publish"), None)
    cursos.lambda_handler(peticion("POST", token(), path_params={"courseId": cid},
                                   resource="/courses/{courseId}/archive"), None)
    assert _publicar(cid)["statusCode"] == 409  # archivado
    assert _anuncios_publicados() == []


@pytest.mark.parametrize("body", [
    {}, {"title": "", "body": "x"}, {"title": "x" * 201, "body": "x"},
    {"title": "x", "body": ""}, {"title": "x", "body": "x" * 10001}, {"title": 3, "body": "x"},
])
def test_valida_el_body(aws, body):
    cid = _curso()
    r = _publicar(cid, body=body)
    assert r["statusCode"] == 400
    assert _cuerpo(r)["code"] == "DATOS_INVALIDOS"


def test_curso_inexistente_o_de_otro_tenant_da_404(aws):
    cid = _curso()
    assert _publicar("crs_01JCQRZHQT9F4WY2JYPDV546K7")["statusCode"] == 404
    assert _publicar("no-es-un-id")["statusCode"] == 404
    assert _publicar(cid, headers=token(tenant="upc", user=OTRO_DOCENTE))["statusCode"] == 404
    assert _listar(cid, headers=token(rol="STUDENT", tenant="upc", user=ALUMNO))["statusCode"] == 404


def test_matriculados_leen_los_anuncios_el_mas_reciente_primero(aws):
    cid = _curso()
    titulos = [f"Aviso {i}" for i in range(5)]
    for titulo in titulos:
        assert _publicar(cid, body={"title": titulo, "body": "x"})["statusCode"] == 201

    vistos, nxt = [], None
    for _ in range(5):
        r = _listar(cid, query={"limit": "2"} | ({"nextToken": nxt} if nxt else {}))
        assert r["statusCode"] == 200, r["body"]
        pagina = _cuerpo(r)
        vistos += [a["title"] for a in pagina["items"]]
        nxt = pagina["nextToken"]
        if nxt is None:
            break
    assert vistos == list(reversed(titulos))
    assert _cuerpo(_listar(cid))["items"][0]["body"] == "x"
    # También el dueño y el ADMIN; un alumno no matriculado no ve el curso.
    assert _listar(cid, headers=token())["statusCode"] == 200
    assert _listar(cid, headers=token(rol="ADMIN", user=ADMIN))["statusCode"] == 200
    ajeno = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"
    assert _listar(cid, headers=token(rol="STUDENT", user=ajeno))["statusCode"] == 404
    assert _listar(cid, headers={})["statusCode"] == 401


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "x"}, {"nextToken": "basura"}])
def test_listar_valida_parametros(aws, query):
    cid = _curso()
    assert _listar(cid, query=query)["statusCode"] == 400


def test_los_anuncios_no_se_mezclan_con_los_cursos(aws):
    cid = _curso()
    _publicar(cid)
    assert len(items("COURSE#")) == 1
    assert len(items(f"ANN#{cid}#")) == 1
    assert DOCENTE != OTRO_DOCENTE
