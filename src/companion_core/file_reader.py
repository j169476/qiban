"""MIT: bounded attachment text extraction; text is data, never instructions."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 10 * 1024 * 1024
    expanded_bytes: int = 32 * 1024 * 1024
    entries: int = 2000
    xml_bytes: int = 8 * 1024 * 1024
    chars: int = 100000
    units: int = 2000
    pages: int = 100
    cells: int = 10000


class Rejected(ValueError):
    pass


class Collector:
    def __init__(self, limits):
        self.limits, self.units, self.chars, self.truncated = limits, [], 0, False

    def add(self, source, value, **extra):
        text = str(value).strip()
        if not text:
            return True
        remaining = self.limits.chars - self.chars
        if remaining <= 0 or len(self.units) >= self.limits.units:
            self.truncated = True
            return False
        kept = text[:remaining]
        self.units.append({"source": source, "text": kept, **extra})
        self.chars += len(kept)
        if len(kept) < len(text):
            self.truncated = True
            return False
        return True


def check_office(data, limits):
    from defusedxml import ElementTree

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > limits.entries or sum(x.file_size for x in entries) > limits.expanded_bytes:
            raise Rejected("archive_expansion_limit")
        seen = set()
        for entry in entries:
            name = entry.filename
            if name in seen or ".." in PurePosixPath(name).parts or name.startswith("/") or "\\" in name:
                raise Rejected("invalid_archive_name")
            seen.add(name)
            if entry.flag_bits & 1:
                raise Rejected("encrypted_archive")
            if entry.file_size > 1024 * 1024 and entry.file_size > max(entry.compress_size, 1) * 200:
                raise Rejected("archive_compression_ratio")
            if name.lower().endswith("vbaproject.bin"):
                raise Rejected("macro_content")
            if name.endswith((".xml", ".rels")):
                if entry.file_size > limits.xml_bytes:
                    raise Rejected("xml_size_limit")
                ElementTree.fromstring(archive.read(entry), forbid_dtd=True)


def extract(path, limits=Limits()):
    path = Path(path)
    with path.open('rb') as stream:
        data = stream.read(limits.file_bytes + 1)
    return extract_bytes(path.name, data, limits)


def extract_bytes(name, data, limits=Limits()):
    suffix = Path(name).suffix.lower()
    result = {"format": suffix, "status": "ok", "units": [], "warnings": [],
              "truncated": False, "content_role": "untrusted_document_data"}
    collector = Collector(limits)
    try:
        if len(data) > limits.file_bytes:
            raise Rejected("file_size_limit")
        if suffix in {".doc", ".xls", ".ppt"}:
            result["status"] = "unsupported_legacy_format"
            return result
        if suffix not in {".txt", ".md", ".csv", ".pdf", ".docx", ".xlsx", ".pptx"}:
            result["status"] = "unsupported_format"
            return result
        if suffix in {".txt", ".md", ".csv"}:
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = data.decode(encoding)
            if "\x00" in text:
                raise Rejected("binary_text")
            if suffix == ".csv":
                reader = csv.reader(io.StringIO(text, newline=""), strict=True)
                for row, values in enumerate(reader, 1):
                    if not collector.add({"row": row}, json.dumps(values, ensure_ascii=False)):
                        break
            else:
                for line, value in enumerate(text.splitlines(), 1):
                    if not collector.add({"line": line}, value):
                        break
        elif suffix == ".pdf":
            from pypdf import PdfReader

            document = PdfReader(io.BytesIO(data), strict=True)
            if document.is_encrypted:
                result["status"] = "encrypted"
                return result
            for index, page in enumerate(document.pages):
                if index >= limits.pages:
                    collector.truncated = True
                    break
                text = page.extract_text() or ""
                if not text.strip():
                    result["warnings"].append({"page": index + 1, "code": "no_extractable_text"})
                elif not collector.add({"page": index + 1}, text):
                    break
        else:
            if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
                result["status"] = "encrypted_or_legacy_container"
                return result
            check_office(data, limits)
            if suffix == ".docx":
                from docx import Document
                from docx.table import Table

                result["scope"] = "body_paragraphs_and_tables; no_headers_notes_images"
                for index, block in enumerate(Document(io.BytesIO(data)).iter_inner_content(), 1):
                    if isinstance(block, Table):
                        for row, cells in enumerate(block.rows, 1):
                            for col, cell in enumerate(cells.cells, 1):
                                if not collector.add({"block": index, "table_row": row, "column": col}, cell.text):
                                    break
                            if collector.truncated:
                                break
                    elif not collector.add({"block": index, "paragraph": True}, block.text):
                        break
                    if collector.truncated:
                        break
            elif suffix == ".xlsx":
                from openpyxl import load_workbook

                document = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
                result["scope"] = "cell_values_and_formula_text; formulas_not_evaluated; no_charts_images"
                visited = 0
                try:
                    for sheet in document:
                        for row in sheet.iter_rows():
                            for cell in row:
                                visited += 1
                                if visited > limits.cells:
                                    collector.truncated = True
                                    break
                                if cell.value is not None and not collector.add(
                                    {"sheet": sheet.title, "cell": cell.coordinate}, cell.value,
                                    formula_not_evaluated=cell.data_type == "f",
                                ):
                                    break
                            if collector.truncated:
                                break
                        if collector.truncated:
                            break
                finally:
                    document.close()
            else:
                from pptx import Presentation

                result["scope"] = "slide_text_and_tables; no_notes_images"
                def shapes(items, slide, prefix=""):
                    for index, shape in enumerate(items, 1):
                        source = {"slide": slide, "shape": prefix + str(index)}
                        if hasattr(shape, "shapes"):
                            shapes(shape.shapes, slide, prefix + str(index) + ".")
                        elif shape.has_table:
                            for row, cells in enumerate(shape.table.rows, 1):
                                for col, cell in enumerate(cells.cells, 1):
                                    if not collector.add({**source, "row": row, "column": col}, cell.text):
                                        return
                        elif shape.has_text_frame and not collector.add(source, shape.text):
                            return
                        if collector.truncated:
                            return
                for slide, item in enumerate(Presentation(io.BytesIO(data)).slides, 1):
                    if slide > limits.pages:
                        collector.truncated = True
                        break
                    shapes(item.shapes, slide)
                    if collector.truncated:
                        break
        result.update(units=collector.units, truncated=collector.truncated)
        if collector.truncated:
            result["status"] = "truncated"
        elif result["warnings"]:
            result["status"] = "partial" if collector.units else "needs_ocr_or_empty"
        elif not collector.units:
            result["status"] = "empty"
    except ImportError as error:
        result.update(status="dependency_unavailable", error_type=type(error).__name__)
    except UnicodeDecodeError:
        result["status"] = "unsupported_encoding"
    except Rejected as error:
        result.update(status="rejected", reason=str(error))
    except Exception as error:
        # The probe reports a parser failure; it never retries another parser or
        # turns corrupt input into a successful empty document.
        result.update(status="parse_error", error_type=type(error).__name__)
    return result
