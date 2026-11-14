"""
Seed de ms-cursos (ACA-03): 30.000 cursos en la tabla principal, UTEC 15.000, UPC 10.000 y PUCP 5.000.

    python scripts/poblar_datos.py --stage dev            # tabla dev-t_cursos
    python scripts/poblar_datos.py --stage dev --escala 0.01   # 1 %: 150/100/50 para probar
    TABLA=otra-tabla python scripts/poblar_datos.py

Usa las credenciales de AWS del entorno (en nubeduca-backend: source scripts/cuenta.sh).

- NO escribe ítems EVENTO#: cargar 30.000 cursos no debe inundar el bus.
- Es idempotente: los ids son fijos, así que correrlo otra vez reescribe los mismos ítems.
- Cada tenant recibe un periodo de seed activo (PROY#TERM#trm_...SEED) y sus cursos van en él.
- 60 % PUBLISHED, 30 % DRAFT y 10 % ARCHIVED, de 20 docentes de seed por tenant.
- ACA-04: proyecta los usuarios demo del frontend (PROY#USER# y PROY#USERCODE#) para que la
  importación CSV funcione antes de ACA-02. Cuando ms-usuarios publique user.provisioned,
  el consumidor escribe los mismos ítems.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import comun  # noqa: E402
from lib import tabla as t  # noqa: E402

REPARTO = {"utec": 15000, "upc": 10000, "pucp": 5000}
TERM_SEED = "trm_" + "0" * 22 + "SEED"
CREADO_EN = "2026-08-17T13:00:00Z"
ESPACIO = uuid.UUID("6f1c2e8a-3b7d-4c5e-9a10-5eed00000000")
TEMAS = ("Cloud Computing", "Bases de Datos", "Algoritmos", "Redes", "Sistemas Operativos",
         "Ingeniería de Software", "Estadística", "Cálculo", "Física", "Ética Profesional")


def _estado(n: int) -> str:
    resto = n % 10
    return "ARCHIVED" if resto == 9 else "DRAFT" if resto >= 6 else "PUBLISHED"


def _docente(tenant: str, n: int) -> str:
    return str(uuid.uuid5(ESPACIO, f"{tenant}-docente-{n % 20}"))


def _curso(tenant: str, n: int) -> list[dict]:
    course_id = f"crs_{n:026d}"
    code = f"SEED-{n:05d}"
    curso = {
        "tenant_id": tenant, "sk": comun.sk_curso(course_id), "entidad": "COURSE", "version": 1,
        "courseId": course_id, "termId": TERM_SEED, "code": code,
        "title": f"{TEMAS[n % len(TEMAS)]} {n // len(TEMAS) + 1}",
        "description": "Curso de seed para pruebas de carga.",
        "status": _estado(n), "ownerId": _docente(tenant, n), "createdAt": CREADO_EN, "seed": True,
    }
    codigo = {"tenant_id": tenant, "sk": comun.sk_codigo(TERM_SEED, code), "entidad": "COURSECODE",
              "courseId": course_id, "seed": True}
    return [curso, codigo]


# Usuarios demo del frontend (nubeduca-frontend/src/lib/demo-users.ts y mocks/fixtures/users.ts).
USUARIOS_DEMO = {
    "utec": [("11111111-1111-4111-8111-111111111111", "STUDENT", "202310457"),
             ("22222222-2222-4222-8222-222222222222", "TEACHER", "DOC-UTEC-001")],
    "upc": [("44444444-4444-4444-8444-444444444444", "STUDENT", "202311457"),
            ("55555555-5555-4555-8555-555555555555", "TEACHER", "DOC-UPC-001")],
    "pucp": [("77777777-7777-4777-8777-777777777777", "STUDENT", "202312457"),
             ("88888888-8888-4888-8888-888888888888", "TEACHER", "DOC-PUCP-001")],
}


def _usuarios_demo(tenant: str) -> list[dict]:
    items = []
    for user_id, rol, codigo in USUARIOS_DEMO.get(tenant, []):
        items.append({"tenant_id": tenant, "sk": comun.sk_usuario(user_id), "entidad": "PROYECCION",
                      "userId": user_id, "role": rol, "institutionalCode": codigo, "seed": True})
        items.append({"tenant_id": tenant, "sk": comun.sk_codigo_usuario(codigo), "entidad": "PROYECCION",
                      "userId": user_id, "role": rol, "seed": True})
    return items


def poblar(reparto: dict[str, int] = REPARTO) -> dict[str, int]:
    """Devuelve {tenant: cursos escritos}."""
    escritos = {}
    with t.tabla().batch_writer(overwrite_by_pkeys=["tenant_id", "sk"]) as lote:
        for tenant, cantidad in reparto.items():
            lote.put_item(Item={"tenant_id": tenant, "sk": comun.sk_periodo(TERM_SEED), "entidad": "PROYECCION",
                                "termId": TERM_SEED, "name": "SEED", "startsOn": "2026-08-17",
                                "endsOn": "2026-12-12", "status": "ACTIVE", "seed": True})
            for item in _usuarios_demo(tenant):
                lote.put_item(Item=item)
            for n in range(1, cantidad + 1):
                for item in _curso(tenant, n):
                    lote.put_item(Item=item)
            escritos[tenant] = cantidad
    return escritos


def main():
    parser = argparse.ArgumentParser(description="Seed de ms-cursos")
    parser.add_argument("--stage", help="usa la tabla <stage>-t_cursos")
    parser.add_argument("--escala", type=float, default=1.0, help="fracción del reparto (1 = 30.000)")
    args = parser.parse_args()
    if args.stage:
        os.environ["TABLA"] = f"{args.stage}-t_cursos"
    if not os.environ.get("TABLA"):
        raise SystemExit("Pasa --stage o define TABLA.")
    reparto = {k: max(1, int(v * args.escala)) for k, v in REPARTO.items()}
    for tenant, cantidad in poblar(reparto).items():
        print(f"{tenant}: {cantidad} cursos")
    print(f"Total: {sum(reparto.values())} cursos en {os.environ['TABLA']}")


if __name__ == "__main__":
    main()
