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


def poblar(reparto: dict[str, int] = REPARTO) -> dict[str, int]:
    """Devuelve {tenant: cursos escritos}."""
    escritos = {}
    with t.tabla().batch_writer(overwrite_by_pkeys=["tenant_id", "sk"]) as lote:
        for tenant, cantidad in reparto.items():
            lote.put_item(Item={"tenant_id": tenant, "sk": comun.sk_periodo(TERM_SEED), "entidad": "PROYECCION",
                                "termId": TERM_SEED, "name": "SEED", "startsOn": "2026-08-17",
                                "endsOn": "2026-12-12", "status": "ACTIVE", "seed": True})
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
