import sqlite3
import streamlit as st
import os

DB_PATH = "papers.db"

def get_connection():
    # Streamlitのスレッド環境でロックを最小化するため、timeoutを少し長めに設定し、
    # 接続設定を標準化します。
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30.0,
    )
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

# 今後、データベース操作の関数（CRUD）をここに追加していきます
