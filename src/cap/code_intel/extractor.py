"""
CAP Code Intelligence — AST Extractor.

Uses ast-grep (sg) CLI for parsing source files and extracting symbols,
relationships, imports, and exports per language.

Supported languages: python, typescript, javascript, go, rust (NOT HCL).
"""

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cap.code_intel.extractor")

SUPPORTED_LANGUAGES = {"python", "typescript", "javascript", "go", "rust"}

# File extension to language mapping
EXTENSION_MAP = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}

SG_BIN = "sg"
SUBPROCESS_TIMEOUT = 30


@dataclass
class Symbol:
    """A code symbol extracted from a source file."""

    name: str
    kind: str  # 'function', 'class', 'method', 'struct', 'interface', 'trait', 'type', 'import'
    file_path: str
    start_line: int
    end_line: int
    signature: str
    visibility: str = "public"  # 'public', 'private', 'internal'
    parent: Optional[str] = None
    docstring: Optional[str] = None


@dataclass
class Relationship:
    """A relationship between two code symbols."""

    source: str  # qualified name
    target: str  # qualified name or module path
    kind: str  # 'calls', 'imports', 'inherits', 'uses_type', 'implements'
    file_path: str
    line: int


@dataclass
class FileIndex:
    """Complete extraction result for a single file."""

    path: str
    language: str
    hash: str  # content hash for incremental checking
    symbols: list[Symbol] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)


def detect_language(file_path: str) -> Optional[str]:
    """Detect language from file extension."""
    ext = Path(file_path).suffix.lower()
    return EXTENSION_MAP.get(ext)


def content_hash(file_path: str) -> str:
    """Compute SHA-256 hash of file contents.

    Raises:
        OSError: If the file cannot be read (permissions, not found, etc.)
    """
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_sg(args: list[str], cwd: str = None) -> Optional[str]:
    """Run ast-grep and return stdout, or None on failure."""
    cmd = [SG_BIN] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            cwd=cwd,
        )
        if result.returncode == 0 or result.stdout.strip():
            return result.stdout
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("sg command failed: %s", e)
        return None


def _sg_search(pattern: str, language: str, file_path: str) -> list[dict]:
    """Run sg pattern search on a single file, return matches."""
    args = ["run", "--pattern", pattern, "--lang", language, "--json=compact", file_path]
    stdout = _run_sg(args)
    if not stdout or not stdout.strip():
        return []
    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            data = [data]
        return data
    except json.JSONDecodeError:
        # Try line-by-line
        results = []
        for line in stdout.strip().splitlines():
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return results


def extract_file(file_path: str, language: str = None) -> Optional[FileIndex]:
    """
    Parse a single file and extract symbols and relationships.

    Args:
        file_path: Absolute path to the source file.
        language: Language override. If None, detected from extension.

    Returns:
        FileIndex with extracted symbols and relationships, or None if unsupported.
    """
    if not os.path.isfile(file_path):
        return None

    if language is None:
        language = detect_language(file_path)

    if language is None or language not in SUPPORTED_LANGUAGES:
        return None

    file_hash = content_hash(file_path)

    # Dispatch to language-specific extractor
    extractor = _LANGUAGE_EXTRACTORS.get(language)
    if not extractor:
        return None

    symbols, relationships, imports, exports = extractor(file_path, language)

    return FileIndex(
        path=file_path,
        language=language,
        hash=file_hash,
        symbols=symbols,
        relationships=relationships,
        imports=imports,
        exports=exports,
    )


# ─── Python Extraction ────────────────────────────────────────────────────────


