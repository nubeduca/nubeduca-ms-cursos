"""
Consumidor de lms.users.user.provisioned.v1 (ms-usuarios, ACA-02), para ACA-04.

Guarda dos proyecciones del usuario:
- PROY#USER#<userId>            rol y código institucional
- PROY#USERCODE#<código>        código -> userId; la importación CSV de matrículas lo usa

Lector tolerante: solo usa userId, role e institutionalCode. Un usuario sin código
(un ADMIN, por ejemplo) solo deja la primera.
"""

from api.comun import sk_codigo_usuario, sk_usuario
from lib import inbox


def guardar_usuario(evento: inbox.Evento) -> list:
    datos = evento.data
    user_id, codigo = datos["userId"], datos.get("institutionalCode")
    escrituras = [{
        "Put": {
            "Item": {
                "tenant_id": evento.tenant_id,
                "sk": sk_usuario(user_id),
                "entidad": "PROYECCION",
                "userId": user_id,
                "role": datos.get("role"),
                "institutionalCode": codigo,
                "recibidoDe": evento.source,
                "version": evento.aggregate_version,
            }
        }
    }]
    if codigo:
        escrituras.append({
            "Put": {
                "Item": {
                    "tenant_id": evento.tenant_id,
                    "sk": sk_codigo_usuario(codigo),
                    "entidad": "PROYECCION",
                    "userId": user_id,
                    "role": datos.get("role"),
                }
            }
        })
    return escrituras


REACCIONES = {
    "lms.users.user.provisioned.v1": guardar_usuario,
}


def lambda_handler(event, context):
    return inbox.manejar_lote(event, REACCIONES)
