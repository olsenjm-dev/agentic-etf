#!/usr/bin/env python3
"""
Clean text that came back from the Google Drive connector's read_file_content.

  python3 drive_text.py unescape  IN OUT     Google Doc text -> plain text (strips markdown escapes
                                             like \\_ \\[ \\# \\* \\& and collapses doubled newlines);
                                             if OUT ends in .json the result is validated as JSON.
  python3 drive_text.py table2csv IN OUT     Google Sheet markdown table -> CSV

The task writes the connector's fileContent string to IN verbatim, then runs this.
"""
import csv, json, re, sys

ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!&|<>~])")

def unescape(text):
    text = ESC.sub(r"\1", text)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n\n", "\n", text)           # Docs export doubles every newline
    return text.strip() + "\n"

def table2csv(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [ESC.sub(r"\1", c).strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) or c == "" for c in cells):
            continue                                 # alignment row or empty header row
        if all(c == "" for c in cells):
            continue
        rows.append(cells)
    return rows

def main():
    mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    raw = open(src, encoding="utf-8").read()
    # accept either the bare fileContent text or the whole {"fileContent": "..."} JSON blob
    if raw.lstrip().startswith("{") and '"fileContent"' in raw[:40]:
        try:
            raw = json.loads(raw)["fileContent"]
        except Exception:
            pass
    if mode == "unescape":
        out = unescape(raw)
        if dst.endswith(".json"):
            json.loads(out)                          # raises if the round trip corrupted anything
        open(dst, "w", encoding="utf-8").write(out)
    elif mode == "table2csv":
        rows = table2csv(raw)
        with open(dst, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    else:
        sys.exit("mode must be unescape or table2csv")
    print(f"wrote {dst}")

if __name__ == "__main__":
    main()
