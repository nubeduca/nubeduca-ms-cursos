"""ACA-04: matrícula manual, importación CSV, retiro y cursos del alumno."""

import base64
import json
import re

from api import cursos, matriculas, secciones
from consumidores import eventos as consumidor
from consumidores import periodos
from lib import esquemas, inbox
from scripts import poblar_datos
from tests.conftest import (ADMIN, ALUMNO, DOCENTE, EJEMPLOS, OTRO_DOCENTE, TERM, eventos, items,
                            mensaje_term_started, peticion, token)

ENR = re.compile(r"^enr_[0-9A-HJKMNP-TV-Z]{26}$")
ALUMNO_2 = "3c9a1f0e-5b2d-4e8a-9c7f-1d2e3f4a5b6c"
CREADA, RETIRADA = "lms.courses.enrollment.created.v1", "lms.courses.enrollment.dropped.v1"


def _curso(publicar=False, headers=None):
    periodos.lambda_handler({"Records": [{"messageId": "m", "body": mensaje_term_started()}]}, None)
    r = cursos.lambda_handler(peticion("POST", headers or token(), {"termId": TERM, "code": "CS2032",
                                                                     "title": "Cloud"}, resource="/courses"), None)
    cid = json.loads(r["body"])["courseId"]
    if publicar:
        cursos.lambda_handler(peticion("POST", headers or token(), path_params={"courseId": cid},
                                       resource="/courses/{courseId}/publish"), None)
    return cid


def _llamar(metodo, cid, body=None, headers=None, recurso="/courses/{courseId}/enrollments", extra=None,
            query=None):
    path = {"courseId": cid, **(extra or {})}
    evento = peticion(metodo, token() if headers is None else headers, body, path, query, resource=recurso)
    return matriculas.lambda_handler(evento, None)


def _importar(cid, csv, headers=None, base64_=False):
    evento = peticion("POST", token() if headers is None else headers, None, {"courseId": cid},
                      resource="/courses/{courseId}/enrollments/import")
    evento["body"] = base64.b64encode(csv.encode()).decode() if base64_ else csv
    evento["isBase64Encoded"] = base64_
    return matriculas.lambda_handler(evento, None)


def _usuario(user_id, codigo, rol="STUDENT", event_id="01JC6HTF3PYB8X8HHCZDGRPY33", tenant="utec"):
    """Simula user.provisioned de ms-usuarios con el ejemplo oficial del catálogo."""
    ev = json.loads((EJEMPLOS / "lms.users.user.provisioned.v1.json").read_text(encoding="utf-8"))
    ev["detail"]["metadata"].update(eventId=event_id, tenantId=tenant, aggregateId=user_id)
    ev["detail"]["data"].update(userId=user_id, role=rol, institutionalCode=codigo)
    return consumidor.lambda_handler({"Records": [{"messageId": event_id, "body": json.dumps(ev)}]}, None)


def _cuerpo(r):
    return json.loads(r["body"]) if r.get("body") else None


# --- Contrato ---------------------------------------------------------------------


def test_ejemplos_oficiales_cumplen_su_esquema():
    for nombre in (CREADA, RETIRADA):
        ev = json.loads((EJEMPLOS / f"{nombre}.json").read_text(encoding="utf-8"))
        assert esquemas.errores(ev["detail-type"], ev["detail"]) == []


# --- Matrícula manual -------------------------------------------------------------


def test_matricular_publica_enrollment_created(aws):
    cid = _curso()
    r = _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"})
    assert r["statusCode"] == 201
    m = _cuerpo(r)
    assert ENR.match(m["enrollmentId"]) and m["userId"] == ALUMNO and m["role"] == "STUDENT" and m["createdAt"]
    ev = [e for e in eventos() if e["_detailType"] == CREADA]
    assert len(ev) == 1
    meta, data = ev[0]["metadata"], ev[0]["data"]
    assert meta["aggregateType"] == "enrollment" and meta["aggregateId"] == m["enrollmentId"]
    assert meta["aggregateVersion"] == 1 and meta["courseId"] == cid and meta["actorId"] == DOCENTE
    assert data == {"enrollmentId": m["enrollmentId"], "userId": ALUMNO, "role": "STUDENT"}
    assert esquemas.errores(CREADA, {"metadata": meta, "data": data}) == []
    assert len(items(f"ENROLL#{cid}#")) == 1 and len(items(f"USERCOURSE#{ALUMNO}#")) == 1