def _extract_python(file_path: str, language: str):
    """Extract symbols and relationships from a Python file."""
    symbols = []
    relationships = []
    imports = []
    exports = []

    # Extract function definitions
    matches = _sg_search("def $FUNC($$$ARGS)", language, file_path)
    for m in matches:
        name = _extract_metavar(m, "FUNC")
        if not name:
            name = _extract_name_from_text(m.get("text", ""), "def ")
        start = _get_start_line(m)
        end = _get_end_line(m)
        sig = m.get("text", "").split("\n")[0].strip()

        # Determine if it's a method (inside a class) by checking indentation
        is_method = _is_indented(m)
        kind = "method" if is_method else "function"
        visibility = "private" if name and name.startswith("_") else "public"

        if name:
            symbols.append(Symbol(
                name=name,
                kind=kind,
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
            ))

    # Extract class definitions
    matches = _sg_search("class $CLASS", language, file_path)
    seen_classes = set()
    for m in matches:
        name = _extract_metavar(m, "CLASS")
        if not name:
            name = _extract_name_from_text(m.get("text", ""), "class ")
        start = _get_start_line(m)
        end = _get_end_line(m)
        sig = m.get("text", "").split("\n")[0].strip()

        if name and name not in seen_classes:
            seen_classes.add(name)
            symbols.append(Symbol(
                name=name,
                kind="class",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility="public",
            ))

            # Extract inheritance relationships from the signature text
            bases = _extract_bases_from_signature(sig)
            for base in bases:
                relationships.append(Relationship(
                    source=name,
                    target=base,
                    kind="inherits",
                    file_path=file_path,
                    line=start,
                ))

    # Extract imports
    import_matches = _sg_search("import $MOD", language, file_path)
    for m in import_matches:
        mod = _extract_metavar(m, "MOD")
        if mod:
            imports.append(mod)
            relationships.append(Relationship(
                source=Path(file_path).stem,
                target=mod,
                kind="imports",
                file_path=file_path,
                line=_get_start_line(m),
            ))

    from_matches = _sg_search("from $MOD import $NAME", language, file_path)
    seen_from_imports = set()
    for m in from_matches:
        mod = _extract_metavar(m, "MOD")
        if mod and mod not in seen_from_imports:
            seen_from_imports.add(mod)
            imports.append(mod)
            relationships.append(Relationship(
                source=Path(file_path).stem,
                target=mod,
                kind="imports",
                file_path=file_path,
                line=_get_start_line(m),
            ))

    # Extract function calls as relationships
    call_matches = _sg_search("$FUNC($$$ARGS)", language, file_path)
    for m in call_matches:
        func_name = _extract_metavar(m, "FUNC")
        if func_name and not func_name.startswith(("(", "[", "{", "\"", "'")):
            # Filter out common non-call patterns
            if func_name not in ("if", "for", "while", "with", "elif", "return", "print"):
                relationships.append(Relationship(
                    source=Path(file_path).stem,
                    target=func_name,
                    kind="calls",
                    file_path=file_path,
                    line=_get_start_line(m),
                ))

    # Extract decorated functions/classes
    decorator_matches = _sg_search("@$DECORATOR", language, file_path)
    for m in decorator_matches:
        dec = _extract_metavar(m, "DECORATOR")
        if dec:
            relationships.append(Relationship(
                source=Path(file_path).stem,
                target=dec,
                kind="uses_type",
                file_path=file_path,
                line=_get_start_line(m),
            ))

    # All top-level defs are exports
    for sym in symbols:
        if sym.visibility == "public":
            exports.append(sym.name)

    return symbols, relationships, imports, exports


# ─── TypeScript/JavaScript Extraction ─────────────────────────────────────────


