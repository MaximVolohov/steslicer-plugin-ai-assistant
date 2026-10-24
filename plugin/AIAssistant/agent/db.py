import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fdata.sqlite")


def get_connection():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables():
    conn = get_connection()
    if conn is None:
        return {"error": "База данных не найдена: {}".format(DB_PATH)}
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return {"tables": tables}


def query_table(table_name, limit=50):
    conn = get_connection()
    if conn is None:
        return {"error": "База данных не найдена: {}".format(DB_PATH)}
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM {} LIMIT ?".format(table_name), (limit,))
    except Exception as e:
        conn.close()
        return {"error": "Ошибка запроса к таблице {}: {}".format(table_name, str(e))}
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return {"table": table_name, "rows": rows}


def find_params(case_type, subtype=None):
    conn = get_connection()
    if conn is None:
        return {"error": "База данных не найдена: {}".format(DB_PATH)}
    cur = conn.cursor()
    try:
        if subtype:
            cur.execute(
                "SELECT * FROM params WHERE type = ? AND subtype = ?",
                (case_type, subtype)
            )
        else:
            cur.execute("SELECT * FROM params WHERE type = ?", (case_type,))
    except Exception as e:
        conn.close()
        return {"error": "Ошибка запроса параметров: {}".format(str(e))}
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    if not rows:
        return {"error": "Параметры для типа '{}' не найдены".format(case_type)}
    return {"params": rows}