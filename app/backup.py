"""Full backup of the database, and restoring it into a new one.

A backup is a ZIP:

* ``tables/NN_<table>.csv`` - every row of every table in ``public`` (CSV with a header row),
  numbered parents first so foreign keys hold while restoring;
* ``manifest.json`` - tables, row counts, columns, sequences and the SQL files that had run;
* ``restore.sql`` - the same restore for ``psql`` (``psql "$URL" -f restore.sql`` in the unzipped folder);
* ``README.txt`` - how to restore, in Thai.

The rows are read in one REPEATABLE READ transaction, so the files agree with each other even while
people keep booking. A backup holds everything in the database, patients' HN and names included:
it must be kept as carefully as the medical records.

Command line (e.g. a nightly job on a hospital server; the URL comes from the environment, never argv)::

    DATABASE_URL=... python -m app.backup dump orchestra-backup.zip
    DATABASE_URL=... python -m app.backup restore orchestra-backup.zip   # into an empty database
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .store import StoreError

FORMAT = 1
SPOOL = 16 * 1024 * 1024  # keep small backups in memory, bigger ones in a temporary file


def _name(qualified: str) -> str:
    return qualified.split(".", 1)[-1].strip('"')


def table_order(conn) -> list[str]:
    """Tables of ``public``, every table after the tables its foreign keys point to."""
    tables = [r["relname"] for r in conn.execute(
        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' ORDER BY relname")]
    parents: dict[str, set[str]] = {t: set() for t in tables}
    for r in conn.execute("""SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent
                             FROM pg_constraint WHERE contype = 'f' AND connamespace = 'public'::regnamespace"""):
        child, parent = _name(r["child"]), _name(r["parent"])
        if child in parents and parent in parents and child != parent:
            parents[child].add(parent)
    order: list[str] = []
    while len(order) < len(tables):
        ready = [t for t in tables if t not in order and parents[t] <= set(order)]
        if not ready:  # a cycle (none today): the rest in name order
            ready = [t for t in tables if t not in order]
            order.extend(ready)
            break
        order.extend(ready)
    return order


def _sequences(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT table_name AS table, column_name AS column,
                  pg_get_serial_sequence(format('public.%I', table_name), column_name) AS sequence
           FROM information_schema.columns
           WHERE table_schema = 'public' AND column_default LIKE 'nextval(%'
           ORDER BY table_name, column_name""") if r["sequence"]]  # no parameters: % is sent as is


