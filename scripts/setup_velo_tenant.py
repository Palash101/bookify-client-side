#!/usr/bin/env python3
"""
Migrate Velo data from velo_live_db.sql into PostgreSQL.

Default tenant_id is the string ORG-103 on every row (users, sales, bookings, …).
There is no tenants table — this script never creates tenants or tenant_api_keys.
Velo members → roles.key = user, user_type = client.
Velo trainers → roles.key = trainer, user_type = member.

IMPORTANT
---------
- velo_live_db.sql is a MySQL phpMyAdmin dump — it cannot be loaded with psql directly.
- Default mode reads the .sql file and inserts into PostgreSQL (no MySQL server needed).
- Legacy --use-mysql mode is optional if you prefer importing via local MySQL first.

Prerequisites
-------------
  brew services start postgresql@17

Usage (FULL migration — all tables in one run)
-----
  python scripts/setup_velo_tenant.py --sql-path ~/Downloads/velo_live_db.sql --tenant-id ORG-103 --force-migrate

  If it stops midway: same command with --resume (continues; does not wipe data).

Optional partial commands (only if full run was split on purpose):
  --sales-only | --classes-only | --programs-only | --core-only (not recommended)

All rows use tenant_id from --tenant-id (default ORG-103).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.db.session import SessionLocal  # noqa: E402
from app.core.settings import settings  # noqa: E402

# Fast path: no per-query SQL echo (DEBUG=True would log every INSERT over the network).
migration_engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    echo=False,
)
INSERT_BATCH_SIZE = 5000
BULK_PAGE_SIZE = 5000

# PostgreSQL user_type_enum: member | client | admin
CLIENT_BOOKIFY_USER_TYPE = "client"   # gym members (roles.key = user)
TRAINER_BOOKIFY_USER_TYPE = "member"  # coaches (roles.key = trainer)

# Skip classes/bookings/layout when parsing dump (--core-only) — much faster.
CORE_MIGRATE_TABLES = (
    "roles",
    "model_has_roles",
    "locations",
    "packages",
    "users",
    "wallet_transactions",
    "package_user",
    "transactions",
)

DEFAULT_SQL = Path.home() / "Downloads" / "velo_live_db.sql"
DEFAULT_TENANT_ID = "ORG-103"
DEFAULT_MYSQL_DB = "velo_live_db"
STATE_FILE = Path(__file__).parent / ".velo_migration_state.json"

# Checkpoint labels for full migration (do not use with --core-only unless you finish via --resume).
FULL_MIGRATION_PHASES = ("1a", "1b", "1c", "2", "3", "4", "5_classes", "6_programs")


def _fresh_maps(tenant_id: str) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "completed_phases": [],
        "migration_complete": False,
        "locations": {},
        "fitness_programs": {},
        "users": {},
        "packages": {},
        "package_pricing": {},
        "wallet_transactions": {},
        "user_packages": {},
        "package_user_packages": {},
        "sales": {},
        "class_schedules": {},
        "gym_classes": {},
    }


def _read_migration_state() -> Dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _write_migration_state(maps: Dict[str, Any], *, last_phase: Optional[str] = None, complete: bool = False) -> None:
    if last_phase:
        done = maps.setdefault("completed_phases", [])
        if last_phase not in done:
            done.append(last_phase)
    if complete:
        maps["migration_complete"] = True
    STATE_FILE.write_text(json.dumps(maps, indent=2), encoding="utf-8")


def _phase_done(maps: Dict[str, Any], phase: str) -> bool:
    return phase in maps.get("completed_phases", [])


def _infer_completed_phases_from_db(conn, tenant_id: str) -> List[str]:
    """Guess which migration phases finished (for --resume with old state files)."""
    phases: List[str] = []
    row = conn.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM locations WHERE tenant_id = :tid) AS locs,
              (SELECT count(*) FROM packages WHERE tenant_id = :tid) AS pkgs,
              (SELECT count(*) FROM users WHERE tenant_id = :tid) AS users,
              (SELECT count(*) FROM wallet_transactions wt
                 JOIN users u ON u.id = wt.user_id WHERE u.tenant_id = :tid) AS wtxn,
              (SELECT count(*) FROM user_packages up
                 JOIN users u ON u.id = up.user_id WHERE u.tenant_id = :tid) AS upkg,
              (SELECT count(*) FROM sales WHERE tenant_id = :tid) AS sales,
              (SELECT count(*) FROM class_bookings WHERE tenant_id = :tid) AS bookings,
              (SELECT count(*) FROM training_program_layout WHERE tenant_id = :tid) AS layouts
            """
        ),
        {"tid": tenant_id},
    ).one()
    if row[0]:
        phases.append("1a")
    if row[1]:
        phases.append("1b")
    if row[2]:
        phases.append("1c")
    if row[3]:
        phases.append("2")
    if row[4]:
        phases.append("3")
    if row[5]:
        phases.append("4")
    if row[6]:
        phases.append("5_classes")
    if row[7]:
        phases.append("6_programs")
    return phases


# MySQL role name -> Bookify roles.key
MYSQL_ROLE_TO_BOOKIFY = {
    "super admin": "admin",
    "staff": "staff",
    "user": "user",
    "store manager": "manager",
    "coach": "trainer",
    "receptionist": "staff",
    "doublejoy manager": "manager",
    "lead vxp": "staff",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _apply_fast_session(conn) -> None:
    """Bulk load: commit async to server (safe enough for one-off migration)."""
    conn.execute(text("SET LOCAL synchronous_commit TO off"))


def _bulk_insert_tuples(
    conn,
    sql_with_percent_s: str,
    rows: List[tuple],
    *,
    template: str,
    page_size: int = BULK_PAGE_SIZE,
) -> None:
    if not rows:
        return
    from psycopg2.extras import execute_values

    raw = conn.connection.dbapi_connection
    cur = raw.cursor()
    try:
        execute_values(cur, sql_with_percent_s, rows, template=template, page_size=page_size)
    finally:
        cur.close()


def _executemany(conn, sql: str, batch: List[Dict[str, Any]]) -> None:
    if batch:
        conn.execute(text(sql), batch)


def _bookify_user_params(
    *,
    tenant_id: str,
    user_uuid: str,
    role_id: str,
    user_type: str,
    email: str,
    row: Dict[str, Optional[str]],
) -> Dict[str, Any]:
    dob = row.get("dob")
    if dob in (None, "", "0000-00-00"):
        dob = None
    return {
        "id": user_uuid,
        "tid": tenant_id,
        "rid": role_id,
        "email": email[:500],
        "phone": row.get("phone"),
        "phash": row.get("password"),
        "fn": (row.get("first_name") or "")[:70],
        "ln": (row.get("last_name") or "")[:70],
        "gender": _normalize_user_gender(row.get("gender")),
        "dob": dob,
        "active": str(row.get("status")) == "1",
        "utype": user_type,
        "ca": row.get("created_at"),
        "ua": row.get("updated_at"),
    }


_USER_INSERT_SQL = """
    INSERT INTO users (
        id, tenant_id, role_id, email, phone, password_hash,
        first_name, last_name, gender, dob, is_active, user_type,
        created_at, updated_at
    ) VALUES (
        CAST(:id AS uuid), :tid, CAST(:rid AS uuid), :email, :phone, :phash,
        :fn, :ln, :gender, CAST(:dob AS date), :active, :utype,
        COALESCE(CAST(:ca AS timestamptz), now()),
        COALESCE(CAST(:ua AS timestamptz), now())
    )
"""


_USER_BULK_SQL = """
    INSERT INTO users (
        id, tenant_id, role_id, email, phone, password_hash,
        first_name, last_name, gender, dob, is_active, user_type,
        created_at, updated_at
    ) VALUES %s
"""
_USER_BULK_TEMPLATE = (
    "(%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, "
    "COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now()))"
)


def _insert_bookify_users_batch(conn, batch: List[Dict[str, Any]]) -> None:
    rows = [
        (
            p["id"],
            p["tid"],
            p["rid"],
            p["email"],
            p["phone"],
            p["phash"],
            p["fn"],
            p["ln"],
            p["gender"],
            p["dob"],
            p["active"],
            p["utype"],
            p["ca"],
            p["ua"],
        )
        for p in batch
    ]
    _bulk_insert_tuples(conn, _USER_BULK_SQL, rows, template=_USER_BULK_TEMPLATE)


def _mysql_base_args(mysql_user: str, mysql_password: str, mysql_host: str) -> List[str]:
    args = ["mysql", f"-u{mysql_user}", f"-h{mysql_host}"]
    if mysql_password:
        args.append(f"-p{mysql_password}")
    return args


def run_cmd(cmd: List[str], *, input_file: Optional[Path] = None, timeout: Optional[int] = None) -> None:
    log(f"RUN: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    if input_file:
        with open(input_file, "rb") as fh:
            proc = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=timeout)
    else:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"Command failed ({proc.returncode}): {err}")


def mysql_query(
    sql: str,
    *,
    mysql_user: str,
    mysql_password: str,
    mysql_host: str,
    database: str,
) -> List[str]:
    cmd = _mysql_base_args(mysql_user, mysql_password, mysql_host) + [
        "-N",
        "-B",
        database,
        "-e",
        sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"MySQL query failed: {proc.stderr or proc.stdout}")
    lines = [ln for ln in proc.stdout.strip().split("\n") if ln]
    return lines


def mysql_available(mysql_user: str, mysql_password: str, mysql_host: str) -> bool:
    try:
        run_cmd(_mysql_base_args(mysql_user, mysql_password, mysql_host) + ["-e", "SELECT 1"])
        return True
    except Exception:
        return False


def step_pg_connection_check() -> None:
    log("Checking PostgreSQL connection (Bookify)...")
    with migration_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database(), current_user, version()"
            )
        ).fetchone()
    log(f"  OK → db={row[0]}, user={row[1]}")


def log_migration_tenant(tenant_id: str) -> None:
    """Log target tenant string — stored directly on migrated rows (no tenants table)."""
    log(f"Target tenant_id='{tenant_id}' on all migrated rows (tenants table not used).")