def test_matricula_con_seccion(aws):
    cid = _curso()
    r = secciones.lambda_handler(peticion("POST", token(), {"name": "Sección 1"}, {"courseId": cid},
                                          resource="/courses/{courseId}/sections"), None)
    sec = _cuerpo(r)["sectionId"]
    r = _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT", "sectionId": sec})
    assert r["statusCode"] == 201 and _cuerpo(r)["sectionId"] == sec
    ev = [e for e in eventos() if e["_detailType"] == CREADA][0]
    assert ev["data"]["sectionId"] == sec
    otra = "sec_01JCQRZHQT9F4WY2JYPDV546K7"
    assert _llamar("POST", cid, {"userId": ALUMNO_2, "role": "STUDENT", "sectionId": otra})["statusCode"] == 400


def test_matricula_duplicada_y_curso_archivado_dan_409(aws):
    cid = _curso()
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"})["statusCode"] == 201
    r = _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"})
    assert r["statusCode"] == 409 and _cuerpo(r)["code"] == "YA_MATRICULADO"
    assert len([e for e in eventos() if e["_detailType"] == CREADA]) == 1  # el 409 no publica
    cursos.lambda_handler(peticion("POST", token(), path_params={"courseId": cid},
                                   resource="/courses/{courseId}/archive"), None)
    r = _llamar("POST", cid, {"userId": ALUMNO_2, "role": "STUDENT"})
    assert r["statusCode"] == 409 and _cuerpo(r)["code"] == "CURSO_ARCHIVADO"


def test_validacion_permisos_y_tenants(aws):
    cid = _curso()
    assert _llamar("POST", cid, {"userId": "no-uuid", "role": "STUDENT"})["statusCode"] == 400
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "ADMIN"})["statusCode"] == 400
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}, headers={})["statusCode"] == 401
    alumno = token(rol="STUDENT", user=ALUMNO)
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}, headers=alumno)["statusCode"] == 403
    assert _llamar("GET", cid, headers=alumno)["statusCode"] == 403
    otro = token(user=OTRO_DOCENTE)
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}, headers=otro)["statusCode"] == 403
    assert _llamar("GET", cid, headers=token(tenant="upc"))["statusCode"] == 404  # otro tenant no lo ve
    admin = token(rol="ADMIN", user=ADMIN)
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}, headers=admin)["statusCode"] == 201


def test_listar_filtra_por_rol_y_pagina(aws):
    cid = _curso()
    _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"})
    _llamar("POST", cid, {"userId": ALUMNO_2, "role": "STUDENT"})
    _llamar("POST", cid, {"userId": OTRO_DOCENTE, "role": "TEACHER"})
    todos = _cuerpo(_llamar("GET", cid))
    assert len(todos["items"]) == 3 and todos["nextToken"] is None
    alumnos = _cuerpo(_llamar("GET", cid, query={"role": "STUDENT"}))["items"]
    assert {m["userId"] for m in alumnos} == {ALUMNO, ALUMNO_2}
    p1 = _cuerpo(_llamar("GET", cid, query={"limit": "2"}))
    assert len(p1["items"]) == 2 and p1["nextToken"]
    p2 = _cuerpo(_llamar("GET", cid, query={"limit": "2", "nextToken": p1["nextToken"]}))
    assert len(p2["items"]) == 1 and p2["nextToken"] is None
    # el docente matriculado (no dueño) puede ver la lista y el curso
    otro = token(user=OTRO_DOCENTE)
    assert _llamar("GET", cid, headers=otro)["statusCode"] == 200
    assert _llamar("GET", cid, query={"nextToken": "basura"})["statusCode"] == 400


# --- Retiro -----------------------------------------------------------------------


