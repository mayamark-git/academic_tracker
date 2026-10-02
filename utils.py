import os
import re
import urllib.parse

def sanitize_name(raw_name: str) -> str:
    """人名からURL・Markdown構文・記号を除去し、純粋な名前文字列のみにする"""
    if not raw_name:
        return ""

    text = urllib.parse.unquote(str(raw_name)).strip()
    text = re.sub(r'https?://[^\s\)\]\"\'>]+', '', text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'[\[\]\(\)\{\}\<\>\"\'`]', '', text)
    m = re.findall(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ffA-Za-z0-9]+', text)

    if m:
        return " ".join(m).strip()

    return text.strip()

def sanitize_society_name(raw_soc: str) -> str:
    """学会名から不要な記号・説明ラベルを除去し、正式な学会名のみを抽出"""
    if not raw_soc:
        return ""

    text = urllib.parse.unquote(str(raw_soc)).strip()
    text = re.sub(r'https?://[^\s\)\]\"\'>]+', '', text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'[\(\)\[\]\{\}\<\>\"\'`]', '', text)
    text = re.sub(r'^(所属学協会活動|所属学協会|所属学会|主な学会|学会名|所属)[:：\s・○*]*', '', text)
    text = re.sub(r'(など|他|所属)$', '', text)
    return text.strip()

def sanitize_url(raw_url: str) -> str:
    """純粋なHTTP/HTTPS URLを取り出す（Markdownリンク構文は返さない）"""
    if not raw_url:
        return ""

    text = str(raw_url).strip()
    m = re.search(r'https?://[^\s\)\]\"\'>]+', text, flags=re.IGNORECASE)

    if not m:
        return ""

    url = m.group(0).rstrip('.,;)]}')
    return url

def sanitize_doi(raw_doi: str) -> str:
    """DOIを正規化してDOI本体のみを返す"""
    if not raw_doi:
        return ""

    text = str(raw_doi).strip()
    markdown_match = re.search(r'\[([^\]]+)\]\([^)]+\)', text)
    if markdown_match:
        text = markdown_match.group(1).strip()

    text = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^doi:\s*', '', text, flags=re.IGNORECASE)
    text = text.strip().rstrip('.,;)]')
    return text