def _extract_typescript(file_path: str, language: str):
    """Extract symbols and relationships from a TypeScript/JavaScript file."""
    symbols = []
    relationships = []
    imports = []
    exports = []

    # Extract function declarations
    for pattern in [
        "function $FUNC($$$ARGS) { $$$BODY }",
        "function $FUNC($$$ARGS): $RET { $$$BODY }",
        "const $FUNC = ($$$ARGS) => { $$$BODY }",
        "const $FUNC = ($$$ARGS): $RET => { $$$BODY }",
        "const $FUNC = ($$$ARGS) => $EXPR",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "FUNC")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            symbols.append(Symbol(
                name=name,
                kind="function",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility="public",
            ))

    # Extract class declarations
    for pattern in [
        "class $CLASS { $$$BODY }",
        "class $CLASS extends $BASE { $$$BODY }",
        "class $CLASS implements $IFACE { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "CLASS")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            symbols.append(Symbol(
                name=name,
                kind="class",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility="public",
            ))
            # Inheritance
            base = _extract_metavar(m, "BASE")
            if base:
                relationships.append(Relationship(
                    source=name,
                    target=base,
                    kind="inherits",
                    file_path=file_path,
                    line=start,
                ))
            iface = _extract_metavar(m, "IFACE")
            if iface:
                relationships.append(Relationship(
                    source=name,
                    target=iface,
                    kind="implements",
                    file_path=file_path,
                    line=start,
                ))

    # Extract interfaces (TypeScript only)
    if language == "typescript":
        for pattern in [
            "interface $IFACE { $$$BODY }",
            "interface $IFACE extends $BASE { $$$BODY }",
        ]:
            matches = _sg_search(pattern, language, file_path)
            for m in matches:
                name = _extract_metavar(m, "IFACE")
                if not name:
                    continue
                start = _get_start_line(m)
                end = _get_end_line(m)
                sig = m.get("text", "").split("\n")[0].strip()
                symbols.append(Symbol(
                    name=name,
                    kind="interface",
                    file_path=file_path,
                    start_line=start,
                    end_line=end,
                    signature=sig,
                    visibility="public",
                ))
                base = _extract_metavar(m, "BASE")
                if base:
                    relationships.append(Relationship(
                        source=name,
                        target=base,
                        kind="inherits",
                        file_path=file_path,
                        line=start,
                    ))

        # Extract type aliases
        matches = _sg_search("type $NAME = $$$DEF", language, file_path)
        for m in matches:
            name = _extract_metavar(m, "NAME")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            symbols.append(Symbol(
                name=name,
                kind="type",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility="public",
            ))

    # Extract imports
    for pattern in [
        "import $$$NAMES from '$MOD'",
        'import $$$NAMES from "$MOD"',
        "import '$MOD'",
        'import "$MOD"',
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            mod = _extract_metavar(m, "MOD")
            if mod:
                imports.append(mod)
                relationships.append(Relationship(
                    source=Path(file_path).stem,
                    target=mod,
                    kind="imports",
                    file_path=file_path,
                    line=_get_start_line(m),
                ))

    # Extract export statements as exports
    export_matches = _sg_search("export $$$DECL", language, file_path)
    for m in export_matches:
        text = m.get("text", "")
        # Try to extract the name from export statement
        for keyword in ("function ", "class ", "const ", "let ", "var ", "interface ", "type ", "enum "):
            if keyword in text:
                rest = text.split(keyword, 1)[1]
                name = rest.split("(")[0].split("{")[0].split("=")[0].split("<")[0].split(":")[0].strip()
                if name:
                    exports.append(name)
                    break

    return symbols, relationships, imports, exports


# ─── Go Extraction ────────────────────────────────────────────────────────────


def _extract_go(file_path: str, language: str):
    """Extract symbols and relationships from a Go file."""
    symbols = []
    relationships = []
    imports = []
    exports = []

    # Extract function declarations
    for pattern in [
        "func $FUNC($$$ARGS) $$$RET { $$$BODY }",
        "func $FUNC($$$ARGS) { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "FUNC")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            visibility = "public" if name[0].isupper() else "private"
            symbols.append(Symbol(
                name=name,
                kind="function",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
            ))
            if visibility == "public":
                exports.append(name)

    # Extract method declarations (receiver functions)
    for pattern in [
        "func ($RECV $TYPE) $METHOD($$$ARGS) $$$RET { $$$BODY }",
        "func ($RECV $TYPE) $METHOD($$$ARGS) { $$$BODY }",
        "func ($RECV *$TYPE) $METHOD($$$ARGS) $$$RET { $$$BODY }",
        "func ($RECV *$TYPE) $METHOD($$$ARGS) { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "METHOD")
            recv_type = _extract_metavar(m, "TYPE")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            visibility = "public" if name[0].isupper() else "private"
            symbols.append(Symbol(
                name=name,
                kind="method",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
                parent=recv_type,
            ))

    # Extract struct definitions
    matches = _sg_search("type $NAME struct { $$$FIELDS }", language, file_path)
    for m in matches:
        name = _extract_metavar(m, "NAME")
        if not name:
            continue
        start = _get_start_line(m)
        end = _get_end_line(m)
        sig = f"type {name} struct"
        visibility = "public" if name[0].isupper() else "private"
        symbols.append(Symbol(
            name=name,
            kind="struct",
            file_path=file_path,
            start_line=start,
            end_line=end,
            signature=sig,
            visibility=visibility,
        ))
        if visibility == "public":
            exports.append(name)

    # Extract interface definitions
    matches = _sg_search("type $NAME interface { $$$METHODS }", language, file_path)
    for m in matches:
        name = _extract_metavar(m, "NAME")
        if not name:
            continue
        start = _get_start_line(m)
        end = _get_end_line(m)
        sig = f"type {name} interface"
        visibility = "public" if name[0].isupper() else "private"
        symbols.append(Symbol(
            name=name,
            kind="interface",
            file_path=file_path,
            start_line=start,
            end_line=end,
            signature=sig,
            visibility=visibility,
        ))
        if visibility == "public":
            exports.append(name)

    # Extract imports
    import_matches = _sg_search('import "$PKG"', language, file_path)
    for m in import_matches:
        pkg = _extract_metavar(m, "PKG")
        if pkg:
            imports.append(pkg)
            relationships.append(Relationship(
                source=Path(file_path).stem,
                target=pkg,
                kind="imports",
                file_path=file_path,
                line=_get_start_line(m),
            ))

    # Block imports
    block_matches = _sg_search("import ($$$PKGS)", language, file_path)
    for m in block_matches:
        text = m.get("text", "")
        for line in text.splitlines():
            line = line.strip().strip('"').strip("'")
            if line and line not in ("import (", ")", "import"):
                # Handle aliased imports: alias "pkg"
                parts = line.split('"')
                if len(parts) >= 2:
                    pkg = parts[1]
                else:
                    pkg = line.strip()
                if pkg:
                    imports.append(pkg)
                    relationships.append(Relationship(
                        source=Path(file_path).stem,
                        target=pkg,
                        kind="imports",
                        file_path=file_path,
                        line=_get_start_line(m),
                    ))

    return symbols, relationships, imports, exports


# ─── Rust Extraction ──────────────────────────────────────────────────────────


def _extract_rust(file_path: str, language: str):
    """Extract symbols and relationships from a Rust file."""
    symbols = []
    relationships = []
    imports = []
    exports = []

    # Extract function declarations
    for pattern in [
        "fn $FUNC($$$ARGS) -> $RET { $$$BODY }",
        "fn $FUNC($$$ARGS) { $$$BODY }",
        "pub fn $FUNC($$$ARGS) -> $RET { $$$BODY }",
        "pub fn $FUNC($$$ARGS) { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "FUNC")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            visibility = "public" if "pub " in sig else "private"
            symbols.append(Symbol(
                name=name,
                kind="function",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
            ))
            if visibility == "public":
                exports.append(name)

    # Extract struct definitions
    for pattern in [
        "struct $NAME { $$$FIELDS }",
        "pub struct $NAME { $$$FIELDS }",
        "struct $NAME($$$FIELDS);",
        "pub struct $NAME($$$FIELDS);",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "NAME")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            visibility = "public" if "pub " in sig else "private"
            symbols.append(Symbol(
                name=name,
                kind="struct",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
            ))
            if visibility == "public":
                exports.append(name)

    # Extract trait definitions
    for pattern in [
        "trait $NAME { $$$BODY }",
        "pub trait $NAME { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            name = _extract_metavar(m, "NAME")
            if not name:
                continue
            start = _get_start_line(m)
            end = _get_end_line(m)
            sig = m.get("text", "").split("\n")[0].strip()
            visibility = "public" if "pub " in sig else "private"
            symbols.append(Symbol(
                name=name,
                kind="trait",
                file_path=file_path,
                start_line=start,
                end_line=end,
                signature=sig,
                visibility=visibility,
            ))

    # Extract impl blocks
    for pattern in [
        "impl $TYPE { $$$BODY }",
        "impl $TRAIT for $TYPE { $$$BODY }",
    ]:
        matches = _sg_search(pattern, language, file_path)
        for m in matches:
            trait = _extract_metavar(m, "TRAIT")
            type_name = _extract_metavar(m, "TYPE")
            if trait and type_name:
                relationships.append(Relationship(
                    source=type_name,
                    target=trait,
                    kind="implements",
                    file_path=file_path,
                    line=_get_start_line(m),
                ))

    # Extract use statements
    use_matches = _sg_search("use $$$PATH;", language, file_path)
    for m in use_matches:
        text = m.get("text", "").strip()
        if text.startswith("use "):
            path = text[4:].rstrip(";").strip()
            imports.append(path)
            relationships.append(Relationship(
                source=Path(file_path).stem,
                target=path,
                kind="imports",
                file_path=file_path,
                line=_get_start_line(m),
            ))

    return symbols, relationships, imports, exports


# ─── Helper Functions ─────────────────────────────────────────────────────────


def _extract_metavar(match: dict, var_name: str) -> Optional[str]:
    """Extract a metavariable value from an ast-grep match.

    ast-grep v0.44+ uses nested structure:
      metaVariables: { single: { NAME: {text, range} }, multi: { NAME: [{text, range}...] }, transformed: {} }
    Older versions use flat structure:
      metaVariables: { NAME: {text, range} } or { NAME: "value" }
    """
    meta = match.get("metaVariables", {})
    if not meta:
        meta = match.get("meta_variables", {})
    if not meta:
        return None

    # Handle nested structure (v0.44+)
    single = meta.get("single", {})
    multi = meta.get("multi", {})

    # Check in 'single' first
    if single and var_name in single:
        var = single[var_name]
        if isinstance(var, dict):
            return var.get("text", "")
        if isinstance(var, str):
            return var
        return None

    # Check in 'multi' (for $$$ variables)
    if multi and var_name in multi:
        var = multi[var_name]
        if isinstance(var, list):
            texts = []
            for item in var:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return ", ".join(texts) if texts else None
        if isinstance(var, dict):
            return var.get("text", "")
        if isinstance(var, str):
            return var
        return None

    # Fallback: flat structure (older versions)
    var = meta.get(var_name) or meta.get(f"${var_name}")
    if var is None:
        return None

    if isinstance(var, dict):
        return var.get("text", "")
    if isinstance(var, str):
        return var
    if isinstance(var, list):
        texts = []
        for item in var:
            if isinstance(item, dict):
                texts.append(item.get("text", ""))
            elif isinstance(item, str):
                texts.append(item)
        return ", ".join(texts) if texts else None
    return None


def _extract_bases_from_signature(sig: str) -> list[str]:
    """Extract base class names from a class signature like 'class Foo(Bar, Baz):'."""
    if "(" not in sig or ")" not in sig:
        return []
    try:
        inside = sig.split("(", 1)[1].rsplit(")", 1)[0]
        bases = []
        for base in inside.split(","):
            base = base.strip()
            # Remove keyword args like metaclass=ABC
            if "=" in base:
                continue
            if base:
                bases.append(base)
        return bases
    except (IndexError, ValueError):
        return []


def _extract_name_from_text(text: str, prefix: str) -> Optional[str]:
    """Extract a name from text after a given prefix."""
    if prefix not in text:
        return None
    rest = text.split(prefix, 1)[1]
    # Take first word-like token
    name = ""
    for ch in rest:
        if ch.isalnum() or ch == "_":
            name += ch
        else:
            break
    return name if name else None


def _get_start_line(match: dict) -> int:
    """Extract start line from ast-grep match."""
    rng = match.get("range", {})
    start = rng.get("start", {})
    return start.get("line", 0)


def _get_end_line(match: dict) -> int:
    """Extract end line from ast-grep match."""
    rng = match.get("range", {})
    end = rng.get("end", {})
    return end.get("line", 0)


def _is_indented(match: dict) -> bool:
    """Check if a match starts with indentation (likely inside a class)."""
    rng = match.get("range", {})
    start = rng.get("start", {})
    col = start.get("column", 0)
    return col > 0


# Language extractor dispatch table
_LANGUAGE_EXTRACTORS = {
    "python": _extract_python,
    "typescript": _extract_typescript,
    "javascript": _extract_typescript,  # JS uses same patterns
    "go": _extract_go,
    "rust": _extract_rust,
}
