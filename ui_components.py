import os
import sqlite3
import streamlit as st
from database import get_connection

def toggle_paper_read_status(paper_id: int, current_status: int):
    """論文の既読・未読状態を切り替える"""
    new_status = 0 if current_status else 1
    with get_connection() as conn:
        conn.execute("UPDATE papers SET is_read = ? WHERE id = ?", (new_status, paper_id))
    st.rerun()

def delete_paper(paper_id: int, title: str):
    """論文を削除し、除外リストに登録する"""
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO ignored_papers (title) VALUES (?)", (title,))
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    st.rerun()
