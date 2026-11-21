import json
import os
import re
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_VERSION = "2022-06-28"

# Укажи в .env или прямо здесь ID своих баз данных из Notion
DATABASES = {
    "coffee_lots": {"title": "Лоты кофе", "id": os.getenv("DB_COFFEE_LOTS")},
    "regions": {"title": "Регионы", "id": os.getenv("DB_REGIONS")},
    "countries": {"title": "Страны", "id": os.getenv("DB_COUNTRIES")},
    "flavor_notes": {"title": "Вкусовые ноты", "id": os.getenv("DB_FLAVOR_NOTES")},
    "processing": {"title": "Способы обработки", "id": os.getenv("DB_PROCESSING")},
    "brewing": {"title": "Способы приготовления", "id": os.getenv("DB_BREWING")},
    "tastings": {"title": "Журнал дегустаций", "id": os.getenv("DB_TASTINGS")},
    "media": {"title": "Медиатека / Источники", "id": os.getenv("DB_MEDIA")},
}

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "-", text)
    return text or "untitled"


def query_database(database_id: str) -> list[dict]:
    """Забирает все записи из базы данных Notion с учётом пагинации."""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    results = []
    payload = {}

    while True:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return results


def get_page_blocks_text(page_id: str) -> str:
    """Выгружает текстовое содержимое тела страницы (описания, шаги рецептов и т.д.)."""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return ""

    lines = []
    for block in resp.json().get("results", []):
        b_type = block.get("type")
        b_data = block.get(b_type, {})
        rich_text = b_data.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich_text).strip()
        if not text:
            continue

        if b_type.startswith("heading_"):
            level = "#" * int(b_type[-1])
            lines.append(f"\n{level} {text}")
        elif b_type == "bulleted_list_item":
            lines.append(f"- {text}")
        elif b_type == "numbered_list_item":
            lines.append(f"1. {text}")
        else:
            lines.append(text)

    return "\n".join(lines).strip()


def extract_title(properties: dict) -> str:
    for prop in properties.values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", [])).strip()
    return "Без названия"


def parse_property_value(prop: dict, id_index: dict) -> tuple:
    """
    Возвращает кортеж (значение_для_JSON, строка_для_Markdown).
    Для полей relation подставляет читаемые названия и относительные ссылки на файлы.
    """
    p_type = prop.get("type")

    if p_type == "title":
        val = "".join(t.get("plain_text", "") for t in prop.get("title", []))
        return val, val

    if p_type == "rich_text":
        val = "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
        return val, val

    if p_type == "number":
        val = prop.get("number")
        return val, str(val) if val is not None else ""

    if p_type == "select":
        sel = prop.get("select")
        val = sel.get("name") if sel else None
        return val, val or ""

    if p_type == "multi_select":
        vals = [item.get("name") for item in prop.get("multi_select", [])]
        return vals, ", ".join(vals)

    if p_type == "date":
        d = prop.get("date")
        val = d.get("start") if d else None
        return val, val or ""

    if p_type == "url":
        val = prop.get("url")
        return val, f"[{val}]({val})" if val else ""

    if p_type == "files":
        urls = []
        for f in prop.get("files", []):
            if f.get("type") == "external":
                urls.append(f["external"]["url"])
            elif f.get("type") == "file":
                urls.append(f["file"]["url"])
        return urls, ", ".join(urls)

    if p_type == "relation":
        rel_items = []
        md_links = []
        for rel in prop.get("relation", []):
            rel_id = rel["id"]
            meta = id_index.get(rel_id)
            if meta:
                rel_items.append({"id": rel_id, "title": meta["title"], "table": meta["table"]})
                md_links.append(f"[{meta['title']}](../{meta['table']}/{meta['filename']})")
            else:
                rel_items.append({"id": rel_id, "title": rel_id})
                md_links.append(rel_id)
        return rel_items, ", ".join(md_links)

    if p_type == "rollup":
        rollup = prop.get("rollup", {})
        r_type = rollup.get("type")
        if r_type == "number":
            val = rollup.get("number")
            return val, str(val) if val is not None else ""
        if r_type == "array":
            items_json = []
            items_md = []
            for item in rollup.get("array", []):
                j_val, m_val = parse_property_value(item, id_index)
                if j_val not in (None, "", []):
                    items_json.append(j_val)
                    items_md.append(m_val)
            return items_json, ", ".join(items_md)

    if p_type == "formula":
        formula = prop.get("formula", {})
        f_type = formula.get("type")
        val = formula.get(f_type)
        return val, str(val) if val is not None else ""

    return None, ""


def main():
    if not NOTION_TOKEN:
        raise RuntimeError("Не задан NOTION_TOKEN в переменных окружения или файле .env")

    raw_tables = {}
    id_index = {}

    # Шаг 1. Загружаем все таблицы и строим единый справочник page_id -> метаданные
    for table_key, cfg in DATABASES.items():
        db_id = cfg["id"]
        if not db_id:
            print(f"[Пропуск] Не задан ID для таблицы: {cfg['title']}")
            continue

        print(f"Загрузка таблицы: {cfg['title']}...")
        pages = query_database(db_id)
        raw_tables[table_key] = pages

        for page in pages:
            p_id = page["id"]
            title = extract_title(page.get("properties", {}))
            filename = f"{slugify(title)}_{p_id[:8]}.md"
            id_index[p_id] = {
                "title": title,
                "table": table_key,
                "filename": filename,
            }

    # Шаг 2. Формируем граф знаний в JSON и генерируем связанные Markdown-страницы
    knowledge_graph = {}
    docs_root = Path("docs")
    data_root = Path("data")
    docs_root.mkdir(exist_ok=True)
    data_root.mkdir(exist_ok=True)

    for table_key, pages in raw_tables.items():
        table_dir = docs_root / table_key
        table_dir.mkdir(parents=True, exist_ok=True)
        knowledge_graph[table_key] = []

        for page in pages:
            p_id = page["id"]
            meta = id_index[p_id]
            props = page.get("properties", {})

            parsed_props_json = {}
            md_lines = [f"# {meta['title']}\n", "## Свойства и связи\n"]

            for prop_name, prop_obj in props.items():
                if prop_obj.get("type") == "title":
                    continue
                j_val, m_val = parse_property_value(prop_obj, id_index)
                parsed_props_json[prop_name] = j_val
                if m_val:
                    md_lines.append(f"- **{prop_name}:** {m_val}")

            body_text = get_page_blocks_text(p_id)
            if body_text:
                md_lines.append("\n## Описание и содержание\n")
                md_lines.append(body_text)

            # Сохраняем Markdown-файл сущности
            file_path = table_dir / meta["filename"]
            file_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

            knowledge_graph[table_key].append({
                "id": p_id,
                "title": meta["title"],
                "file": f"docs/{table_key}/{meta['filename']}",
                "properties": parsed_props_json,
                "content": body_text,
            })

    # Сохраняем полный JSON-граф связей
    json_path = data_root / "knowledge_base.json"
    json_path.write_text(json.dumps(knowledge_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово! Данные сохранены в {json_path} и папку {docs_root}/")


if __name__ == "__main__":
    main()
