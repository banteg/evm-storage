"""Stdlib-only worker loaded inside an exact, isolated Vyper environment.

This file intentionally avoids importing any other :mod:`evm_storage` module.
It must remain compatible with Python 3.8 for historical compiler releases.
"""

import contextlib
import json
import sys


def _version():
    import vyper

    return getattr(vyper, "__version__", "unknown")


def _words(typ):
    for name in ("storage_size_in_words", "size_in_words"):
        value = getattr(typ, name, None)
        if isinstance(value, int):
            return max(1, value)
    value = getattr(typ, "size_in_bytes", None)
    if isinstance(value, int):
        return max(1, (value + 31) // 32)
    getter = getattr(typ, "get_size_in", None)
    if callable(getter):
        try:
            from vyper.semantics.types import DataLocation

            return max(1, int(getter(DataLocation.STORAGE)))
        except Exception:
            pass
    return 1


def _render_type(typ, definitions, seen=None):
    if seen is None:
        seen = set()
    marker = id(typ)
    if marker in seen:
        return getattr(typ, "name", typ.__class__.__name__)
    seen = set(seen)
    seen.add(marker)

    key = getattr(typ, "keytype", None)
    value = getattr(typ, "valuetype", None)
    if key is None:
        key = getattr(typ, "key_type", None)
    if value is None:
        value = getattr(typ, "value_type", None)
    cls_name = typ.__class__.__name__.lower()
    if key is not None and value is not None and ("map" in cls_name or "mapping" in cls_name):
        rendered_key = _render_type(key, definitions, seen)
        rendered_value = _render_type(value, definitions, seen)
        return f"HashMap[{rendered_key}, {rendered_value}]"

    members = None
    if "struct" in cls_name:
        members = getattr(typ, "member_types", None)
        if members is None:
            members = getattr(typ, "members", None)
        if members is None and hasattr(typ, "tuple_items"):
            try:
                members = dict(typ.tuple_items())
            except Exception:
                members = None
    if isinstance(members, dict) and members:
        label = getattr(typ, "name", None) or getattr(typ, "_id", None) or str(typ)
        label = str(label)
        offset = 0
        serialized = []
        for name, member_type in members.items():
            rendered = _render_type(member_type, definitions, seen)
            size = _words(member_type)
            serialized.append(
                {"name": str(name), "type": rendered, "slot": offset, "n_slots": size}
            )
            offset += size
        definitions[label] = {"members": serialized, "n_slots": offset}
        return label

    subtype = getattr(typ, "subtype", None)
    if subtype is None and "array" in cls_name:
        subtype = getattr(typ, "value_type", None)
    count = getattr(typ, "count", None)
    if count is None:
        count = getattr(typ, "max_count", None)
    if subtype is not None and isinstance(count, int):
        inner = _render_type(subtype, definitions, seen)
        if cls_name.startswith(("darray", "dynamic")):
            return f"DynArray[{inner}, {count}]"
        return f"{inner}[{count}]"

    # Modern Vyper types render correctly.  Old MappingType is handled above,
    # avoiding the historical key/value reversal in __repr__.
    return str(typ)


def _native_layout(source):
    import vyper

    compile_code = vyper.compile_code
    attempts = (
        lambda: compile_code(source, output_formats=["layout"]),
        lambda: compile_code(source, ["layout"]),
        lambda: compile_code(source, contract_path="<stdin>", output_formats=["layout"]),
    )
    last = None
    for attempt in attempts:
        try:
            result = attempt()
            layout = result.get("layout")
            if isinstance(layout, dict):
                return layout
        except Exception as exc:
            last = exc
    if last is not None:
        raise last
    raise RuntimeError("compiler returned no layout")


def _compiler_data(source):
    from vyper.compiler.phases import CompilerData

    attempts = (
        lambda: CompilerData(source),
        lambda: CompilerData(source, "<stdin>"),
        lambda: CompilerData(source, "<stdin>", None, 0),
    )
    last = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last = exc
    raise last


def _legacy_global_context(source):
    definitions = {}
    data = None
    try:
        data = _compiler_data(source)
        context = data.global_ctx
    except (ImportError, ModuleNotFoundError):
        from vyper.ast import parse_to_ast
        from vyper.parser.global_context import GlobalContext

        context = GlobalContext.get_global_context(parse_to_ast(source))

    layout = {}
    for name, record in context._globals.items():
        typ = record.typ
        layout[str(name)] = {
            "type": _render_type(typ, definitions),
            "location": "storage",
            "slot": int(record.pos),
            # These compilers allocate one root slot per top-level declaration;
            # compound values use the historical hashed-storage dialect.
            "n_slots": 1,
        }
    # Reentrancy salts are allocated during code generation in this epoch.
    if data is not None:
        with contextlib.suppress(Exception):
            _ = data.lll_nodes
    for key, slot in getattr(context, "_nonrentrant_keys", {}).items():
        layout[f"nonreentrant.{key}"] = {
            "type": "nonreentrant lock",
            "location": "storage",
            "slot": int(slot),
            "n_slots": 1,
        }
    return layout, definitions, "legacy-hashed"


def _metadata_position(position):
    for value in (position, getattr(position, "position", None), getattr(position, "slot", None)):
        if isinstance(value, int):
            return value
    raise RuntimeError("could not extract compiler-assigned storage position")


def _annotated_layout(source):
    from vyper import ast as vy_ast

    definitions = {}
    data = _compiler_data(source)
    # Modern compilers allocate positions lazily while producing storage_layout.
    with contextlib.suppress(AttributeError):
        _ = data.storage_layout
    module = getattr(data, "annotated_vyper_module", None)
    if module is None:
        module = data.vyper_module_folded
    layout = {}
    node_types = [vy_ast.AnnAssign]
    variable_decl = getattr(vy_ast, "VariableDecl", None)
    if variable_decl is not None:
        node_types.append(variable_decl)
    nodes = []
    for node_type in node_types:
        nodes.extend(module.get_children(node_type))
    for node in nodes:
        metadata = getattr(node.target, "_metadata", {})
        varinfo = metadata.get("varinfo")
        typ = metadata.get("type") or (varinfo.typ if varinfo is not None else None)
        if typ is None:
            continue
        position = getattr(typ, "position", None)
        if position is None and varinfo is not None:
            position = varinfo.position
        layout[str(node.target.id)] = {
            "type": _render_type(typ, definitions),
            "location": "storage",
            "slot": _metadata_position(position),
            "n_slots": _words(typ),
        }
    found_function_locks = False
    for node in module.get_children(vy_ast.FunctionDef):
        function_type = getattr(node, "_metadata", {}).get("type")
        key = getattr(function_type, "nonreentrant", None)
        position = getattr(function_type, "reentrancy_key_position", None)
        if key is None or position is None:
            continue
        found_function_locks = True
        layout[f"$.nonreentrant.{key}@{node.name}"] = {
            "type": "nonreentrant lock",
            "location": "storage",
            "slot": _metadata_position(position),
            "n_slots": 1,
        }
    if not found_function_locks:
        try:
            context = data.global_ctx
            _ = data.lll_nodes
            for key, slot in context._nonrentrant_keys.items():
                layout[f"nonreentrant.{key}"] = {
                    "type": "nonreentrant lock",
                    "location": "storage",
                    "slot": int(slot),
                    "n_slots": 1,
                }
        except Exception:
            pass
    return layout, definitions, "inline"


def _version_tuple(value):
    out = []
    for part in value.lstrip("v").split("."):
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        out.append(int(digits or 0))
    return tuple([*out, 0, 0, 0][:3])


def main():
    request = json.load(sys.stdin)
    source = request["source"]
    version = _version()
    parsed = _version_tuple(version)
    definitions = {}
    dialect = "inline"
    method = "native-layout"
    if parsed >= (0, 2, 16):
        layout = _native_layout(source)
        # Native layouts carry the authoritative slot table.  Pair it with
        # compiler type objects to recover struct members and reliable spans.
        try:
            annotated, definitions, _ = _annotated_layout(source)
            namespace = layout.get("storage_layout", layout)
            function_lock_keys = {
                name.split(".nonreentrant.", 1)[1].split("@", 1)[0]
                for name in annotated
                if name.startswith("$.nonreentrant.")
            }
            if parsed <= (0, 3, 0):
                for key in function_lock_keys:
                    namespace.pop(f"nonreentrant.{key}", None)
            for name, item in annotated.items():
                if name.startswith("$.nonreentrant.") and parsed <= (0, 3, 0):
                    namespace[name] = item
                elif name in namespace and isinstance(namespace[name], dict):
                    namespace[name].setdefault("n_slots", item.get("n_slots"))
        except Exception:
            pass
    elif parsed >= (0, 2, 13):
        layout, definitions, dialect = _annotated_layout(source)
        method = "annotated-ast"
    else:
        layout, definitions, dialect = _legacy_global_context(source)
        method = "global-context"
    json.dump(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": version,
            "contract": request.get("contract"),
            "layout": layout,
            "type_definitions": definitions,
            "extraction": {"method": method, "storage_dialect": dialect},
        },
        sys.stdout,
        sort_keys=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        sys.exit(1)
