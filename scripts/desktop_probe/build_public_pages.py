"""Build the offline desktop reader from its reviewed repository documents."""
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = {
    "declaration": "docs/桌面声明.md",
    "wechat": "docs/微信支持范围.md",
    "costs": "docs/模型选择参考.md",
}
INLINE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)|\*\*(.+?)\*\*|`([^`]+)`")


def article(source, page_title=None, *, repository_links=None):
    """Render the documents' headings, paragraphs, lists and pipe tables as escaped HTML."""
    references = {}

    def inline(line):
        result = []
        cursor = 0
        for match in INLINE.finditer(line):
            result.append(html.escape(line[cursor:match.start()]))
            label, target, strong, code = match.groups()
            if label is not None:
                if repository_links is not None:
                    url = urlsplit(target)
                    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                        raise ValueError(f"Expected an HTTPS project link: {target}")
                    repository_links.add(target)
                    result.append('<a href="' + html.escape(target, quote=True) +
                                  '" target="_blank" rel="noopener noreferrer">' + html.escape(label) + '</a>')
                elif target.startswith(("https://", "http://")):
                    result.append(html.escape(label))
                    references[target] = label
                else:
                    result.append(html.escape(label))
            elif strong is not None:
                result.append("<strong>" + html.escape(strong) + "</strong>")
            else:
                result.append("<code>" + html.escape(code) + "</code>")
            cursor = match.end()
        return "".join(result) + html.escape(line[cursor:])

    blocks, paragraph, items = [], [], []
    lines = [line.strip() for line in source.splitlines()] + [""]
    heading = "数据"
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        title = re.fullmatch(r"(#{1,6})\s+(.+)", line)
        item = re.fullmatch(r"(?:[-*]|\d+[.)、])\s+(.+)", line)
        table = line.startswith("|") and i < len(lines) and re.fullmatch(r"[|\s:-]+", lines[i])
        if paragraph and (not line or title or item or table):
            blocks.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if items and not item:
            blocks.append("<ul>" + "".join(items) + "</ul>")
            items.clear()
        if title:
            heading = title[2]
            if len(title[1]) == 1 and heading == page_title:
                continue
            level = max(2, min(4, len(title[1]) + (0 if page_title else 1)))
            blocks.append(f"<h{level}>" + inline(heading) + f"</h{level}>")
        elif item:
            items.append("<li>" + inline(item[1]) + "</li>")
        elif table:
            labels = [cell.strip() for cell in line.strip("|").split("|")]
            def cells(row, tag):
                result = []
                for i, cell in enumerate(row.strip("|").split("|")):
                    label = labels[i] if i < len(labels) else ""
                    attr = 'scope="col"' if tag == "th" else 'data-label="' + html.escape(label) + '"'
                    result.append(f"<{tag} {attr}>" + inline(cell.strip()) + f"</{tag}>")
                return "<tr>" + "".join(result) + "</tr>"
            blocks.append('<div class="reader-table" role="region" tabindex="0" aria-label="' +
                          html.escape(heading, quote=True) + '"><table><thead>' + cells(line, "th") +
                          "</thead><tbody>")
            i += 1
            while i < len(lines) and lines[i].startswith("|"):
                blocks.append(cells(lines[i], "td"))
                i += 1
            blocks.append("</tbody></table></div>")
        elif line:
            paragraph.append(line)
    if references:
        entries = "".join("<li>" + html.escape(label) + "<br><code>" + html.escape(url) +
                          "</code></li>" for url, label in references.items())
        blocks.append("<details><summary>来源地址</summary><ul>" + entries + "</ul></details>")
    return "\n".join(blocks)


def build(destination, allowed_links=None):
    pages = {key: article((ROOT / path).read_text(encoding="utf-8-sig"),
                          "声明" if key == "declaration" else None) for key, path in DOCUMENTS.items()}
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8-sig")
    pages["declaration"] += ("<details><summary>MIT 许可原文</summary><pre>" +
                             html.escape(license_text) + "</pre></details>")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8-sig")
    index = re.search(r"^## 桌面许可索引\s*\n(.*?)(?=^## |\Z)", notices, re.M | re.S)
    if index is None:
        raise ValueError("缺少桌面许可索引")
    urls = set()
    content = article(index[1], repository_links=urls)
    if not urls:
        raise ValueError("桌面许可索引没有上游链接")
    pages["declaration"] += '<details class="reader-upstreams"><summary>第三方项目与许可</summary>' + content + '</details>'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("window.qibanPublicPages = " + json.dumps(pages, ensure_ascii=False) + ";\n",
                           encoding="utf-8")
    if allowed_links:
        allowed_links.write_text("\n".join(sorted(urls)) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