def _columns(conn, table: str) -> list[str]:
    return [r["column_name"] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s
           ORDER BY ordinal_position""", (table,))]


def dump(url: str) -> tuple[tempfile.SpooledTemporaryFile, dict]:
    """Write a backup; returns the (rewound) file and its manifest."""
    from . import system_status

    out = tempfile.SpooledTemporaryFile(max_size=SPOOL)
    try:
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=15, prepare_threshold=None,
                               **_ssl(url))
    except psycopg.Error as exc:
        raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {str(exc).splitlines()[0]}") from exc
    with conn, zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        order = table_order(conn)
        manifest = {
            "format": FORMAT, "app": "ORchestraAI",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or None,
            "migrations": [m["name"] for m in system_status.check(conn) if m["status"] == "applied"],
            "tables": [], "sequences": _sequences(conn),
        }
        for i, table in enumerate(order, 1):
            columns = _columns(conn, table)
            file = f"tables/{i:02d}_{table}.csv"
            query = sql.SQL("COPY (SELECT {cols} FROM {table}) TO STDOUT WITH (FORMAT csv, HEADER)").format(
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns), table=sql.Identifier("public", table))
            with zf.open(file, "w") as f, conn.cursor() as cur:
                with cur.copy(query) as copy:
                    for chunk in copy:
                        f.write(chunk)
                rows = cur.rowcount
            manifest["tables"].append({"name": table, "file": file, "rows": rows, "columns": columns})
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.writestr("restore.sql", restore_sql(manifest))
        zf.writestr("README.txt", README.format(created=manifest["created_at"],
                                                rows=sum(t["rows"] for t in manifest["tables"]),
                                                tables=len(manifest["tables"])))
    out.seek(0)
    return out, manifest


def restore_sql(manifest: dict) -> str:
    lines = ["-- ORchestraAI: restore this backup into an EMPTY database that already has every SQL file run.",
             "-- In the unzipped folder:  psql \"$DATABASE_URL\" -f restore.sql", r"\set ON_ERROR_STOP on", "BEGIN;"]
    for t in manifest["tables"]:
        cols = ", ".join(f'"{c}"' for c in t["columns"])
        lines.append("\\copy public.\"" + t["name"] + f"\" ({cols}) FROM '{t['file']}' WITH (FORMAT csv, HEADER)")
    for s in manifest["sequences"]:
        lines.append(f"SELECT setval('{s['sequence']}', COALESCE((SELECT max(\"{s['column']}\") FROM "
                     f"public.\"{s['table']}\"), 0) + 1, false);")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def restore(url: str, archive, *, into_empty: bool = True) -> dict:
    """Load a backup ZIP (path or file) into the database at ``url``; every table must exist and be empty."""
    try:
        zf = zipfile.ZipFile(archive)
        manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise StoreError("ไม่ใช่ไฟล์สำรองข้อมูลของ ORchestraAI") from exc
    if manifest.get("format") != FORMAT:
        raise StoreError(f"ไฟล์สำรองรูปแบบ {manifest.get('format')} ไม่รองรับ")
    try:
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=15, prepare_threshold=None, **_ssl(url))
    except psycopg.Error as exc:
        raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {str(exc).splitlines()[0]}") from exc
    loaded = {}
    with conn:
        have = set(table_order(conn))
        missing = [t["name"] for t in manifest["tables"] if t["name"] not in have]
        if missing:
            raise StoreError("ฐานข้อมูลปลายทางยังไม่มีตาราง " + ", ".join(missing[:6])
                             + " · รัน SQL ทุกไฟล์ใน supabase/migrations ก่อน")
        if into_empty:
            full = [t["name"] for t in manifest["tables"]
                    if conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {}) AS x").format(
                        sql.Identifier("public", t["name"]))).fetchone()["x"]]
            if full:
                raise StoreError("ฐานข้อมูลปลายทางมีข้อมูลอยู่แล้ว (" + ", ".join(full[:6])
                                 + ") · กู้คืนได้เฉพาะในฐานข้อมูลใหม่ที่ว่างเปล่า")
        try:
            for t in manifest["tables"]:
                cols = sql.SQL(", ").join(sql.Identifier(c) for c in t["columns"])
                query = sql.SQL("COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER)").format(
                    table=sql.Identifier("public", t["name"]), cols=cols)
                with conn.cursor() as cur, cur.copy(query) as copy, zf.open(t["file"]) as f:
                    while chunk := f.read(1 << 16):
                        copy.write(chunk)
                loaded[t["name"]] = t["rows"]
            for s in manifest["sequences"]:
                if s["table"] in loaded:
                    conn.execute(sql.SQL("SELECT setval(%s, COALESCE((SELECT max({col}) FROM {table}), 0) + 1, false)")
                                 .format(col=sql.Identifier(s["column"]), table=sql.Identifier("public", s["table"])),
                                 (s["sequence"],))
        except psycopg.Error as exc:
            raise StoreError(f"กู้คืนไม่สำเร็จ (ยกเลิกทั้งหมด ไม่มีอะไรถูกบันทึก): {str(exc).splitlines()[0]}") from exc
    return {"tables": len(loaded), "rows": sum(loaded.values()), "created_at": manifest.get("created_at")}


def _ssl(url: str) -> dict:
    local = any(h in url for h in ("localhost", "127.0.0.1", "/var/run", "host=/"))
    return {} if "sslmode=" in url or local else {"sslmode": "require"}


def patients_in(manifest: dict) -> int:
    return next((t["rows"] for t in manifest["tables"] if t["name"] == "surgery_requests"), 0)


README = """ไฟล์สำรองข้อมูล ORchestraAI
สร้างเมื่อ {created} (UTC) · {tables} ตาราง · {rows} แถว

ไฟล์นี้มีข้อมูลทุกอย่างในฐานข้อมูล รวม HN และชื่อผู้ป่วย บัญชีผู้ใช้ (รหัสผ่านเก็บแบบ hash)
เก็บไว้ในที่ปลอดภัยของโรงพยาบาลเหมือนเวชระเบียน อย่าส่งทางแชตหรืออีเมล

กู้คืน (ในฐานข้อมูลใหม่ที่ว่างเปล่า เช่น โปรเจกต์ Supabase ใหม่):
1. รัน SQL ทุกไฟล์ใน supabase/migrations ตามลำดับ (หรือเปิดแอปที่ต่อกับฐานข้อมูลใหม่ แล้วกด
   "รัน SQL ที่ยังขาด" ในหน้า ⚙ สถานะระบบ)
2. วิธีที่ 1 (มี Python และโค้ด ORchestraAI):
     DATABASE_URL="<connection string ของฐานข้อมูลใหม่>" python -m app.backup restore ไฟล์นี้.zip
   วิธีที่ 2 (มี psql): แตกไฟล์ zip แล้วในโฟลเดอร์นั้นรัน
     psql "<connection string ของฐานข้อมูลใหม่>" -f restore.sql
3. ตั้ง DATABASE_URL ของแอปใน Render ให้ชี้ไปฐานข้อมูลใหม่

การกู้คืนทำในทรานแซกชันเดียว ถ้าผิดพลาดจะยกเลิกทั้งหมด
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["dump", "restore"])
    parser.add_argument("file", help="ไฟล์ .zip ที่จะเขียน (dump) หรืออ่าน (restore)")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ตั้ง DATABASE_URL ก่อน (ไม่ใส่ connection string ในคำสั่ง เพื่อไม่ให้รหัสผ่านค้างในประวัติคำสั่ง)",
              file=sys.stderr)
        return 2
    try:
        if args.action == "dump":
            out, manifest = dump(url)
            with open(args.file, "wb") as f:
                while chunk := out.read(1 << 16):
                    f.write(chunk)
            print(f"สำรองแล้ว: {len(manifest['tables'])} ตาราง {sum(t['rows'] for t in manifest['tables'])} แถว → {args.file}")
        else:
            done = restore(url, args.file)
            print(f"กู้คืนแล้ว: {done['tables']} ตาราง {done['rows']} แถว (สำรองเมื่อ {done['created_at']})")
    except StoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