def test_retirar_publica_dropped_y_libera(aws):
    cid = _curso()
    enr = _cuerpo(_llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}))["enrollmentId"]
    recurso = "/courses/{courseId}/enrollments/{enrollmentId}"
    r = _llamar("DELETE", cid, recurso=recurso, extra={"enrollmentId": enr})
    assert r["statusCode"] == 204
    ev = [e for e in eventos() if e["_detailType"] == RETIRADA]
    assert len(ev) == 1
    assert ev[0]["data"] == {"enrollmentId": enr, "userId": ALUMNO}
    assert ev[0]["metadata"]["aggregateVersion"] == 2 and ev[0]["metadata"]["courseId"] == cid
    assert esquemas.errores(RETIRADA, {"metadata": ev[0]["metadata"], "data": ev[0]["data"]}) == []
    assert items(f"ENROLL#{cid}#") == [] and items("ENROLLID#") == [] and items(f"USERCOURSE#{ALUMNO}#") == []
    assert _llamar("DELETE", cid, recurso=recurso, extra={"enrollmentId": enr})["statusCode"] == 404
    assert _llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"})["statusCode"] == 201  # se puede volver


def test_retirar_matricula_de_otro_curso_da_404(aws):
    cid = _curso()
    enr = _cuerpo(_llamar("POST", cid, {"userId": ALUMNO, "role": "STUDENT"}))["enrollmentId"]
    otro_curso = _cuerpo(cursos.lambda_handler(peticion("POST", token(), {"termId": TERM, "code": "CS1111",
                                                                          "title": "Otro"},
                                                        resource="/courses"), None))["courseId"]
    r = _llamar("DELETE", otro_curso, recurso="/courses/{courseId}/enrollments/{enrollmentId}",
                extra={"enrollmentId": enr})
    assert r["statusCode"] == 404 and len(items(f"ENROLL#{cid}#")) == 1


# --- Importación CSV ----------------------------------------------------------------


def test_csv_crea_omite_y_reporta_errores(aws):
    cid = _curso()
    secciones.lambda_handler(peticion("POST", token(), {"name": "Sección 1"}, {"courseId": cid},
                                      resource="/courses/{courseId}/sections"), None)
    _usuario(ALUMNO, "202310457")
    _usuario(ALUMNO_2, "202310458", event_id="01JC6HTF3PYB8X8HHCZDGRPY34")
    _usuario(OTRO_DOCENTE, "DOC-UTEC-002", rol="TEACHER", event_id="01JC6HTF3PYB8X8HHCZDGRPY35")
    _llamar("POST", cid, {"userId": ALUMNO_2, "role": "STUDENT"})  # ya estaba
    csv = ("institutionalCode,role,section\n"
           "202310457,STUDENT,Sección 1\n"      # 2: se crea con sección
           "202310458,STUDENT,\n"               # 3: ya matriculado -> skipped
           "DOC-UTEC-002,teacher,\n"            # 4: se crea (rol en minúsculas vale)
           "999999999,STUDENT,\n"               # 5: no existe
           "202310457,STUDENT,\n"               # 6: repetido en el mismo CSV -> skipped
           "202310457,ADMIN,\n"                 # 7: rol inválido
           "202310457,STUDENT,Noche\n"          # 8: sección inexistente
           "\n")
    r = _importar(cid, csv)
    assert r["statusCode"] == 200
    res = _cuerpo(r)
    assert res["created"] == 2 and res["skipped"] == 2
    assert [(e["line"], e["code"]) for e in res["errors"]] == [
        (5, "USUARIO_NO_EXISTE"), (7, "ROL_INVALIDO"), (8, "SECCION_NO_EXISTE")]
    assert res["errors"][0]["institutionalCode"] == "999999999"
    creadas = [e for e in eventos() if e["_detailType"] == CREADA]
    assert len(creadas) == 3  # 1 manual + 2 del CSV
    assert all(esquemas.errores(CREADA, {"metadata": e["metadata"], "data": e["data"]}) == [] for e in creadas)
    con_seccion = [e for e in creadas if e["data"]["userId"] == ALUMNO][0]
    assert con_seccion["data"]["sectionId"].startswith("sec_")
    assert len(items(f"ENROLL#{cid}#")) == 3


