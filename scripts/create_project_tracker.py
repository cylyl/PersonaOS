#!/root/orca/workspaces/PersonaOS/yabby/.venv/bin/python3
"""Create a Project Tracker database in Notion with standard properties."""
import argparse
import sys
from pathlib import Path
from notion_client import Client, APIResponseError

def get_token() -> str:
    paths = [
        Path.home() / ".notion",
        Path("/root/.notion"),
    ]
    for p in paths:
        if p.is_file():
            token = p.read_text().strip()
            if token:
                return token
    sys.exit("Error: No Notion token found in ~/.notion")

def main():
    parser = argparse.ArgumentParser(description="Create a Project Tracker Database in Notion")
    parser.add_argument("--parent", help="Parent Page ID or URL where the tracker should be created")
    parser.add_argument("--name", default="Project Tracker", help="Tracker Title (default: Project Tracker)")
    args = parser.parse_args()

    token = get_token()
    notion = Client(auth=token)

    parent_id = args.parent
    if not parent_id:
        res = notion.search(filter={"value": "page", "property": "object"}).get("results", [])
        if not res:
            sys.exit("[!] No accessible parent page found. Share a page with the 'PersonaOS' connection in Notion first, or pass --parent <page_id>.")
        parent_id = res[0]["id"]
        print(f"Using accessible parent page: {parent_id}")

    # Clean ID
    clean_parent = parent_id.split("/")[-1].split("?")[0].replace("-", "")
    if len(clean_parent) > 32:
        clean_parent = clean_parent[-32:]

    schema = {
        "Task Name": {"title": {}},
        "Status": {
            "status": {
                "options": [
                    {"name": "Not started", "color": "default"},
                    {"name": "In progress", "color": "blue"},
                    {"name": "In review", "color": "yellow"},
                    {"name": "Done", "color": "green"},
                    {"name": "Blocked", "color": "red"},
                ]
            }
        },
        "Priority": {
            "select": {
                "options": [
                    {"name": "Urgent", "color": "red"},
                    {"name": "High", "color": "orange"},
                    {"name": "Medium", "color": "yellow"},
                    {"name": "Low", "color": "gray"},
                ]
            }
        },
        "Due Date": {"date": {}},
        "Category": {
            "multi_select": {
                "options": [
                    {"name": "Core", "color": "blue"},
                    {"name": "Bug", "color": "red"},
                    {"name": "Feature", "color": "green"},
                    {"name": "DevOps", "color": "purple"},
                ]
            }
        },
        "Progress": {"number": {"format": "percent"}},
    }

    try:
        db = notion.databases.create(
            parent={"type": "page_id", "page_id": clean_parent},
            title=[{"type": "text", "text": {"content": args.name}}],
            properties=schema,
        )
        print(f"\n[OK] Project Tracker '{args.name}' successfully created!")
        print(f"Database ID: {db.get('id')}")
        print(f"URL: {db.get('url')}")
    except APIResponseError as e:
        sys.exit(f"Notion API error: {e}")

if __name__ == "__main__":
    main()