def _db_table_exists(conn, table_name: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = :t
                )
                """
            ),
            {"t": table_name},
        ).scalar()
    )


def _resolve_bookify_role_ids(conn) -> Tuple[str, str, str]:
    """Return (user_role_id, trainer_role_id, fallback_role_id) as strings."""
    role_rows = conn.execute(text("SELECT id, key FROM roles WHERE key IS NOT NULL")).fetchall()
    role_by_key = {r[1]: str(r[0]) for r in role_rows}
    user_role_id = role_by_key.get("user") or role_by_key.get("member")
    trainer_role_id = role_by_key.get("trainer") or role_by_key.get("coach")
    fallback = user_role_id or trainer_role_id or (str(role_rows[0][0]) if role_rows else None)
    if not user_role_id:
        raise RuntimeError("No 'user' (or 'member') role in PostgreSQL roles table.")
    if not trainer_role_id:
        raise RuntimeError("No 'trainer' role in PostgreSQL roles table.")
    return user_role_id, trainer_role_id, fallback


def _load_velo_users_index(inserts: Dict[str, List[str]]) -> Dict[str, Dict[str, Optional[str]]]:
    """All Velo users rows by MySQL id (including rows without email)."""
    index: Dict[str, Dict[str, Optional[str]]] = {}
    for stmt in inserts.get("users", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            if row.get("deleted_at"):
                continue
            index[str(row.get("id"))] = row
    return index


def _collect_trainer_mysql_ids(
    inserts: Dict[str, List[str]],
    user_roles: Dict[str, str],
    role_names: Dict[str, str],
) -> set:
    """MySQL user ids that should be Bookify trainers."""
    trainer_mids = set()
    trainer_role_names = {"coach", "trainer", "instructor"}

    for stmt in inserts.get("classes", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            tid = row.get("trainer_id")
            if tid and str(tid) not in ("0", ""):
                trainer_mids.add(str(tid))

    for user_mid, mysql_rid in user_roles.items():
        rname = (role_names.get(mysql_rid) or "").strip().lower()
        if rname in trainer_role_names or MYSQL_ROLE_TO_BOOKIFY.get(rname) == "trainer":
            trainer_mids.add(str(user_mid))

    return trainer_mids


def _normalize_user_gender(raw: Optional[str]) -> str:
    gender = (raw or "Male").lower()
    if gender not in ("male", "female", "other"):
        return "male"
    return gender


def _insert_bookify_user(
    conn,
    *,
    tenant_id: str,
    user_uuid: str,
    role_id: str,
    user_type: str,
    email: str,
    row: Dict[str, Optional[str]],
) -> None:
    _insert_bookify_users_batch(
        conn,
        [
            _bookify_user_params(
                tenant_id=tenant_id,
                user_uuid=user_uuid,
                role_id=role_id,
                user_type=user_type,
                email=email,
                row=row,
            )
        ],
    )


def _migrate_velo_trainers(
    conn,
    tenant_id: str,
    inserts: Dict[str, List[str]],
    maps: Dict[str, Any],
    user_roles: Dict[str, str],
    role_names: Dict[str, str],
    trainer_role_id: str,
) -> Tuple[int, int, int]:
    """Ensure every Velo trainer exists in users with roles.key = trainer (user_type = member)."""
    velo_users = _load_velo_users_index(inserts)
    trainer_mids = _collect_trainer_mysql_ids(inserts, user_roles, role_names)
    inserted = 0
    updated = 0
    skipped = 0

    for mid in sorted(trainer_mids, key=lambda x: int(x) if x.isdigit() else x):
        row = velo_users.get(mid)
        if not row:
            skipped += 1
            continue

        email = (row.get("email") or "").strip().lower()
        if not email:
            email = f"trainer+{mid}@{tenant_id.lower()}.import.local"

        existing_uuid = maps["users"].get(mid)
        if existing_uuid:
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET role_id = CAST(:rid AS uuid),
                        user_type = :utype,
                        email = COALESCE(NULLIF(email, ''), :email),
                        first_name = COALESCE(:fn, first_name),
                        last_name = COALESCE(:ln, last_name),
                        updated_at = COALESCE(CAST(:ua AS timestamptz), now())
                    WHERE id = CAST(:uid AS uuid) AND tenant_id = :tid
                    """
                ),
                {
                    "rid": trainer_role_id,
                    "utype": TRAINER_BOOKIFY_USER_TYPE,
                    "uid": existing_uuid,
                    "tid": tenant_id,
                    "email": email[:500],
                    "fn": (row.get("first_name") or "")[:70] or None,
                    "ln": (row.get("last_name") or "")[:70] or None,
                    "ua": row.get("updated_at"),
                },
            )
            updated += 1
            continue

        user_uuid = str(uuid.uuid4())
        maps["users"][mid] = user_uuid
        _insert_bookify_user(
            conn,
            tenant_id=tenant_id,
            user_uuid=user_uuid,
            role_id=trainer_role_id,
            user_type=TRAINER_BOOKIFY_USER_TYPE,
            email=email,
            row=row,
        )
        inserted += 1

    return inserted, updated, skipped


def step_import_mysql_dump(
    sql_path: Path,
    mysql_db: str,
    mysql_user: str,
    mysql_password: str,
    mysql_host: str,
    *,
    force_reimport: bool,
) -> None:
    log(f"Importing MySQL dump ({sql_path.stat().st_size / 1e6:.0f} MB) — may take several minutes...")
    if not sql_path.is_file():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    head = sql_path.read_text(encoding="utf-8", errors="ignore")[:500]
    if "phpMyAdmin" not in head and "MySQL" not in head:
        log("  WARNING: file does not look like a MySQL/phpMyAdmin dump.")

    base = _mysql_base_args(mysql_user, mysql_password, mysql_host)
    run_cmd(
        base
        + [
            "-e",
            f"CREATE DATABASE IF NOT EXISTS `{mysql_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
        ],
    )

    if force_reimport:
        run_cmd(
            base
            + [
                "-e",
                f"DROP DATABASE IF EXISTS `{mysql_db}`; CREATE DATABASE `{mysql_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            ],
        )

    # Large import — allow up to 2 hours
    run_cmd(base + [mysql_db], input_file=sql_path, timeout=7200)

    tables = mysql_query(
        "SHOW TABLES;",
        mysql_user=mysql_user,
        mysql_password=mysql_password,
        mysql_host=mysql_host,
        database=mysql_db,
    )
    log(f"  MySQL import done. Tables: {len(tables)}")


def _parse_tsv_rows(lines: List[str]) -> List[List[str]]:
    return [ln.split("\t") for ln in lines if ln.strip()]


# ---------------------------------------------------------------------------
# Direct SQL file → PostgreSQL (no MySQL server)
# ---------------------------------------------------------------------------

INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+`(\w+)`\s*\(([^)]+)\)\s*VALUES\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def _unquote_mysql(val: str) -> Optional[str]:
    val = val.strip()
    if val.upper() == "NULL":
        return None
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        inner = val[1:-1]
        return inner.replace("\\'", "'").replace("\\\\", "\\")
    return val


def _split_mysql_row_fields(row_inner: str) -> List[Optional[str]]:
    fields: List[Optional[str]] = []
    buf: List[str] = []
    in_str = False
    escape = False
    i = 0
    while i < len(row_inner):
        ch = row_inner[i]
        if in_str:
            if escape:
                buf.append(ch)
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_str = False
            else:
                buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_str = True
            i += 1
            continue
        if ch == ",":
            fields.append(_unquote_mysql("".join(buf)))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    fields.append(_unquote_mysql("".join(buf)))
    return fields


def _split_mysql_rows(values_blob: str) -> List[List[Optional[str]]]:
    rows: List[List[Optional[str]]] = []
    depth = 0
    in_str = False
    escape = False
    start = 0
    for i, ch in enumerate(values_blob):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
            continue
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                rows.append(_split_mysql_row_fields(values_blob[start:i]))
    return rows


def _parse_mysql_insert(stmt: str) -> Tuple[str, List[str], List[List[Optional[str]]]]:
    m = INSERT_RE.search(stmt)
    if not m:
        raise ValueError("Not a MySQL INSERT statement")
    table = m.group(1)
    columns = [c.strip().strip("`") for c in m.group(2).split(",")]
    values_blob = m.group(3).rstrip().rstrip(";").strip()
    return table, columns, _split_mysql_rows(values_blob)


def _rows_as_dicts(columns: List[str], rows: List[List[Optional[str]]]) -> List[Dict[str, Optional[str]]]:
    return [dict(zip(columns, row)) for row in rows]


MIGRATE_TABLES = (
    "roles",
    "model_has_roles",
    "locations",
    "packages",
    "users",
    "wallet_transactions",
    "package_user",
    "transactions",
    "classes",
    "bookings",
    "layout",
)