def test_csv_grande_en_varios_lotes_y_en_base64(aws):
    cid = _curso()
    filas = []
    for n in range(45):  # 3 transacciones de 20, 20 y 5
        user_id = f"00000000-0000-4000-8000-{n:012d}"
        codigo = f"2026{n:05d}"
        _usuario(user_id, codigo, event_id=f"01JC6HTF3PYB8X8HHCZD{n:06d}")
        filas.append(f"{codigo},STUDENT,")
    csv = "﻿institutionalCode,role,section\r\n" + "\r\n".join(filas)  # con BOM y CRLF, como Excel
    res = _cuerpo(_importar(cid, csv, base64_=True))
    assert res == {"created": 45, "skipped": 0, "errors": []}
    assert len(items(f"ENROLL#{cid}#")) == 45
    assert _cuerpo(_importar(cid, csv))["skipped"] == 45  # reimportar no duplica


def test_csv_invalido_o_con_permisos(aws):
    cid = _curso()
    assert _importar(cid, "codigo,rol\n1,STUDENT\n")["statusCode"] == 400
    assert _importar(cid, "")["statusCode"] == 400
    muchas = "institutionalCode,role,section\n" + "x,STUDENT,\n" * 2001
    assert _importar(cid, muchas)["statusCode"] == 400
    assert _importar(cid, "institutionalCode,role,section\n", headers=token(user=OTRO_DOCENTE))["statusCode"] == 403


# --- El alumno ve sus cursos ------------------------------------------------------


def test_alumno_ve_solo_cursos_publicados_donde_esta_matriculado(aws):
    publicado = _curso(publicar=True)
    borrador = _cuerpo(cursos.lambda_handler(peticion("POST", token(), {"termId": TERM, "code": "CS3333",
                                                                        "title": "Borrador"},
                                                      resource="/courses"), None))["courseId"]
    alumno = token(rol="STUDENT", user=ALUMNO)

    def mis_cursos(query=None):
        r = cursos.lambda_handler(peticion("GET", alumno, query=query, resource="/courses"), None)
        return _cuerpo(r)

    assert mis_cursos()["items"] == []
    _llamar("POST", publicado, {"userId": ALUMNO, "role": "STUDENT"})
    _llamar("POST", borrador, {"userId": ALUMNO, "role": "STUDENT"})
    ids_ = [c["courseId"] for c in mis_cursos()["items"]]
    assert ids_ == [publicado]  # el borrador no se ve
    r = cursos.lambda_handler(peticion("GET", alumno, path_params={"courseId": publicado},
                                       resource="/courses/{courseId}"), None)
    assert r["statusCode"] == 200
    assert mis_cursos({"nextToken": "basura"}) is not None


# --- Consumidor user.provisioned --------------------------------------------------


def test_user_provisioned_guarda_proyecciones_y_es_idempotente(aws):
    assert _usuario(ALUMNO, "202310457") == {"batchItemFailures": []}
    assert _usuario(ALUMNO, "202310457") == {"batchItemFailures": []}  # duplicado: no falla
    proy = items(f"PROY#USER#{ALUMNO}")
    assert len(proy) == 1 and proy[0]["institutionalCode"] == "202310457"
    assert items("PROY#USERCODE#202310457")[0]["userId"] == ALUMNO
    assert items("PROY#USERCODE#", "upc") == []
    cuerpo = json.loads((EJEMPLOS / "lms.users.user.provisioned.v1.json").read_text(encoding="utf-8"))
    # el ejemplo oficial trae el mismo eventId: el inbox lo descarta
    assert inbox.procesar(inbox.leer_mensaje(json.dumps(cuerpo)), consumidor.REACCIONES) == "DUPLICADO"


def test_consumidor_combinado_sigue_procesando_term_started(aws):
    lote = {"Records": [{"messageId": "m1", "body": mensaje_term_started()}]}
    assert consumidor.lambda_handler(lote, None) == {"batchItemFailures": []}
    assert len(items("PROY#TERM#")) == 1


def test_seed_proyecta_usuarios_demo(aws):
    poblar_datos.poblar({"utec": 1, "upc": 1, "pucp": 1})
    assert items("PROY#USERCODE#202310457")[0]["userId"] == "11111111-1111-4111-8111-111111111111"
    assert items("PROY#USERCODE#202311457", "upc")[0]["userId"] == "44444444-4444-4444-8444-444444444444"
    assert items("EVENTO#") == []
