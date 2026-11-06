#!/root/orca/workspaces/PersonaOS/yabby/.venv/bin/python3
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
    parser = argparse.ArgumentParser(description="Create a page in Notion")
    parser.add_argument("title", nargs="?", default="test", help="Page title (default: test)")
    parser.add_argument("--parent", help="Parent page or database ID")
    parser.add_argument("--content", default="This is a test page created by PersonaOS.", help="Body paragraph text")
    args = parser.parse_args()

    token = get_token()
    notion = Client(auth=token)

    parent_id = args.parent
    if not parent_id:
        print("Searching for accessible parent pages in Notion...")
        res = notion.search(filter={"value": "page", "property": "object"}).get("results", [])
        if not res:
            print("\n[!] No pages or databases are currently shared with the PersonaOS integration in Notion.")
            print("To allow PersonaOS to create pages:")
            print("  1. Open Notion in your browser or desktop app.")
            print("  2. Open any page (or create a new empty page).")
            print("  3. Click the `...` menu (top right) -> `Connect to` -> select `PersonaOS`.")
            print("  4. Re-run this command (or run with --parent <page_id>).\n")
            sys.exit(1)
        parent_id = res[0]["id"]
        parent_title = res[0].get("properties", {}).get("title", {}).get("title", [{}])[0].get("plain_text", "Untitled")
        print(f"Using accessible parent page: \"{parent_title}\" ({parent_id})")

    # Format parent ID (remove hyphens if needed or keep)
    clean_parent = parent_id.replace("-", "")

    try:
        new_page = notion.pages.create(
            parent={"page_id": clean_parent},
            properties={
                "title": [
                    {
                        "text": {
                            "content": args.title
                        }
                    }
                ]
            },
            children=[
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": args.content
                                }
                            }
                        ]
                    }
                }
            ]
        )
        print(f"\nSuccessfully created Notion page \"{args.title}\"!")
        print(f"Page ID: {new_page.get("id")}")
        print(f"URL: {new_page.get("url")}")
    except APIResponseError as e:
        sys.exit(f"Notion API error: {e}")

if __name__ == "__main__":
    main()