def collect_mysql_inserts(
    sql_path: Path,
    *,
    tables: Optional[Tuple[str, ...]] = None,
) -> Dict[str, List[str]]:
    """Single pass over the dump — collect INSERT blocks for needed tables."""
    use_tables = tables or MIGRATE_TABLES
    collected: Dict[str, List[str]] = {t: [] for t in use_tables}
    needles = {t: f"INSERT INTO `{t}`" for t in use_tables}
    active: Optional[str] = None
    buffer: List[str] = []
    mb = 0

    with open(sql_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            mb += len(line.encode("utf-8", errors="ignore"))
            if mb % (100 * 1024 * 1024) < len(line):
                log(f"  reading SQL file… {mb / 1e6:.0f} MB")

            matched = next((t for t, n in needles.items() if n in line), None)
            if matched:
                if active and buffer:
                    collected[active].append("".join(buffer))
                active = matched
                buffer = [line]
                continue

            if active:
                buffer.append(line)
                if line.rstrip().endswith(";"):
                    collected[active].append("".join(buffer))
                    active = None
                    buffer = []

    if active and buffer:
        collected[active].append("".join(buffer))

    for t in MIGRATE_TABLES:
        log(f"  found `{t}` INSERT blocks: {len(collected[t])}")
    return collected


def _load_mysql_reference_maps(inserts: Dict[str, List[str]]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """MySQL role id -> name; user id -> mysql role id."""
    role_names: Dict[str, str] = {}
    user_roles: Dict[str, str] = {}

    for stmt in inserts.get("roles", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            role_names[str(row["id"])] = (row.get("name") or "User").strip()

    for stmt in inserts.get("model_has_roles", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            mtype = row.get("model_type") or ""
            if "User" not in mtype:
                continue
            user_roles[str(row["model_id"])] = str(row["role_id"])

    return role_names, user_roles


def _velo_txn_payment_source(row: Dict[str, Optional[str]]) -> Optional[str]:
    """Map Velo transactions row -> sales.payment_source."""
    ttype = (row.get("type") or "").strip()
    order_id = (row.get("order_id") or "").strip()
    purchase = (row.get("PurchaseType") or "").strip().lower()

    if order_id.startswith("PUW") or (ttype == "Wallet" and not row.get("package_user_id")):
        return "wallet_add"
    if order_id.startswith("PKG") or ttype == "Package":
        if purchase == "wallet":
            return "package_wallet"
        return "package_gateway"
    if ttype == "Wallet":
        return "wallet_add"
    return None


def _velo_txn_gateway(row: Dict[str, Optional[str]]) -> str:
    purchase = (row.get("PurchaseType") or "Admin").strip().lower()
    if "visa" in purchase or "master" in purchase or "apple" in purchase:
        return "stripe"
    if purchase == "wallet":
        return "wallet"
    if "qnb" in purchase:
        return "qnb"
    return "velo_legacy"


def _parse_velo_time(raw: Optional[str]) -> Optional[time]:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _normalize_velo_times(
    start_raw: Optional[str], end_raw: Optional[str]
) -> Tuple[Optional[time], Optional[time]]:
    """Ensure end_time > start_time (DB constraint chk_time_valid)."""
    st = _parse_velo_time(start_raw)
    et = _parse_velo_time(end_raw)
    if st is None:
        return None, None
    if et is None or et <= st:
        et = (datetime.combine(date.today(), st) + timedelta(minutes=45)).time()
    return st, et


def _velo_price_type_booking_type(price_type: Optional[str]) -> str:
    """Values must match PostgreSQL booking_type_enum: price | free | packages."""
    pt = (price_type or "").strip()
    if pt == "Free":
        return "free"
    if pt == "Amount":
        return "price"
    return "packages"


def _velo_booking_payment_mode(btype: Optional[str]) -> Optional[str]:
    t = (btype or "").strip()
    if t == "Wallet":
        return "wallet"
    if t == "Package":
        return "package"
    if t == "Gym":
        return "free"
    if t == "Prepaid":
        return "package"
    return "package" if t else None


def _velo_booking_status(raw: Optional[str]) -> str:
    m = {
        "Booked": "confirmed",
        "Cancelled": "cancelled",
        "Waiting": "waiting",
        "Hold": "pending",
    }
    return m.get((raw or "").strip(), "cancelled")


def _velo_gender_restriction(raw: Optional[str]) -> str:
    """Values must match PostgreSQL gender_enum: ladies | mixed | male | female."""
    if (raw or "").strip() == "Female":
        return "ladies"
    return "mixed"


def _velo_class_gender_fp(raw: Optional[str]) -> str:
    """fitness_programs.gender_restriction column (varchar)."""
    if (raw or "").strip() == "Female":
        return "female"
    return "mixed"


def _velo_layout_status(raw: Optional[str]) -> str:
    if (raw or "").strip().lower() == "active":
        return "active"
    return "inactive"


def _velo_description_to_layout_json(
    desc: Optional[str],
    admin_desc: Optional[str],
    max_seats: Optional[str],
) -> Dict[str, Any]:
    """Convert Velo layout.description JSON → Bookify mobile_app_json shape."""
    for raw in (desc, admin_desc):
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        seats_raw = data.get("seat") or data.get("seats") or []
        seats: List[Dict[str, Any]] = []
        if isinstance(seats_raw, list):
            for s in seats_raw:
                if not isinstance(s, dict):
                    continue
                num = s.get("num") or s.get("id")
                if num is None:
                    continue
                label = str(num)
                seats.append(
                    {
                        "id": label,
                        "text": label,
                        "x": int(s.get("x") or 0),
                        "y": int(s.get("y") or 0),
                        "size": 48,
                        "style": "circle",
                        "status": "available",
                    }
                )
        if seats:
            total = int(max_seats) if max_seats and str(max_seats).isdigit() else len(seats)
            out: Dict[str, Any] = {"seats": seats, "totalSeats": total}
            if data.get("stage"):
                out["stage"] = data.get("stage")
            return out
    total = int(max_seats) if max_seats and str(max_seats).isdigit() else 0
    return {"seats": [], "totalSeats": total}


def _migrate_velo_fitness_programs_and_layouts(
    tenant_id: str,
    inserts: Dict[str, List[str]],
    maps: Dict[str, Any],
) -> Tuple[int, int, int]:
    """
    Enrich location programs, import Velo classes as fitness_programs,
    and import Velo layout → training_program_layout.
    """
    maps.setdefault("fitness_programs_by_class", {})
    maps.setdefault("training_program_layouts", {})

    loc_rows: Dict[str, Dict[str, Optional[str]]] = {}
    for stmt in inserts.get("locations", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            if row.get("deleted_at"):
                continue
            loc_rows[str(row.get("id"))] = row

    layout_to_location: Dict[str, str] = {}
    for loc_mid, row in loc_rows.items():
        lid = row.get("layout_id")
        if lid and str(lid) not in ("", "0", "NULL"):
            layout_to_location[str(lid)] = loc_mid

    updated_loc = 0
    with migration_engine.begin() as conn:
        for loc_mid, row in loc_rows.items():
            fp_id = maps.get("fitness_programs", {}).get(loc_mid)
            loc_uuid = maps.get("locations", {}).get(loc_mid)
            if not fp_id or not loc_uuid:
                continue
            layout_mid = row.get("layout_id")
            conn.execute(
                text(
                    """
                    UPDATE fitness_programs SET
                        name = :name,
                        description = :desc,
                        image_url = :img,
                        spot_name = :spot,
                        show_spots_left = :show_left,
                        spots_left_label = :left_label,
                        is_layout_required = :layout_req,
                        is_active = :active,
                        updated_at = COALESCE(CAST(:ua AS timestamptz), now())
                    WHERE id = :fpid AND tenant_id = :tid
                    """
                ),
                {
                    "fpid": int(fp_id),
                    "tid": tenant_id,
                    "name": (row.get("name") or "Program")[:255],
                    "desc": f"Velo location {row.get('name') or ''}".strip()[:2000],
                    "img": row.get("image"),
                    "spot": (row.get("spot_name") or "Spot")[:50],
                    "show_left": bool(row.get("leftSpot")),
                    "left_label": (row.get("leftSpot") or "")[:50] or None,
                    "layout_req": bool(layout_mid and str(layout_mid) not in ("0", "")),
                    "active": str(row.get("status")) == "1",
                    "ua": row.get("updated_at"),
                },
            )
            updated_loc += 1

    class_count = 0
    class_skip = 0
    pending: List[Dict[str, Any]] = []
    pos = 0

    def _flush_class_programs(rows: List[Dict[str, Any]]) -> None:
        nonlocal class_count
        if not rows:
            return
        with migration_engine.begin() as conn:
            for p in rows:
                fp_id = conn.execute(
                    text(
                        """
                        INSERT INTO fitness_programs (
                            tenant_id, location_id, name, description, image_url,
                            is_active, training_mode, gender_restriction,
                            is_layout_required, spot_name, display_position,
                            created_at, updated_at
                        ) VALUES (
                            :tid, CAST(:lid AS uuid), :name, :desc, :img,
                            :active, 'one_to_many', :gender,
                            :layout_req, :spot, :pos,
                            COALESCE(CAST(:ca AS timestamptz), now()),
                            COALESCE(CAST(:ua AS timestamptz), now())
                        )
                        RETURNING id
                        """
                    ),
                    p,
                ).scalar()
                maps["fitness_programs_by_class"][p["class_mid"]] = int(fp_id)
        class_count += len(rows)
        if class_count % 5000 < len(rows):
            log(f"    fitness_programs (from classes): {class_count}")

    for stmt in inserts.get("classes", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            if row.get("deleted_at") or str(row.get("status")) != "1":
                class_skip += 1
                continue
            loc_mid = str(row.get("location_id") or "")
            loc_uuid = maps.get("locations", {}).get(loc_mid)
            if not loc_uuid:
                class_skip += 1
                continue
            class_mid = str(row.get("id"))
            if class_mid in maps["fitness_programs_by_class"]:
                continue
            pos += 1
            ctype = row.get("type") or ""
            title = (row.get("title") or "Class").strip()
            unique_name = f"{title} [{class_mid}]"[:255]
            pending.append(
                {
                    "class_mid": class_mid,
                    "tid": tenant_id,
                    "lid": loc_uuid,
                    "name": unique_name,
                    "desc": (row.get("description") or f"{ctype} program")[:5000],
                    "img": row.get("image") or row.get("banner"),
                    "active": True,
                    "gender": _velo_class_gender_fp(row.get("gender")),
                    "layout_req": bool(row.get("layout_id")),
                    "spot": (loc_rows.get(loc_mid, {}).get("spot_name") or "Spot")[:50],
                    "pos": pos,
                    "ca": row.get("created_at"),
                    "ua": row.get("updated_at"),
                }
            )
            if len(pending) >= 1000:
                _flush_class_programs(pending)
                pending = []
    _flush_class_programs(pending)
    log(f"  fitness_programs from Velo classes: {class_count} (skipped {class_skip})")

    layout_count = 0
    layout_skip = 0
    default_fp = next(iter((maps.get("fitness_programs") or {}).values()), None)
    with migration_engine.begin() as conn:
        for stmt in inserts.get("layout", []):
            _, cols, rows = _parse_mysql_insert(stmt)
            for row in _rows_as_dicts(cols, rows):
                if row.get("deleted_at"):
                    layout_skip += 1
                    continue
                layout_mid = str(row.get("id"))
                loc_mid = layout_to_location.get(layout_mid)
                program_id = maps.get("fitness_programs", {}).get(loc_mid) if loc_mid else None
                if program_id is None:
                    program_id = default_fp
                if program_id is None:
                    layout_skip += 1
                    continue
                mobile_json = _velo_description_to_layout_json(
                    row.get("description"),
                    row.get("admin_description"),
                    row.get("max_seats"),
                )
                desktop_json = (
                    _velo_description_to_layout_json(
                        row.get("admin_description"),
                        None,
                        row.get("max_seats"),
                    )
                    if row.get("admin_description")
                    else None
                )
                layout_uuid = str(uuid.uuid4())
                conn.execute(
                    text(
                        """
                        INSERT INTO training_program_layout (
                            id, program_id, tenant_id, layout_name,
                            mobile_app_json, desktop_json, status, max_seat
                        ) VALUES (
                            CAST(:id AS uuid), :pid, :tid, :lname,
                            CAST(:mobile AS jsonb), CAST(:desktop AS jsonb),
                            CAST(:st AS layout_status_enum), :max_seat
                        )
                        """
                    ),
                    {
                        "id": layout_uuid,
                        "pid": int(program_id),
                        "tid": tenant_id,
                        "lname": (row.get("name") or "Layout")[:255],
                        "mobile": json.dumps(mobile_json),
                        "desktop": json.dumps(desktop_json) if desktop_json else None,
                        "st": _velo_layout_status(row.get("status")),
                        "max_seat": float(row.get("max_seats") or 0) if row.get("max_seats") else None,
                    },
                )
                maps["training_program_layouts"][layout_mid] = layout_uuid
                layout_count += 1
    log(f"  training_program_layout: {layout_count} (skipped {layout_skip})")
    return updated_loc, class_count, layout_count


def step_migrate_programs_only(tenant_id: str, sql_path: Path) -> None:
    if not STATE_FILE.is_file():
        raise RuntimeError(f"Missing {STATE_FILE} — run full migration first.")
    maps = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not maps.get("locations") or not maps.get("fitness_programs"):
        raise RuntimeError("State file missing location/program maps — run full migration first.")

    log("Migrating Velo locations/classes/layout → fitness_programs + training_program_layout...")
    with migration_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM training_program_layout WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
    inserts = collect_mysql_inserts(sql_path)
    upd, cls_n, lay_n = _migrate_velo_fitness_programs_and_layouts(tenant_id, inserts, maps)
    log(f"  location programs updated: {upd}, class programs: {cls_n}, layouts: {lay_n}")
    STATE_FILE.write_text(json.dumps(maps, indent=2), encoding="utf-8")
    log(f"  ID maps updated → {STATE_FILE}")


def _velo_session_key(classes_id: Optional[str], bdate: Optional[str], start: Optional[str], end: Optional[str]) -> str:
    return f"{classes_id}|{bdate}|{start}|{end}"


def _fallback_trainer_uuid(maps: Dict[str, Any], templates: Dict[str, Dict[str, Optional[str]]]) -> Optional[str]:
    for row in templates.values():
        uid = maps.get("users", {}).get(str(row.get("trainer_id")))
        if uid:
            return uid
    users = maps.get("users") or {}
    return next(iter(users.values()), None)


def _load_velo_class_templates(inserts: Dict[str, List[str]]) -> Dict[str, Dict[str, Optional[str]]]:
    templates: Dict[str, Dict[str, Optional[str]]] = {}
    for stmt in inserts.get("classes", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            templates[str(row.get("id"))] = row
    return templates


def _clear_velo_class_tables(conn, tenant_id: str) -> None:
    conn.execute(text("DELETE FROM class_bookings WHERE tenant_id = :tid"), {"tid": tenant_id})
    conn.execute(
        text(
            """
            DELETE FROM gym_classes gc
            USING fitness_programs fp
            WHERE gc.training_programme_id = fp.id AND fp.tenant_id = :tid
            """
        ),
        {"tid": tenant_id},
    )
    conn.execute(
        text(
            """
            DELETE FROM class_schedules cs
            WHERE cs.created_by IN (SELECT id FROM users WHERE tenant_id = :tid)
            """
        ),
        {"tid": tenant_id},
    )


def _migrate_velo_class_schedules(
    conn,
    maps: Dict[str, Any],
    templates: Dict[str, Dict[str, Optional[str]]],
) -> int:
    maps.setdefault("class_schedules", {})
    count = 0
    total = len(templates)
    for mid, row in templates.items():
        if mid in maps["class_schedules"]:
            continue
        if row.get("deleted_at"):
            continue
        if str(row.get("status")) != "1":
            continue
        trainer_uid = maps["users"].get(str(row.get("trainer_id")))
        title = (row.get("title") or "Class")[:255]
        sched_id = conn.execute(
            text(
                """
                INSERT INTO class_schedules (name, status, created_by, created_at, updated_at)
                VALUES (
                    :name, 'active', CAST(:cby AS uuid),
                    COALESCE(CAST(:ca AS timestamptz), now()),
                    COALESCE(CAST(:ua AS timestamptz), now())
                )
                RETURNING id
                """
            ),
            {
                "name": title,
                "cby": trainer_uid,
                "ca": row.get("created_at"),
                "ua": row.get("updated_at"),
            },
        ).scalar()
        maps["class_schedules"][mid] = int(sched_id)
        count += 1
        if count % 2000 == 0:
            log(f"    class_schedules: {count}/{total}")
    return count


def _ensure_gym_class(
    conn,
    maps: Dict[str, Any],
    templates: Dict[str, Dict[str, Optional[str]]],
    session_key: str,
    classes_id: str,
    bdate: str,
    start_raw: str,
    end_raw: str,
    gym_sessions: Dict[str, str],
    fallback_trainer: Optional[str],
) -> Optional[str]:
    if session_key in gym_sessions:
        return gym_sessions[session_key]
    tpl = templates.get(classes_id)
    if not tpl:
        return None
    loc_mid = str(tpl.get("location_id") or "")
    fp_id = maps.get("fitness_programs", {}).get(loc_mid)
    if fp_id is None:
        return None
    sched_id = maps.get("class_schedules", {}).get(classes_id)
    trainer_uid = maps["users"].get(str(tpl.get("trainer_id"))) or fallback_trainer
    if not trainer_uid:
        return None
    seats = tpl.get("seats")
    max_bookings = int(seats) if seats and str(seats).isdigit() else None
    waiting = tpl.get("waiting_allowed")
    max_waitings = int(waiting) if waiting and str(waiting).isdigit() else 0
    price_raw = tpl.get("price")
    price = float(price_raw) if price_raw and str(price_raw).replace(".", "").isdigit() else None
    st, et = _normalize_velo_times(start_raw, end_raw)
    if st is None:
        return None
    class_uuid = str(uuid.uuid4())
    gym_sessions[session_key] = class_uuid
    maps.setdefault("gym_classes", {})[session_key] = class_uuid
    conn.execute(
        text(
            """
            INSERT INTO gym_classes (
                id, training_programme_id, title, theme_name, trainer_id,
                class_date, start_time, end_time, max_bookings, max_waitings,
                booking_counts, attendance_count, booking_type, price, gender,
                status, schedule_id, layout_id, created_at, updated_at
            ) VALUES (
                CAST(:id AS uuid), :fpid, :title, :theme, CAST(:tid AS uuid),
                CAST(:cdate AS date), CAST(:st AS time), CAST(:et AS time),
                :maxb, :maxw, 0, 0, CAST(:btype AS booking_type_enum), :price,
                CAST(:gender AS gender_enum),
                CAST('published' AS class_status_enum), :sched, :layout,
                COALESCE(CAST(:ca AS timestamptz), now()),
                COALESCE(CAST(:ua AS timestamptz), now())
            )
            """
        ),
        {
            "id": class_uuid,
            "fpid": int(fp_id),
            "title": (tpl.get("title") or "Class")[:255],
            "theme": (tpl.get("theme_name") or "")[:255] or None,
            "tid": trainer_uid,
            "cdate": bdate,
            "st": st,
            "et": et,
            "maxb": max_bookings,
            "maxw": max_waitings,
            "btype": _velo_price_type_booking_type(tpl.get("priceType")),
            "price": price,
            "gender": _velo_gender_restriction(tpl.get("gender")),
            "sched": sched_id,
            "layout": None,
            "ca": tpl.get("created_at"),
            "ua": tpl.get("updated_at"),
        },
    )
    return class_uuid


def _migrate_velo_classes_and_bookings(
    tenant_id: str,
    inserts: Dict[str, List[str]],
    maps: Dict[str, Any],
) -> Tuple[int, int, int, int]:
    """
    Velo classes → class_schedules; bookings → gym_classes + class_bookings.
    Uses batched commits (large booking volume).
    """
    templates = _load_velo_class_templates(inserts)
    n_booking_blocks = len(inserts.get("bookings", []))
    log(f"  Velo class templates loaded: {len(templates)}")
    log(
        f"  Phase 5 may take 2–6+ hours ({n_booking_blocks} booking INSERT blocks, "
        "~800k rows). Next log lines show progress — do not stop unless frozen 30+ min."
    )

    sched_count = 0
    gym_count = 0
    booking_count = 0
    booking_skip = 0

    log("  Clearing prior class_bookings / gym_classes / class_schedules...")
    with migration_engine.begin() as conn:
        _apply_fast_session(conn)
        _clear_velo_class_tables(conn, tenant_id)
    log(f"  Inserting class_schedules (~{len(templates)} templates)...")
    with migration_engine.begin() as conn:
        _apply_fast_session(conn)
        sched_count = _migrate_velo_class_schedules(conn, maps, templates)
    log(f"  class_schedules: {sched_count}")

    # MySQL package_user id -> PG user_packages.id; then resolve sale_id below
    mysql_up_to_pg: Dict[str, str] = maps.get("user_packages", {})
    with migration_engine.connect() as conn:
        pg_up_rows = conn.execute(
            text(
                """
                SELECT up.id, up.sale_id
                FROM user_packages up
                JOIN users u ON u.id = up.user_id
                WHERE u.tenant_id = :tid
                """
            ),
            {"tid": tenant_id},
        )
        pg_up_sale = {str(r[0]): str(r[1]) if r[1] else None for r in pg_up_rows}
    mysql_up_to_sale: Dict[str, Optional[str]] = {}
    for mysql_id, pg_up in mysql_up_to_pg.items():
        mysql_up_to_sale[mysql_id] = pg_up_sale.get(pg_up)

    gym_sessions: Dict[str, str] = {}
    fallback_trainer = _fallback_trainer_uuid(maps, templates)
    if not fallback_trainer:
        raise RuntimeError("No trainer user available for gym_classes migration.")

    log("  Pass 1: scan bookings → build gym_classes (slowest step)...")
    batch: List[Tuple[str, str, str, str, str]] = []
    seen_sessions = 0
    rows_scanned = 0
    gym_batch_size = 2000
    for bi, stmt in enumerate(inserts.get("bookings", [])):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            rows_scanned += 1
            if rows_scanned % 250000 == 0:
                log(
                    f"    Pass 1 scan: {rows_scanned} booking rows, "
                    f"{len(gym_sessions)} unique sessions, block {bi + 1}/{n_booking_blocks}"
                )
            cid = str(row.get("classes_id") or "")
            bdate = row.get("date")
            if not cid or not bdate:
                continue
            sk = _velo_session_key(cid, bdate, row.get("start_time"), row.get("end_time"))
            if sk in gym_sessions:
                continue
            batch.append((sk, cid, bdate, row.get("start_time") or "", row.get("end_time") or ""))
            if len(batch) >= gym_batch_size:
                with migration_engine.begin() as conn:
                    _apply_fast_session(conn)
                    for item in batch:
                        if _ensure_gym_class(
                            conn, maps, templates, item[0], item[1], item[2], item[3], item[4],
                            gym_sessions, fallback_trainer,
                        ):
                            seen_sessions += 1
                batch = []
                if seen_sessions and seen_sessions % 2000 == 0:
                    log(f"    gym_classes inserted: {seen_sessions} (scanned {rows_scanned} rows)")
    if batch:
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            for item in batch:
                if _ensure_gym_class(
                    conn, maps, templates, item[0], item[1], item[2], item[3], item[4],
                    gym_sessions, fallback_trainer,
                ):
                    seen_sessions += 1
    gym_count = len(gym_sessions)
    log(f"  Pass 1 done — gym_classes: {gym_count} (scanned {rows_scanned} booking rows)")

    log("  Pass 2: class_bookings (batched, ~800k inserts)...")
    pending_rows: List[Dict[str, Any]] = []

    _booking_sql = """
        INSERT INTO class_bookings (
            id, tenant_id, user_id, class_id, seat_id, status,
            booked_at, confirmed_at, cancelled_at, payment_mode,
            user_package_id, order_id, checkin_time, created_at, updated_at
        ) VALUES (
            CAST(:id AS uuid), :tid, CAST(:uid AS uuid), CAST(:cid AS uuid),
            :seat, :st,
            COALESCE(CAST(:ba AS timestamptz), now()),
            CASE WHEN :st = 'confirmed' THEN COALESCE(CAST(:ba AS timestamptz), now()) ELSE NULL END,
            CASE WHEN :st = 'cancelled' THEN COALESCE(CAST(:ua AS timestamptz), now()) ELSE NULL END,
            :pmode, CAST(:sale AS uuid), :oref,
            CASE WHEN :chk THEN COALESCE(CAST(:ua AS timestamptz), now()) ELSE NULL END,
            COALESCE(CAST(:ca AS timestamptz), now()),
            COALESCE(CAST(:ua AS timestamptz), now())
        )
    """

    def _flush_bookings(rows: List[Dict[str, Any]]) -> None:
        nonlocal booking_count
        if not rows:
            return
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            _executemany(conn, _booking_sql, rows)
        booking_count += len(rows)
        log(f"    class_bookings: {booking_count}")

    pass2_scanned = 0
    for bi, stmt in enumerate(inserts.get("bookings", [])):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            pass2_scanned += 1
            if pass2_scanned % 250000 == 0:
                log(
                    f"    Pass 2 scan: {pass2_scanned} rows, {booking_count} inserted, "
                    f"block {bi + 1}/{n_booking_blocks}"
                )
            uid = maps["users"].get(str(row.get("user_id")))
            if not uid:
                booking_skip += 1
                continue
            cid = str(row.get("classes_id") or "")
            bdate = row.get("date")
            if not cid or not bdate:
                booking_skip += 1
                continue
            sk = _velo_session_key(cid, bdate, row.get("start_time"), row.get("end_time"))
            class_uuid = gym_sessions.get(sk)
            if not class_uuid:
                booking_skip += 1
                continue
            status = _velo_booking_status(row.get("status"))
            if (row.get("status") or "").strip() == "Hold":
                booking_skip += 1
                continue
            up_mysql = str(row.get("user_package_id") or "")
            sale_id = mysql_up_to_sale.get(up_mysql) if up_mysql and up_mysql != "0" else None
            attended = (row.get("attended") or "").strip() == "Yes"
            pending_rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "tid": tenant_id,
                    "uid": uid,
                    "cid": class_uuid,
                    "seat": row.get("seat_text"),
                    "st": status,
                    "ba": row.get("created_at"),
                    "ua": row.get("updated_at"),
                    "pmode": _velo_booking_payment_mode(row.get("type")),
                    "sale": sale_id,
                    "oref": row.get("booking_ref"),
                    "chk": attended,
                    "ca": row.get("created_at"),
                }
            )
            if len(pending_rows) >= 5000:
                _flush_bookings(pending_rows)
                pending_rows = []

    _flush_bookings(pending_rows)

    return sched_count, gym_count, booking_count, booking_skip


def step_migrate_classes_only(tenant_id: str, sql_path: Path) -> None:
    if not STATE_FILE.is_file():
        raise RuntimeError(f"Missing {STATE_FILE} — run full migration first.")
    maps = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not maps.get("users") or not maps.get("fitness_programs"):
        raise RuntimeError("State file missing users/fitness_programs maps — run full migration first.")

    log("Migrating Velo classes/bookings → class_schedules, gym_classes, class_bookings...")
    inserts = collect_mysql_inserts(sql_path)
    sched, gym, bk, skip = _migrate_velo_classes_and_bookings(tenant_id, inserts, maps)
    log(f"  class_schedules: {sched}, gym_classes: {gym}, class_bookings: {bk} (skipped {skip})")
    STATE_FILE.write_text(json.dumps(maps, indent=2), encoding="utf-8")
    log(f"  ID maps updated → {STATE_FILE}")


def clear_tenant_migrated_data(tenant_id: str) -> None:
    """Remove prior Velo migration rows so --force-migrate can re-import cleanly."""
    log(f"  Clearing existing migrated data for tenant '{tenant_id}'...")
    with migration_engine.begin() as conn:
        # Child tables first (FK-safe order)
        _clear_velo_class_tables(conn, tenant_id)
        conn.execute(
            text(
                """
                DELETE FROM wallet_transactions
                WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)
                """
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                """
                DELETE FROM sales_transactions
                WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)
                """
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text("DELETE FROM sales WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)"),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                "DELETE FROM user_package_tracking WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)"
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text("DELETE FROM user_packages WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)"),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                """
                DELETE FROM package_pricing
                WHERE package_id IN (SELECT id FROM packages WHERE tenant_id = :tid)
                """
            ),
            {"tid": tenant_id},
        )
        conn.execute(text("DELETE FROM packages WHERE tenant_id = :tid"), {"tid": tenant_id})
        conn.execute(text("DELETE FROM users WHERE tenant_id = :tid"), {"tid": tenant_id})
        conn.execute(
            text("DELETE FROM training_program_layout WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        conn.execute(text("DELETE FROM fitness_programs WHERE tenant_id = :tid"), {"tid": tenant_id})
        conn.execute(text("DELETE FROM locations WHERE tenant_id = :tid"), {"tid": tenant_id})
        # Partial wipes can leave gym_classes without fitness_programs rows
        orphan_gc = conn.execute(
            text(
                """
                DELETE FROM gym_classes gc
                WHERE NOT EXISTS (
                    SELECT 1 FROM fitness_programs fp WHERE fp.id = gc.training_programme_id
                )
                """
            )
        ).rowcount
        if orphan_gc:
            log(f"  Removed orphan gym_classes (no programme): {orphan_gc}")
    log("  Cleared users, locations, packages, and related rows.")


def step_migrate_from_sql_file(
    tenant_id: str,
    sql_path: Path,
    *,
    force_migrate: bool,
    core_only: bool = False,
    resume: bool = False,
) -> Dict[str, Any]:
    if core_only:
        log("WARNING: --core-only skips class_bookings. For ALL data omit --core-only.")
        log("Migrating Velo CORE only — run full migration or --resume to finish bookings later.")
    else:
        log("FULL migration: all Velo tables → PostgreSQL (users, sales, class_bookings, …).")
        log("  One run imports everything. If interrupted, re-run with --resume.")
    log("  Phases commit as they finish — pgAdmin updates after each COMMITTED line.")

    prior = _read_migration_state()
    maps: Dict[str, Any] = _fresh_maps(tenant_id)

    with migration_engine.connect() as conn:
        existing_users = conn.execute(
            text("SELECT count(*) FROM users WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        ).scalar()
        if resume:
            if not prior:
                raise RuntimeError(
                    f"No {STATE_FILE} found. Run a full import first (without --resume), e.g.\n"
                    f"  --tenant-id {tenant_id} --force-migrate"
                )
            file_tid = prior.get("tenant_id")
            if file_tid != tenant_id:
                raise RuntimeError(
                    f"State file is for tenant '{file_tid}', not '{tenant_id}'.\n"
                    f"  --resume only continues the same tenant as the state file.\n"
                    f"  For a full ORG-103 import from scratch, run:\n"
                    f"    python3 scripts/setup_velo_tenant.py --sql-path <dump.sql> "
                    f"--tenant-id {tenant_id} --force-migrate\n"
                    f"  (Do not use --resume; old UUID maps in the state file are for '{file_tid}'.)"
                )
            if prior.get("migration_complete"):
                log("  Migration already marked complete for this tenant.")
                return prior
            maps = prior
            maps["tenant_id"] = tenant_id
            if not maps.get("completed_phases"):
                inferred = _infer_completed_phases_from_db(conn, tenant_id)
                maps["completed_phases"] = inferred
                log(f"  Inferred completed phases from DB: {inferred or '(none — will run all phases)'}")
            log(f"  --resume: continuing after phases {maps.get('completed_phases', [])}")
        elif force_migrate:
            log("  --force-migrate: clearing all prior tenant data before re-import...")
            clear_tenant_migrated_data(tenant_id)
            maps = _fresh_maps(tenant_id)
        elif existing_users and existing_users > 0:
            if prior.get("migration_complete"):
                log(f"  Full migration already done ({existing_users} users). Use --force-migrate to re-import.")
                return prior
            if prior.get("completed_phases"):
                log(f"  Partial import detected ({existing_users} users, phases={prior.get('completed_phases')}).")
                log("  To finish ALL tables: add --resume (keeps saved data).")
                log("  To start over: --force-migrate (wipes tenant data first).")
                return prior
            log(f"  Skipping: {existing_users} users exist but no checkpoint file — use --force-migrate or --resume.")
            return maps
        else:
            # Partial import (e.g. locations without users) — avoid unique constraint failures
            partial = conn.execute(
                text(
                    """
                    SELECT
                      (SELECT count(*) FROM locations WHERE tenant_id = :tid) AS locs,
                      (SELECT count(*) FROM packages WHERE tenant_id = :tid) AS pkgs
                    """
                ),
                {"tid": tenant_id},
            ).one()
            if partial[0] or partial[1]:
                log(
                    f"  Found leftover tenant rows (locations={partial[0]}, packages={partial[1]}) "
                    "— clearing before import."
                )
                clear_tenant_migrated_data(tenant_id)
            maps = _fresh_maps(tenant_id)

    parse_tables = CORE_MIGRATE_TABLES if core_only else MIGRATE_TABLES
    if not resume or not _phase_done(maps, "1a"):
        log("  Scanning SQL dump (all required tables)...")
    inserts = collect_mysql_inserts(sql_path, tables=parse_tables)
    role_names, user_roles = _load_mysql_reference_maps(inserts)
    log(f"  MySQL roles: {len(role_names)}, user-role links: {len(user_roles)}")

    with migration_engine.connect() as conn:
        user_role_id, trainer_role_id, _fallback_role_id = _resolve_bookify_role_ids(conn)

    # --- Phase 1a: locations ---
    if _phase_done(maps, "1a"):
        log("  Skip phase 1a (locations) — already committed.")
        loc_count = len(maps.get("locations", {}))
    else:
        log("  Phase 1a: locations + fitness_programs...")
        loc_count = 0
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            for stmt in inserts.get("locations", []):
                _, cols, rows = _parse_mysql_insert(stmt)
                for row in _rows_as_dicts(cols, rows):
                    if row.get("deleted_at"):
                        continue
                    mid = str(row["id"])
                    name = (row.get("name") or "Location")[:200]
                    status = row.get("status")
                    loc_uuid = str(uuid.uuid4())
                    maps["locations"][mid] = loc_uuid
                    conn.execute(
                        text(
                            """
                            INSERT INTO locations (
                                id, tenant_id, name, address_line1, city, country, is_active,
                                created_at, updated_at
                            ) VALUES (
                                CAST(:id AS uuid), :tid, :name, '-', 'Doha', 'Qatar',
                                :active, COALESCE(CAST(:ca AS timestamptz), now()),
                                COALESCE(CAST(:ua AS timestamptz), now())
                            )
                            """
                        ),
                        {
                            "id": loc_uuid,
                            "tid": tenant_id,
                            "name": name,
                            "active": status in ("1", 1, True),
                            "ca": row.get("created_at"),
                            "ua": row.get("updated_at"),
                        },
                    )
                    fp_id = conn.execute(
                        text(
                            """
                            INSERT INTO fitness_programs (
                                tenant_id, location_id, name, description, image_url,
                                is_active, training_mode, gender_restriction,
                                is_layout_required, spot_name, show_spots_left, spots_left_label,
                                created_at, updated_at
                            ) VALUES (
                                :tid, CAST(:lid AS uuid), :name, :desc, :img,
                                :active, 'one_to_many', 'mixed',
                                :layout_req, :spot, :show_left, :left_label,
                                COALESCE(CAST(:ca AS timestamptz), now()),
                                COALESCE(CAST(:ua AS timestamptz), now())
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "lid": loc_uuid,
                            "name": name[:255],
                            "desc": f"Velo location {name}"[:2000],
                            "img": row.get("image"),
                            "active": status in ("1", 1, True),
                            "layout_req": bool(row.get("layout_id")),
                            "spot": (row.get("spot_name") or "Spot")[:50],
                            "show_left": bool(row.get("leftSpot")),
                            "left_label": (str(row.get("leftSpot"))[:50] if row.get("leftSpot") else None),
                            "ca": row.get("created_at"),
                            "ua": row.get("updated_at"),
                        },
                    ).scalar()
                    maps["fitness_programs"][mid] = int(fp_id)
                    loc_count += 1
        log(f"  locations: {loc_count}")
    log(f"  COMMITTED phase 1a — {loc_count} locations.")
    _write_migration_state(maps, last_phase="1a")

    # --- Phase 1b: packages ---
    if _phase_done(maps, "1b"):
        log("  Skip phase 1b (packages) — already committed.")
        pkg_count = len(maps.get("packages", {}))
    else:
        log("  Phase 1b: packages...")
        pkg_count = 0
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            for stmt in inserts.get("packages", []):
                _, cols, rows = _parse_mysql_insert(stmt)
                for row in _rows_as_dicts(cols, rows):
                    if str(row.get("status")) != "1":
                        continue
                    mid = str(row["id"])
                    name = (row.get("name") or "Package")[:150]
                    ptype = row.get("type") or "ride"
                    rides = row.get("rides")
                    days = row.get("days")
                    amount = row.get("amount") or "0"
                    pkg_uuid = str(uuid.uuid4())
                    maps["packages"][mid] = pkg_uuid
                    pkg_type = "recurring" if ptype == "unlimited" else "one_time"
                    conn.execute(
                        text(
                            """
                            INSERT INTO packages (
                                id, name, validity_days, status, package_type, tenant_id,
                                created_at, updated_at
                            ) VALUES (
                                CAST(:id AS uuid), :name, :days, 'active', :ptype, :tid,
                                COALESCE(CAST(:ca AS timestamp), now()),
                                COALESCE(CAST(:ua AS timestamp), now())
                            )
                            """
                        ),
                        {
                            "id": pkg_uuid,
                            "name": name,
                            "days": int(days) if days and str(days).isdigit() else None,
                            "ptype": pkg_type,
                            "tid": tenant_id,
                            "ca": row.get("created_at"),
                            "ua": row.get("updated_at"),
                        },
                    )
                    session_count = int(rides) if rides and str(rides).isdigit() else None
                    pricing_uuid = str(uuid.uuid4())
                    maps["package_pricing"][mid] = pricing_uuid
                    conn.execute(
                        text(
                            """
                            INSERT INTO package_pricing (
                                id, package_id, price, session_type, session_count, is_unlimited
                            ) VALUES (
                                CAST(:id AS uuid), CAST(:pid AS uuid), :price, 'sessions', :sc, :unl
                            )
                            """
                        ),
                        {
                            "id": pricing_uuid,
                            "pid": pkg_uuid,
                            "price": float(amount),
                            "sc": session_count,
                            "unl": ptype == "unlimited",
                        },
                    )
                    pkg_count += 1
        log(f"  packages: {pkg_count}")
    log(f"  COMMITTED phase 1b — {pkg_count} packages.")
    _write_migration_state(maps, last_phase="1b")

    # --- Phase 1c: members + trainers ---
    if _phase_done(maps, "1c"):
        log("  Skip phase 1c (users/trainers) — already committed.")
        user_count = len(maps.get("users", {}))
    else:
        log("  Phase 1c: users + trainers (batch size %s)..." % INSERT_BATCH_SIZE)
        user_count = 0
        skipped = 0
        trainer_mysql_ids = _collect_trainer_mysql_ids(inserts, user_roles, role_names)
        user_batch: List[Dict[str, Any]] = []
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            for stmt in inserts.get("users", []):
                _, cols, rows = _parse_mysql_insert(stmt)
                for row in _rows_as_dicts(cols, rows):
                    if row.get("deleted_at"):
                        skipped += 1
                        continue
                    mid = str(row["id"])
                    if mid in trainer_mysql_ids:
                        skipped += 1
                        continue
                    email = (row.get("email") or "").strip().lower()
                    if not email:
                        skipped += 1
                        continue
                    user_uuid = str(uuid.uuid4())
                    maps["users"][mid] = user_uuid
                    user_batch.append(
                        _bookify_user_params(
                            tenant_id=tenant_id,
                            user_uuid=user_uuid,
                            role_id=user_role_id,
                            user_type=CLIENT_BOOKIFY_USER_TYPE,
                            email=email,
                            row=row,
                        )
                    )
                    user_count += 1
                    if len(user_batch) >= INSERT_BATCH_SIZE:
                        _insert_bookify_users_batch(conn, user_batch)
                        user_batch.clear()
                        log(f"    clients inserted: {user_count}")
            if user_batch:
                _insert_bookify_users_batch(conn, user_batch)
            log(f"  clients (role=user, user_type=client): {user_count} (skipped {skipped})")
            tr_ins, tr_upd, tr_skip = _migrate_velo_trainers(
                conn,
                tenant_id,
                inserts,
                maps,
                user_roles,
                role_names,
                trainer_role_id,
            )
            log(f"  trainers (role=trainer): {tr_ins} inserted, {tr_upd} updated (skipped {tr_skip})")
    log(f"  COMMITTED phase 1c — {len(maps['users'])} users in DB.")
    _write_migration_state(maps, last_phase="1c")

    # --- Phase 2: wallet ledger ---
    wtxn_by_user_txn: Dict[Tuple[str, str], str] = {}
    if _phase_done(maps, "2"):
        log("  Skip phase 2 (wallet_transactions) — already committed.")
        wtxn_count = len(maps.get("wallet_transactions", {}))
        with migration_engine.connect() as conn:
            wtxn_by_user_txn = _load_wtxn_lookup_from_db(conn, tenant_id)
    else:
        log("  Phase 2: wallet_transactions...")
        _wallet_bulk_sql = """
            INSERT INTO wallet_transactions (
                id, user_id, direction, transaction_id, amount, currency,
                created_by, created_at, updated_at
            ) VALUES %s
        """
        _wallet_template = (
            "(%s::uuid, %s::uuid, %s, %s, %s, 'QAR', %s, "
            "COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now()))"
        )
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            wtxn_count = 0
            wtxn_skip = 0
            wtxn_batch: List[tuple] = []
            for stmt in inserts.get("wallet_transactions", []):
                _, cols, rows = _parse_mysql_insert(stmt)
                for row in _rows_as_dicts(cols, rows):
                    uid = maps["users"].get(str(row.get("user_id")))
                    if not uid:
                        wtxn_skip += 1
                        continue
                    wtype = (row.get("type") or "Credit").lower()
                    direction = "credit" if wtype == "credit" else "debit"
                    status = (row.get("status") or "Completed").lower()
                    if status in ("cancelled", "failed"):
                        wtxn_skip += 1
                        continue
                    wuuid = str(uuid.uuid4())
                    maps["wallet_transactions"][str(row.get("id"))] = wuuid
                    tid = row.get("txn_id")
                    if tid:
                        wtxn_by_user_txn[(uid, tid)] = wuuid
                    wtxn_batch.append(
                        (
                            wuuid,
                            uid,
                            direction,
                            tid,
                            float(row.get("amount") or 0),
                            (row.get("createdBy") or "user").lower(),
                            row.get("created_at"),
                            row.get("updated_at"),
                        )
                    )
                    wtxn_count += 1
                    if len(wtxn_batch) >= INSERT_BATCH_SIZE:
                        _bulk_insert_tuples(conn, _wallet_bulk_sql, wtxn_batch, template=_wallet_template)
                        wtxn_batch.clear()
                    if wtxn_count % 5000 == 0:
                        log(f"    wallet_transactions: {wtxn_count}")
            if wtxn_batch:
                _bulk_insert_tuples(conn, _wallet_bulk_sql, wtxn_batch, template=_wallet_template)
            log(f"  wallet_transactions: {wtxn_count} (skipped {wtxn_skip})")
            w_upd, w_zero = _sync_user_wallet_balances(conn, tenant_id)
            w_bf = _backfill_wallet_transaction_balances(conn, tenant_id)
            log(f"  users.wallet synced: {w_upd} updated, {w_zero} zeroed; ledger rows backfilled: {w_bf}")
    log(f"  COMMITTED phase 2 — wallet_transactions ({wtxn_count} rows).")
    _write_migration_state(maps, last_phase="2")

    # --- Phase 3: user_packages ---
    if _phase_done(maps, "3"):
        log("  Skip phase 3 (user_packages) — already committed.")
        upkg_count = len(maps.get("user_packages", {}))
    else:
        log("  Phase 3: user_packages...")
        _upkg_bulk_sql = """
            INSERT INTO user_packages (
                id, user_id, package_id, pricing_id, session_count,
                session_type, created_at
            ) VALUES %s
        """
        _upkg_template = (
            "(%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, 'sessions', "
            "COALESCE(%s::timestamptz, now()))"
        )
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            upkg_count = 0
            upkg_skip = 0
            upkg_batch: List[tuple] = []
            for stmt in inserts.get("package_user", []):
                _, cols, rows = _parse_mysql_insert(stmt)
                for row in _rows_as_dicts(cols, rows):
                    if str(row.get("status")) != "1":
                        upkg_skip += 1
                        continue
                    uid = maps["users"].get(str(row.get("user_id")))
                    pid = maps["packages"].get(str(row.get("package_id")))
                    if not uid or not pid:
                        upkg_skip += 1
                        continue
                    pricing_id = maps["package_pricing"].get(str(row.get("package_id")))
                    rides_left = row.get("rides_left")
                    pu_mid = str(row.get("id"))
                    pkg_mid = str(row.get("package_id"))
                    maps["package_user_packages"][pu_mid] = pkg_mid
                    upuuid = str(uuid.uuid4())
                    maps["user_packages"][pu_mid] = upuuid
                    upkg_batch.append(
                        (
                            upuuid,
                            uid,
                            pid,
                            pricing_id,
                            int(rides_left) if rides_left and str(rides_left).isdigit() else None,
                            row.get("created_at"),
                        )
                    )
                    upkg_count += 1
                    if len(upkg_batch) >= INSERT_BATCH_SIZE:
                        _bulk_insert_tuples(conn, _upkg_bulk_sql, upkg_batch, template=_upkg_template)
                        upkg_batch.clear()
                    if upkg_count % 5000 == 0:
                        log(f"    user_packages: {upkg_count}")
            if upkg_batch:
                _bulk_insert_tuples(conn, _upkg_bulk_sql, upkg_batch, template=_upkg_template)
            log(f"  user_packages: {upkg_count} (skipped {upkg_skip})")
    log(f"  COMMITTED phase 3 — user_packages ({upkg_count} rows).")
    _write_migration_state(maps, last_phase="3")

    # --- Phase 4: sales ---
    if _phase_done(maps, "4"):
        log("  Skip phase 4 (sales) — already committed.")
        sale_count = len(maps.get("sales", {}))
    else:
        log("  Phase 4: sales + sales_transactions...")
        with migration_engine.begin() as conn:
            _apply_fast_session(conn)
            sale_count, stxn_count, sale_skip = _migrate_velo_transactions_to_sales(
                conn,
                tenant_id,
                maps,
                inserts,
                wtxn_by_user_txn,
            )
            log(f"  sales: {sale_count}, sales_transactions: {stxn_count} (skipped {sale_skip})")
    log("  COMMITTED phase 4 — sales done.")
    _write_migration_state(maps, last_phase="4")

    if core_only:
        log("  WARNING: --core-only — class_bookings NOT imported. Re-run without --core-only for full data.")
    elif _phase_done(maps, "5_classes") and _phase_done(maps, "6_programs"):
        log("  Skip phases 5–6 (classes/bookings/layouts) — already committed.")
    else:
        if not _phase_done(maps, "5_classes"):
            log("  Phase 5: class_schedules, gym_classes, class_bookings...")
            sched_n, gym_n, bk_n, bk_skip = _migrate_velo_classes_and_bookings(tenant_id, inserts, maps)
            log(f"  class_schedules: {sched_n}, gym_classes: {gym_n}, class_bookings: {bk_n} (skipped {bk_skip})")
            _write_migration_state(maps, last_phase="5_classes")
        if not _phase_done(maps, "6_programs"):
            log("  Phase 6: fitness_programs + training_program_layout...")
            upd_fp, cls_fp, lay_n = _migrate_velo_fitness_programs_and_layouts(tenant_id, inserts, maps)
            log(f"  fitness_programs: updated {upd_fp} locations, +{cls_fp} from classes; layouts: {lay_n}")
            _write_migration_state(maps, last_phase="6_programs")

    if not core_only:
        _write_migration_state(maps, complete=True)
        log("  FULL MIGRATION COMPLETE — all Velo tables imported for this tenant.")
    _write_migration_state(maps)
    log(f"  State saved → {STATE_FILE}")
    return maps


def _migrate_velo_transactions_to_sales(
    conn,
    tenant_id: str,
    maps: Dict[str, Any],
    inserts: Dict[str, List[str]],
    wtxn_by_user_txn: Dict[Tuple[str, str], str],
) -> Tuple[int, int, int]:
    """Velo MySQL `transactions` → Bookify sales + sales_transactions."""
    sale_count = 0
    stxn_count = 0
    sale_skip = 0
    maps.setdefault("sales", {})
    _sale_bulk = """
        INSERT INTO sales (
            id, tenant_id, user_id, amount, wallet_transaction_id,
            item_type, item_id, payment_source, created_by_type,
            transaction_id, extra_metadata, created_at, updated_at
        ) VALUES %s
    """
    _sale_template = (
        "(%s::uuid, %s, %s::uuid, %s, %s::uuid, %s, %s::uuid, %s, 'member', %s, %s::jsonb, "
        "COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now()))"
    )
    _stxn_bulk = """
        INSERT INTO sales_transactions (
            order_id, tenant_id, payment_method, gateway, gateway_txn_id,
            status, amount, currency, source, user_id, created_by_type,
            created_by_id, extra_metadata, created_at
        ) VALUES %s
    """
    _stxn_template = (
        "(%s::uuid, %s, %s, %s, %s, 'success', %s, %s, %s, %s::uuid, 'member', "
        "%s::uuid, %s::jsonb, COALESCE(%s::timestamptz, now()))"
    )
    _up_sale_sql = (
        "UPDATE user_packages SET sale_id = CAST(%s AS uuid) WHERE id = CAST(%s AS uuid)"
    )
    sale_batch: List[tuple] = []
    stxn_batch: List[tuple] = []
    up_sale_batch: List[tuple] = []

    def _flush_sale_batches() -> None:
        if sale_batch:
            _bulk_insert_tuples(conn, _sale_bulk, sale_batch, template=_sale_template)
            sale_batch.clear()
        if stxn_batch:
            _bulk_insert_tuples(conn, _stxn_bulk, stxn_batch, template=_stxn_template)
            stxn_batch.clear()
        if up_sale_batch:
            from psycopg2.extras import execute_batch

            raw = conn.connection.dbapi_connection
            cur = raw.cursor()
            try:
                execute_batch(cur, _up_sale_sql, up_sale_batch, page_size=BULK_PAGE_SIZE)
            finally:
                cur.close()
            up_sale_batch.clear()

    for stmt in inserts.get("transactions", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
                status_raw = (row.get("status") or "").strip().upper()
                if status_raw not in ("COMPLETED", "PAID", "SUCCESS"):
                    sale_skip += 1
                    continue

                uid = maps["users"].get(str(row.get("user_id")))
                if not uid:
                    sale_skip += 1
                    continue

                payment_source = _velo_txn_payment_source(row)
                if not payment_source:
                    sale_skip += 1
                    continue

                try:
                    amount = float(str(row.get("amount") or "0").replace(",", ""))
                except ValueError:
                    sale_skip += 1
                    continue

                currency = (row.get("currency") or "QAR").strip()[:3]
                if currency == "QR":
                    currency = "QAR"

                gateway = _velo_txn_gateway(row)
                txn_id_str = row.get("txn_id")
                wtxn_id = (
                    wtxn_by_user_txn.get((uid, txn_id_str))
                    if txn_id_str
                    else None
                )

                item_type = None
                item_id = None
                pu_id = row.get("package_user_id")
                if pu_id and str(pu_id) not in ("NULL", "0", ""):
                    item_type = "package"
                    pkg_mid = maps["package_user_packages"].get(str(pu_id))
                    if pkg_mid:
                        item_id = maps["packages"].get(pkg_mid)

                extra = {
                    "currency": currency,
                    "gateway": gateway,
                    "status": "success" if status_raw in ("COMPLETED", "PAID", "SUCCESS") else "pending",
                    "velo_transaction_id": row.get("id"),
                    "velo_order_id": row.get("order_id"),
                    "velo_txn_id": txn_id_str,
                }
                sale_uuid = str(uuid.uuid4())
                maps["sales"][str(row.get("id"))] = sale_uuid

                st_source = "package" if payment_source in ("package_gateway", "package_wallet") else "wallet"
                st_payment_method = (
                    "wallet" if payment_source in ("package_wallet", "wallet_add") else "gateway"
                )
                sale_batch.append(
                    (
                        sale_uuid,
                        tenant_id,
                        uid,
                        amount,
                        wtxn_id,
                        item_type,
                        item_id,
                        payment_source,
                        int(row["id"]) if str(row.get("id")).isdigit() else None,
                        json.dumps(extra),
                        row.get("created_at"),
                        row.get("updated_at"),
                    )
                )
                stxn_batch.append(
                    (
                        sale_uuid,
                        tenant_id,
                        st_payment_method,
                        gateway,
                        txn_id_str or row.get("order_id"),
                        amount,
                        currency,
                        st_source,
                        uid,
                        uid,
                        json.dumps({"event": "velo_import", "velo_txn_id": row.get("id")}),
                        row.get("created_at"),
                    )
                )

                if pu_id and str(pu_id) not in ("NULL", "0", ""):
                    up_uuid = maps["user_packages"].get(str(pu_id))
                    if up_uuid:
                        up_sale_batch.append((sale_uuid, up_uuid))

                sale_count += 1
                stxn_count += 1
                if len(sale_batch) >= INSERT_BATCH_SIZE:
                    _flush_sale_batches()
                if sale_count % 5000 == 0:
                    log(f"    sales: {sale_count}")

    _flush_sale_batches()
    return sale_count, stxn_count, sale_skip


def _load_wtxn_lookup_from_db(conn, tenant_id: str) -> Dict[Tuple[str, str], str]:
    lookup: Dict[Tuple[str, str], str] = {}
    rows = conn.execute(
        text(
            """
            SELECT wt.id, wt.user_id, wt.transaction_id
            FROM wallet_transactions wt
            JOIN users u ON u.id = wt.user_id
            WHERE u.tenant_id = :tid AND wt.transaction_id IS NOT NULL
            """
        ),
        {"tid": tenant_id},
    )
    for wid, uid, tid in rows:
        lookup[(str(uid), str(tid))] = str(wid)
    return lookup


def step_migrate_sales_only(tenant_id: str, sql_path: Path) -> None:
    """Import sales + sales_transactions using existing .velo_migration_state.json maps."""
    if not STATE_FILE.is_file():
        raise RuntimeError(f"Missing {STATE_FILE} — run full migration first.")
    maps = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not maps.get("users"):
        raise RuntimeError("State file has no user ID maps — run full migration first.")

    log("Migrating Velo transactions → sales (sales-only, reusing ID maps)...")
    with migration_engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM sales_transactions
                WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)
                """
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text("DELETE FROM sales WHERE tenant_id = :tid"),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                """
                UPDATE user_packages SET sale_id = NULL
                WHERE user_id IN (SELECT id FROM users WHERE tenant_id = :tid)
                """
            ),
            {"tid": tenant_id},
        )
        wtxn_by_user_txn = _load_wtxn_lookup_from_db(conn, tenant_id)

    inserts = collect_mysql_inserts(sql_path)
    maps.setdefault("package_user_packages", {})
    for stmt in inserts.get("package_user", []):
        _, cols, rows = _parse_mysql_insert(stmt)
        for row in _rows_as_dicts(cols, rows):
            maps["package_user_packages"][str(row.get("id"))] = str(row.get("package_id"))

    with migration_engine.begin() as conn:
        sale_count, stxn_count, sale_skip = _migrate_velo_transactions_to_sales(
            conn,
            tenant_id,
            maps,
            inserts,
            wtxn_by_user_txn,
        )
    maps.setdefault("sales", {})
    STATE_FILE.write_text(json.dumps(maps, indent=2), encoding="utf-8")
    log(f"  sales: {sale_count}, sales_transactions: {stxn_count} (skipped {sale_skip})")
    log(f"  ID maps updated → {STATE_FILE}")


def step_migrate_mysql_to_postgres(
    tenant_id: str,
    mysql_db: str,
    mysql_user: str,
    mysql_password: str,
    mysql_host: str,
) -> Dict[str, Any]:
    log("Migrating core Velo data → PostgreSQL fithubpro...")
    maps: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "locations": {},
        "fitness_programs": {},
        "users": {},
        "packages": {},
    }

    with migration_engine.begin() as conn:
        # --- roles lookup (existing Bookify roles) ---
        role_rows = conn.execute(text("SELECT id, key FROM roles WHERE key IS NOT NULL")).fetchall()
        role_by_key = {r[1]: str(r[0]) for r in role_rows}
        default_role_id = role_by_key.get("user") or role_by_key.get("member")
        if not default_role_id:
            raise RuntimeError("No 'user' role in PostgreSQL — seed roles first.")

        # --- locations + fitness_programs ---
        loc_lines = mysql_query(
            """
            SELECT id, name, status, created_at, updated_at
            FROM locations WHERE deleted_at IS NULL
            """,
            mysql_user=mysql_user,
            mysql_password=mysql_password,
            mysql_host=mysql_host,
            database=mysql_db,
        )
        for row in _parse_tsv_rows(loc_lines):
            mid, name, status, created_at, updated_at = row[0], row[1], row[2], row[3], row[4]
            loc_uuid = str(uuid.uuid4())
            maps["locations"][mid] = loc_uuid
            conn.execute(
                text(
                    """
                    INSERT INTO locations (
                        id, tenant_id, name, address_line1, city, country, is_active,
                        created_at, updated_at
                    ) VALUES (
                        CAST(:id AS uuid), :tid, :name, '-', 'Doha', 'Qatar',
                        :active, COALESCE(:ca, now()), COALESCE(:ua, now())
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": loc_uuid,
                    "tid": tenant_id,
                    "name": name[:200],
                    "active": status == "1",
                    "ca": created_at or None,
                    "ua": updated_at or None,
                },
            )
            fp_id = conn.execute(
                text(
                    """
                    INSERT INTO fitness_programs (
                        tenant_id, location_id, name, is_active, training_mode, gender_restriction
                    ) VALUES (
                        :tid, CAST(:lid AS uuid), :name, :active, 'one_to_many', 'mixed'
                    )
                    RETURNING id
                    """
                ),
                {"tid": tenant_id, "lid": loc_uuid, "name": name[:60], "active": status == "1"},
            ).scalar()
            maps["fitness_programs"][mid] = int(fp_id)
        log(f"  locations: {len(maps['locations'])}, fitness_programs: {len(maps['fitness_programs'])}")

        # --- users ---
        user_lines = mysql_query(
            """
            SELECT u.id, u.email, u.phone, u.password, u.first_name, u.last_name,
                   u.gender, u.dob, u.status, u.created_at, u.updated_at,
                   COALESCE(r.name, 'User') AS role_name
            FROM users u
            LEFT JOIN model_has_roles mhr ON mhr.model_id = u.id AND mhr.model_type LIKE '%%User'
            LEFT JOIN roles r ON r.id = mhr.role_id
            WHERE u.deleted_at IS NULL AND u.email IS NOT NULL AND u.email != ''
            GROUP BY u.id
            """,
            mysql_user=mysql_user,
            mysql_password=mysql_password,
            mysql_host=mysql_host,
            database=mysql_db,
        )
        user_count = 0
        for row in _parse_tsv_rows(user_lines):
            (
                mid,
                email,
                phone,
                password_hash,
                first_name,
                last_name,
                gender,
                dob,
                status,
                created_at,
                updated_at,
                role_name,
            ) = row
            role_key = MYSQL_ROLE_TO_BOOKIFY.get(role_name.lower(), "user")
            role_id = role_by_key.get(role_key, default_role_id)
            bookify_utype = (
                TRAINER_BOOKIFY_USER_TYPE
                if role_key == "trainer"
                else CLIENT_BOOKIFY_USER_TYPE
            )
            user_uuid = str(uuid.uuid4())
            maps["users"][mid] = user_uuid
            g = (gender or "Male").lower()
            if g not in ("male", "female", "other"):
                g = "male"
            conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, tenant_id, role_id, email, phone, password_hash,
                        first_name, last_name, gender, dob, is_active, user_type,
                        created_at, updated_at
                    ) VALUES (
                        CAST(:id AS uuid), :tid, CAST(:rid AS uuid), :email, :phone, :phash,
                        :fn, :ln, :gender, CAST(:dob AS date), :active, :utype,
                        COALESCE(CAST(:ca AS timestamptz), now()),
                        COALESCE(CAST(:ua AS timestamptz), now())
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": user_uuid,
                    "tid": tenant_id,
                    "rid": role_id,
                    "utype": bookify_utype,
                    "email": email.strip().lower()[:500],
                    "phone": phone or None,
                    "phash": password_hash,
                    "fn": (first_name or "")[:70],
                    "ln": (last_name or "")[:70],
                    "gender": g,
                    "dob": dob if dob and dob != "0000-00-00" else None,
                    "active": status == "1",
                    "ca": created_at or None,
                    "ua": updated_at or None,
                },
            )
            user_count += 1
        log(f"  users migrated: {user_count}")

        # --- packages + pricing ---
        pkg_lines = mysql_query(
            """
            SELECT id, name, type, rides, days, amount, status, created_at, updated_at
            FROM packages WHERE status = 1
            """,
            mysql_user=mysql_user,
            mysql_password=mysql_password,
            mysql_host=mysql_host,
            database=mysql_db,
        )
        pkg_count = 0
        for row in _parse_tsv_rows(pkg_lines):
            mid, name, ptype, rides, days, amount, status, created_at, updated_at = row
            pkg_uuid = str(uuid.uuid4())
            maps["packages"][mid] = pkg_uuid
            pkg_type = "recurring" if ptype == "unlimited" else "one_time"
            conn.execute(
                text(
                    """
                    INSERT INTO packages (
                        id, name, validity_days, status, package_type, tenant_id,
                        created_at, updated_at
                    ) VALUES (
                        CAST(:id AS uuid), :name, :days, 'active', :ptype, :tid,
                        COALESCE(CAST(:ca AS timestamp), now()),
                        COALESCE(CAST(:ua AS timestamp), now())
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": pkg_uuid,
                    "name": name[:150],
                    "days": int(days) if days else None,
                    "ptype": pkg_type,
                    "tid": tenant_id,
                    "ca": created_at or None,
                    "ua": updated_at or None,
                },
            )
            session_count = int(rides) if rides and rides != "NULL" else None
            is_unlimited = ptype == "unlimited"
            price = float(amount) if amount else 0
            conn.execute(
                text(
                    """
                    INSERT INTO package_pricing (
                        package_id, price, session_type, session_count, is_unlimited
                    ) VALUES (
                        CAST(:pid AS uuid), :price, 'sessions', :sc, :unl
                    )
                    """
                ),
                {
                    "pid": pkg_uuid,
                    "price": price,
                    "sc": session_count,
                    "unl": is_unlimited,
                },
            )
            pkg_count += 1
        log(f"  packages migrated: {pkg_count}")

    STATE_FILE.write_text(json.dumps(maps, indent=2), encoding="utf-8")
    log(f"  ID maps saved → {STATE_FILE}")
    return maps


def _sync_user_wallet_balances(conn, tenant_id: str) -> Tuple[int, int]:
    """
    users.wallet = net wallet_transactions (credit minus debit) per user.
    Users with no ledger rows get wallet = 0.
    """
    updated = conn.execute(
        text(
            """
            WITH bal AS (
                SELECT wt.user_id,
                    COALESCE(SUM(
                        CASE WHEN wt.direction = 'credit' THEN wt.amount ELSE -wt.amount END
                    ), 0) AS wallet_balance
                FROM wallet_transactions wt
                JOIN users u ON u.id = wt.user_id
                WHERE u.tenant_id = :tid
                GROUP BY wt.user_id
            )
            UPDATE users u
            SET wallet = bal.wallet_balance, updated_at = now()
            FROM bal
            WHERE u.id = bal.user_id AND u.tenant_id = :tid
            """
        ),
        {"tid": tenant_id},
    ).rowcount
    zeroed = conn.execute(
        text(
            """
            UPDATE users u
            SET wallet = 0, updated_at = now()
            WHERE u.tenant_id = :tid
              AND NOT EXISTS (SELECT 1 FROM wallet_transactions wt WHERE wt.user_id = u.id)
              AND COALESCE(u.wallet, 0) <> 0
            """
        ),
        {"tid": tenant_id},
    ).rowcount
    return int(updated or 0), int(zeroed or 0)


def _backfill_wallet_transaction_balances(conn, tenant_id: str) -> int:
    """Populate wallet_transactions.balance_before / balance_after (running ledger)."""
    result = conn.execute(
        text(
            """
            WITH ordered AS (
                SELECT wt.id,
                    SUM(
                        CASE WHEN wt.direction = 'credit' THEN wt.amount ELSE -wt.amount END
                    ) OVER (
                        PARTITION BY wt.user_id
                        ORDER BY wt.created_at, wt.id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS bal_after,
                    COALESCE(
                        SUM(
                            CASE WHEN wt.direction = 'credit' THEN wt.amount ELSE -wt.amount END
                        ) OVER (
                            PARTITION BY wt.user_id
                            ORDER BY wt.created_at, wt.id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                        ),
                        0
                    ) AS bal_before
                FROM wallet_transactions wt
                JOIN users u ON u.id = wt.user_id
                WHERE u.tenant_id = :tid
            )
            UPDATE wallet_transactions wt
            SET balance_before = ordered.bal_before,
                balance_after = ordered.bal_after,
                updated_at = now()
            FROM ordered
            WHERE wt.id = ordered.id
            """
        ),
        {"tid": tenant_id},
    )
    return int(result.rowcount or 0)


def step_sync_wallets(tenant_id: str) -> None:
    log(f"Syncing users.wallet from wallet_transactions (tenant '{tenant_id}')...")
    with migration_engine.begin() as conn:
        updated, zeroed = _sync_user_wallet_balances(conn, tenant_id)
        backfilled = _backfill_wallet_transaction_balances(conn, tenant_id)
    log(f"  users.wallet updated: {updated}, zeroed (no ledger): {zeroed}")
    log(f"  wallet_transactions balance_before/after backfilled: {backfilled}")


def step_audit_migration(tenant_id: str) -> bool:
    """Data-quality audit after Velo import. Returns True if no critical issues."""
    log("Migration audit:")
    ok = True
    with migration_engine.connect() as conn:
        user_n = conn.execute(
            text("SELECT count(*) FROM users WHERE tenant_id = :tid"), {"tid": tenant_id}
        ).scalar()
        wtxn_n = conn.execute(
            text(
                """
                SELECT count(*) FROM wallet_transactions wt
                JOIN users u ON u.id = wt.user_id WHERE u.tenant_id = :tid
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        wallet_mismatch = conn.execute(
            text(
                """
                WITH calc AS (
                    SELECT u.id,
                        COALESCE(SUM(
                            CASE WHEN wt.direction = 'credit' THEN wt.amount ELSE -wt.amount END
                        ), 0) AS bal
                    FROM users u
                    LEFT JOIN wallet_transactions wt ON wt.user_id = u.id
                    WHERE u.tenant_id = :tid
                    GROUP BY u.id
                )
                SELECT count(*) FROM users u
                JOIN calc ON calc.id = u.id
                WHERE u.tenant_id = :tid
                  AND round(COALESCE(u.wallet, 0)::numeric, 2)
                      IS DISTINCT FROM round(calc.bal::numeric, 2)
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        users_pos_wallet = conn.execute(
            text(
                "SELECT count(*) FROM users WHERE tenant_id = :tid AND COALESCE(wallet, 0) > 0"
            ),
            {"tid": tenant_id},
        ).scalar()
        wtxn_missing_bal = conn.execute(
            text(
                """
                SELECT count(*) FROM wallet_transactions wt
                JOIN users u ON u.id = wt.user_id
                WHERE u.tenant_id = :tid
                  AND (wt.balance_after IS NULL OR wt.balance_before IS NULL)
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        orphan_bookings = conn.execute(
            text(
                """
                SELECT count(*) FROM class_bookings cb
                WHERE cb.tenant_id = :tid
                  AND NOT EXISTS (SELECT 1 FROM gym_classes gc WHERE gc.id = cb.class_id)
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        bookings_no_user = conn.execute(
            text(
                """
                SELECT count(*) FROM class_bookings cb
                WHERE cb.tenant_id = :tid
                  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = cb.user_id)
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        sales_no_user = conn.execute(
            text(
                """
                SELECT count(*) FROM sales s
                WHERE s.tenant_id = :tid
                  AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id)
                """
            ),
            {"tid": tenant_id},
        ).scalar()

        members_n = conn.execute(
            text(
                """
                SELECT count(*) FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.tenant_id = :tid AND r.key = 'user'
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        trainers_n = conn.execute(
            text(
                """
                SELECT count(*) FROM users u
                JOIN roles r ON r.id = u.role_id
                WHERE u.tenant_id = :tid AND r.key = 'trainer'
                """
            ),
            {"tid": tenant_id},
        ).scalar()
        log(f"  users (total):                 {user_n}")
        log(f"  users (role=user):             {members_n}")
        log(f"  users (role=trainer):          {trainers_n}")
        log(f"  wallet_transactions:           {wtxn_n}")
        log(f"  users.wallet > 0:                {users_pos_wallet}")
        log(f"  users.wallet vs ledger mismatch: {wallet_mismatch}")
        log(f"  wallet_txn missing before/after: {wtxn_missing_bal}")
        log(f"  class_bookings orphan class:     {orphan_bookings}")
        log(f"  class_bookings missing user:     {bookings_no_user}")
        log(f"  sales missing user:              {sales_no_user}")

        if wallet_mismatch and wallet_mismatch > 0:
            ok = False
            log("  FIX: run with --sync-wallets to recalculate users.wallet from ledger")
        if wtxn_missing_bal and wtxn_n and wtxn_missing_bal > 0:
            log("  NOTE: --sync-wallets also backfills balance_before/after on wallet_transactions")
        if orphan_bookings or bookings_no_user or sales_no_user:
            ok = False

        if STATE_FILE.is_file():
            maps = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            log("  State file map sizes:")
            for key in (
                "users",
                "wallet_transactions",
                "user_packages",
                "sales",
                "gym_classes",
                "fitness_programs_by_class",
                "training_program_layouts",
            ):
                if key in maps and isinstance(maps[key], dict):
                    log(f"    {key:28} {len(maps[key])}")

    if ok:
        log("  Audit: OK (no critical mismatches)")
    else:
        log("  Audit: issues found — see lines above")
    return ok


def step_verify(tenant_id: str) -> None:
    log("Verification:")
    with migration_engine.connect() as conn:
        checks = [
            ("locations", "SELECT count(*) FROM locations WHERE tenant_id = :tid", {"tid": tenant_id}),
            ("fitness_programs", "SELECT count(*) FROM fitness_programs WHERE tenant_id = :tid", {"tid": tenant_id}),
            ("users", "SELECT count(*) FROM users WHERE tenant_id = :tid", {"tid": tenant_id}),
            ("packages", "SELECT count(*) FROM packages WHERE tenant_id = :tid", {"tid": tenant_id}),
            (
                "package_pricing",
                "SELECT count(*) FROM package_pricing pp JOIN packages p ON p.id = pp.package_id WHERE p.tenant_id = :tid",
                {"tid": tenant_id},
            ),
            (
                "wallet_transactions",
                "SELECT count(*) FROM wallet_transactions wt JOIN users u ON u.id = wt.user_id WHERE u.tenant_id = :tid",
                {"tid": tenant_id},
            ),
            (
                "user_packages",
                "SELECT count(*) FROM user_packages up JOIN users u ON u.id = up.user_id WHERE u.tenant_id = :tid",
                {"tid": tenant_id},
            ),
            (
                "sales",
                "SELECT count(*) FROM sales WHERE tenant_id = :tid",
                {"tid": tenant_id},
            ),
            (
                "sales_transactions",
                """
                SELECT count(*) FROM sales_transactions st
                JOIN users u ON u.id = st.user_id
                WHERE u.tenant_id = :tid
                """,
                {"tid": tenant_id},
            ),
            ("class_schedules", "SELECT count(*) FROM class_schedules", {}),
            (
                "gym_classes",
                """
                SELECT count(*) FROM gym_classes gc
                JOIN fitness_programs fp ON fp.id = gc.training_programme_id
                WHERE fp.tenant_id = :tid
                """,
                {"tid": tenant_id},
            ),
            ("class_bookings", "SELECT count(*) FROM class_bookings WHERE tenant_id = :tid", {"tid": tenant_id}),
            ("fitness_programs", "SELECT count(*) FROM fitness_programs WHERE tenant_id = :tid", {"tid": tenant_id}),
            (
                "training_program_layout",
                "SELECT count(*) FROM training_program_layout WHERE tenant_id = :tid",
                {"tid": tenant_id},
            ),
        ]
        if _db_table_exists(conn, "settings"):
            checks.insert(
                0,
                ("settings", "SELECT count(*) FROM settings WHERE tenant_id = :tid", {"tid": tenant_id}),
            )
        for label, sql, params in checks:
            try:
                n = conn.execute(text(sql), params).scalar()
            except Exception as exc:
                log(f"  {label:20} (skip: {exc})")
                continue
            log(f"  {label:20} {n}")

    step_audit_migration(tenant_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate velo_live_db.sql into PostgreSQL (tenant_id on each row)")
    parser.add_argument("--sql-path", type=Path, default=DEFAULT_SQL)
    parser.add_argument(
        "--tenant-id",
        default=DEFAULT_TENANT_ID,
        help="tenant_id string stored on every migrated row (default ORG-103)",
    )
    parser.add_argument("--mysql-db", default=DEFAULT_MYSQL_DB)
    parser.add_argument("--mysql-user", default="root")
    parser.add_argument("--mysql-password", default="")
    parser.add_argument("--mysql-host", default="localhost")
    parser.add_argument(
        "--tenant-only",
        action="store_true",
        help="Only print DB connection + default tenant_id (no import)",
    )
    parser.add_argument(
        "--use-mysql",
        action="store_true",
        help="Legacy: import via local MySQL server, then migrate",
    )
    parser.add_argument("--skip-mysql-import", action="store_true", help="With --use-mysql: skip dump import")
    parser.add_argument("--force-reimport", action="store_true", help="With --use-mysql: drop & reimport MySQL DB")
    parser.add_argument(
        "--force-migrate",
        action="store_true",
        help="Re-run data migration even if velo users already exist",
    )
    parser.add_argument(
        "--sales-only",
        action="store_true",
        help="Import sales + sales_transactions only (uses .velo_migration_state.json)",
    )
    parser.add_argument(
        "--classes-only",
        action="store_true",
        help="Import class_schedules, gym_classes, class_bookings (uses .velo_migration_state.json)",
    )
    parser.add_argument(
        "--programs-only",
        action="store_true",
        help="Import/enrich fitness_programs + training_program_layout (uses .velo_migration_state.json)",
    )
    parser.add_argument(
        "--sync-wallets",
        action="store_true",
        help="Recalculate users.wallet + wallet_transactions running balances from ledger",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Run table counts + data-quality audit (no import)",
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="NOT full migration: skips class_bookings (omit this for complete import)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue full migration from last checkpoint (after a failed run)",
    )
    args = parser.parse_args()

    if args.core_only and args.resume:
        parser.error("Use either --core-only or --resume, not both.")

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    log(f"Bookify DB: {settings.database_url}")
    log(f"SQL file:  {args.sql_path}")

    try:
        step_pg_connection_check()
        log_migration_tenant(args.tenant_id)

        if args.tenant_only:
            log("Done (--tenant-only).")
            return 0

        if args.audit_only:
            step_verify(args.tenant_id)
            return 0 if step_audit_migration(args.tenant_id) else 1

        if args.sync_wallets:
            step_sync_wallets(args.tenant_id)
            step_audit_migration(args.tenant_id)
            log("Done (--sync-wallets).")
            return 0

        if args.use_mysql:
            if not mysql_available(args.mysql_user, args.mysql_password, args.mysql_host):
                log("ERROR: --use-mysql requires a running MySQL server.")
                log("  Or drop --use-mysql to import directly into PostgreSQL (default).")
                return 1
            if not args.skip_mysql_import:
                step_import_mysql_dump(
                    args.sql_path,
                    args.mysql_db,
                    args.mysql_user,
                    args.mysql_password,
                    args.mysql_host,
                    force_reimport=args.force_reimport,
                )
            step_migrate_mysql_to_postgres(
                args.tenant_id,
                args.mysql_db,
                args.mysql_user,
                args.mysql_password,
                args.mysql_host,
            )
        elif args.sales_only:
            step_migrate_sales_only(args.tenant_id, args.sql_path)
        elif args.classes_only:
            step_migrate_classes_only(args.tenant_id, args.sql_path)
        elif args.programs_only:
            step_migrate_programs_only(args.tenant_id, args.sql_path)
        else:
            maps = step_migrate_from_sql_file(
                args.tenant_id,
                args.sql_path,
                force_migrate=args.force_migrate,
                core_only=args.core_only,
                resume=args.resume,
            )
        step_verify(args.tenant_id)
        audit_ok = step_audit_migration(args.tenant_id)

        log("=" * 60)
        if args.core_only:
            log("CORE IMPORT DONE (bookings skipped — not a full migration).")
        elif maps.get("migration_complete"):
            log("SUCCESS — FULL MIGRATION COMPLETE")
        else:
            log("PARTIAL — re-run with --resume to finish remaining phases")
        log(f"  Tenant ID:     {args.tenant_id}")
        log(
            "  Tables: users, locations, packages, wallet, user_packages, sales, "
            "class_bookings, gym_classes, fitness_programs, layouts."
        )
        log("=" * 60)
        if not audit_ok:
            log("  Audit reported issues — review counts above.")
        if args.sales_only or args.classes_only or args.programs_only:
            return 0
        if args.core_only:
            return 0
        return 0 if maps.get("migration_complete") else 1

    except Exception as exc:
        log(f"FAILED: {exc}")
        return 1
    finally:
        SessionLocal().close()


if __name__ == "__main__":
    sys.exit(main())
