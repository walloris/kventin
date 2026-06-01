#!/usr/bin/env python3
"""Validate a generated test-case XML against the project example.

The example file is the contract: the generated file must mirror its structure
(root tag, element names, parent->child relationships) and must not drop elements
that appear in every example case. The allowed structure is derived from the
example automatically, so replacing the example does not require code changes.

Usage:
    validate_xml.py <generated.xml> <example.xml>

Exit code 0 and prints "VALID" on success; non-zero and prints reasons otherwise.
"""

import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


def _local(tag: str) -> str:
    """Strip XML namespace, keep local tag name."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(path: Path):
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return None, f"{path.name}: XML не well-formed: {exc}"
    except FileNotFoundError:
        return None, f"{path.name}: файл не найден"
    return tree.getroot(), None


def _relations(root) -> set:
    """Set of (parent_tag, child_tag) pairs in the tree."""
    pairs = set()
    for parent in root.iter():
        for child in list(parent):
            pairs.add((_local(parent.tag), _local(child.tag)))
    return pairs


def _tags(root) -> set:
    return {_local(el.tag) for el in root.iter()}


def _case_container_tag(root):
    """Resolve the test-case element tag from the example (e.g. Zephyr export)."""
    for el in root.iter():
        if _local(el.tag) == "testCases":
            kids = list(el)
            if kids:
                return _local(kids[0].tag)
    for name in ("testCase", "test-case", "case"):
        if any(_local(el.tag) == name for el in root.iter()):
            return name
    children = Counter(_local(c.tag) for c in list(root))
    return children.most_common(1)[0][0] if children else None


def _direct_child_tags(element) -> set:
    """Local tag names of direct child elements only (not nested descendants)."""
    return {_local(child.tag) for child in list(element)}


def _required_child_tags(root, case_tag):
    """Direct child tags present in EVERY case node of the example -> mandatory."""
    cases = [el for el in root.iter() if _local(el.tag) == case_tag]
    if not cases:
        return set()
    per_case = [_direct_child_tags(case) for case in cases]
    required = set(per_case[0])
    for tags in per_case[1:]:
        required &= tags
    return required


def validate(generated: Path, example: Path):
    gen_root, err = _parse(generated)
    if err:
        return False, [err]
    ex_root, err = _parse(example)
    if err:
        return False, [f"образец: {err}"]

    problems = []

    if _local(gen_root.tag) != _local(ex_root.tag):
        problems.append(
            f"корневой тег '{_local(gen_root.tag)}' != образца '{_local(ex_root.tag)}'"
        )

    allowed_tags = _tags(ex_root)
    unknown = _tags(gen_root) - allowed_tags
    if unknown:
        problems.append(
            "теги, которых нет в образце (нельзя выдумывать структуру): "
            + ", ".join(sorted(unknown))
        )

    allowed_rel = _relations(ex_root)
    bad_rel = _relations(gen_root) - allowed_rel
    if bad_rel:
        problems.append(
            "недопустимая вложенность (родитель→потомок): "
            + ", ".join(f"{p}→{c}" for p, c in sorted(bad_rel))
        )

    case_tag = _case_container_tag(ex_root)
    if case_tag:
        gen_cases = [el for el in gen_root.iter() if _local(el.tag) == case_tag]
        if not gen_cases:
            problems.append(f"нет ни одного узла кейса <{case_tag}>")
        required = _required_child_tags(ex_root, case_tag)
        for case in gen_cases:
            present = _direct_child_tags(case)
            missing = required - present
            if missing:
                cid = case.get("id") or "<без id>"
                problems.append(
                    f"кейс '{cid}': отсутствуют обязательные элементы образца: "
                    + ", ".join(sorted(missing))
                )

    return (not problems), problems


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: validate_xml.py <generated.xml> <example.xml>")
        sys.exit(2)

    ok, issues = validate(Path(sys.argv[1]), Path(sys.argv[2]))
    if ok:
        print("VALID")
        sys.exit(0)
    print("INVALID:")
    for i in issues:
        print(f"  - {i}")
    sys.exit(1)
